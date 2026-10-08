"""
nginx-log-exporter: tails the nginx JSON access log and exposes Prometheus metrics.

Why not only stub_status? stub_status gives connection counters only. The anomaly
scenarios of step 1 show up in status codes, latency ($request_time) and payload
size ($body_bytes_sent), which exist only in the access log. This exporter turns
the log into counters/histograms so Prometheus/Grafana see the same signal the
Isolation Forest will consume in step 3.

Exposed metrics (port EXPORTER_PORT, default 9102):
  nginx_log_requests_total{method,route,status,status_class}   counter
  nginx_log_request_duration_seconds{route}                    histogram
  nginx_log_response_size_bytes{route}                         histogram
  nginx_log_lines_total / nginx_log_parse_errors_total         counters
  nginx_log_file_reopens_total                                 counter
  nginx_log_last_event_timestamp_seconds / nginx_log_lag_seconds   gauges
  nginx_log_routes_tracked                                     gauge
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram, start_http_server

log = logging.getLogger("nginx-log-exporter")

# Buckets tuned to the stand: normal latency is tens of ms, degradation adds 0.8-2.5 s.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)
# Normal catalog responses are KBs, heavy_payload multiplies them by 20-100.
SIZE_BUCKETS = (256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216)

KNOWN_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
ROUTE_UNKNOWN = "__unknown__"  # 404 to a never-seen path (e.g. not_found_storm)
ROUTE_OTHER = "__other__"      # cardinality cap reached

# Default candidate field names; the first present key wins.
DEFAULT_FIELDS = {
    "time": ["msec", "time_iso8601", "time", "timestamp", "@timestamp", "ts"],
    "status": ["status"],
    "method": ["request_method", "method"],
    "uri": ["uri", "request_uri", "path", "url"],
    "request_time": ["request_time", "duration"],
    "bytes": ["body_bytes_sent", "bytes_sent", "bytes"],
}


# --------------------------------------------------------------------------- parsing
@dataclass
class ParsedLine:
    ts: float | None
    method: str
    path: str
    status: int
    request_time: float
    body_bytes: float


def _first(obj: dict, keys: Iterable[str]) -> Any:
    """Return the first non-empty value among candidate keys (nginx writes '-' for empty)."""
    for k in keys:
        v = obj.get(k)
        if v not in (None, "", "-"):
            return v
    return None


def _to_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        # upstream-like values may look like "0.012, 0.003" -> take the first
        return float(str(v).split(",")[0].strip())
    except ValueError:
        return default


def parse_ts(v: Any) -> float | None:
    """Accept epoch seconds (msec: '1791461400.123') or ISO-8601 ('2026-10-08T12:10:00+00:00')."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def parse_line(line: str, fields: dict[str, list[str]] = DEFAULT_FIELDS) -> ParsedLine:
    """Parse one JSON log line. Raises ValueError on garbage."""
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError("log line is not a JSON object")

    method = _first(obj, fields["method"])
    path = _first(obj, fields["uri"])
    if path is None and isinstance(obj.get("request"), str):
        # fallback: "$request" = "GET /api/products?limit=2 HTTP/1.1"
        parts = obj["request"].split()
        if len(parts) >= 2:
            method = method or parts[0]
            path = parts[1]
    if path is None:
        raise ValueError("no uri field")

    status_raw = _first(obj, fields["status"])
    if status_raw is None:
        raise ValueError("no status field")
    status = int(status_raw)

    return ParsedLine(
        ts=parse_ts(_first(obj, fields["time"])),
        method=str(method or "UNKNOWN").upper(),
        path=str(path),
        status=status,
        request_time=max(_to_float(_first(obj, fields["request_time"])), 0.0),
        body_bytes=max(_to_float(_first(obj, fields["bytes"])), 0.0),
    )


# ------------------------------------------------------------------ route labels
_ID_SEGMENT = re.compile(r"^(\d+|[0-9a-fA-F]{8,}|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27})$")


def normalize_route(path: str) -> str:
    """/api/products/17?x=1 -> /api/products/:id  (keeps label cardinality bounded)."""
    path = path.split("?", 1)[0].split("#", 1)[0][:200] or "/"
    segs = [":id" if _ID_SEGMENT.match(s) else s for s in path.split("/")]
    route = "/".join(segs)
    if len(route) > 1 and route.endswith("/"):
        route = route.rstrip("/") or "/"
    return route


class RouteRegistry:
    """Decides which routes become label values.

    * routes answered with non-404 are registered until MAX_ROUTES is reached;
    * a 404 to an unregistered route goes to `__unknown__` -> the not_found_storm
      scenario is visible as a spike of route="__unknown__" without exploding
      the TSDB with random URIs;
    * after the cap, everything new goes to `__other__`.
    """

    def __init__(self, max_routes: int = 40, known: Iterable[str] = ()):
        self.max_routes = max_routes
        self._known: set[str] = {r for r in known if r}

    def resolve(self, route: str, status: int) -> str:
        if route in self._known:
            return route
        if status == 404:
            return ROUTE_UNKNOWN
        if len(self._known) >= self.max_routes:
            return ROUTE_OTHER
        self._known.add(route)
        log.info("new route label: %s", route)
        return route

    def __len__(self) -> int:
        return len(self._known)


# -------------------------------------------------------------------- metrics
class LogMetrics:
    def __init__(self, registry: CollectorRegistry = REGISTRY):
        self.requests = Counter("nginx_log_requests_total", "Requests parsed from the nginx access log",
                                ["method", "route", "status", "status_class"], registry=registry)
        self.duration = Histogram("nginx_log_request_duration_seconds", "nginx $request_time",
                                  ["route"], buckets=LATENCY_BUCKETS, registry=registry)
        self.size = Histogram("nginx_log_response_size_bytes", "nginx $body_bytes_sent",
                              ["route"], buckets=SIZE_BUCKETS, registry=registry)
        self.lines = Counter("nginx_log_lines", "Log lines read (incl. unparsable)", registry=registry)
        self.parse_errors = Counter("nginx_log_parse_errors", "Unparsable log lines", registry=registry)
        self.reopens = Counter("nginx_log_file_reopens", "Log file rotations/truncations detected",
                               registry=registry)
        self.last_event = Gauge("nginx_log_last_event_timestamp_seconds",
                                "Timestamp of the newest processed log event", registry=registry)
        self.lag = Gauge("nginx_log_lag_seconds", "Now minus newest log event time", registry=registry)
        self.routes = Gauge("nginx_log_routes_tracked", "Distinct route label values", registry=registry)


class LineProcessor:
    """Glue: raw line -> parsed record -> metric updates."""

    def __init__(self, metrics: LogMetrics, routes: RouteRegistry, fields=DEFAULT_FIELDS):
        self.m, self.routes, self.fields = metrics, routes, fields
        self.started = time.time()
        self.last_event_ts: float | None = None
        self.m.lag.set_function(self._lag)
        self.m.routes.set_function(lambda: len(self.routes))

    def _lag(self) -> float:
        return max(time.time() - (self.last_event_ts or self.started), 0.0)

    def process(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        self.m.lines.inc()
        try:
            rec = parse_line(line, self.fields)
        except (ValueError, TypeError) as exc:
            self.m.parse_errors.inc()
            log.debug("cannot parse %r: %s", line[:200], exc)
            return

        route = self.routes.resolve(normalize_route(rec.path), rec.status)
        method = rec.method if rec.method in KNOWN_METHODS else "OTHER"
        self.m.requests.labels(method, route, str(rec.status), f"{rec.status // 100}xx").inc()
        self.m.duration.labels(route).observe(rec.request_time)
        self.m.size.labels(route).observe(rec.body_bytes)

        ts = rec.ts if rec.ts is not None else time.time()
        if self.last_event_ts is None or ts > self.last_event_ts:
            self.last_event_ts = ts
            self.m.last_event.set(ts)


# ---------------------------------------------------------------------- tailing
class FileTailer:
    """Minimal `tail -F`: survives missing file, truncation and rotation (inode change)."""

    def __init__(self, path: str, start_at_end: bool = True):
        self.path = path
        self._skip_history = start_at_end  # only for the very first open
        self._fh = None
        self._ident: tuple[int, int] | None = None
        self._buf = b""
        self.reopens = 0

    def _open(self) -> bool:
        try:
            fh = open(self.path, "rb")
        except FileNotFoundError:
            return False
        st = os.fstat(fh.fileno())
        if self._skip_history:
            fh.seek(0, os.SEEK_END)
        self._skip_history = False  # a rotated/recreated file is read from the start
        self._fh, self._ident, self._buf = fh, (st.st_dev, st.st_ino), b""
        log.info("tailing %s from offset %d", self.path, fh.tell())
        return True

    def _read_available(self, max_lines: int) -> list[str]:
        out: list[str] = []
        while len(out) < max_lines:
            chunk = self._fh.readline()
            if not chunk:
                break
            if not chunk.endswith(b"\n"):  # partial line: nginx is still writing it
                self._buf += chunk
                break
            out.append((self._buf + chunk).decode("utf-8", errors="replace"))
            self._buf = b""
        return out

    def read_lines(self, max_lines: int = 20000) -> list[str]:
        if self._fh is None and not self._open():
            return []
        lines = self._read_available(max_lines)
        if lines:
            return lines
        # Nothing new: check whether the file was rotated or truncated.
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return []  # removed; keep the old handle until a new file appears
        if (st.st_dev, st.st_ino) != self._ident or st.st_size < self._fh.tell():
            log.warning("log file rotated/truncated, reopening")
            self._fh.close()
            self._fh = None
            self.reopens += 1
            if self._open():
                return self._read_available(max_lines)
        return []


# ------------------------------------------------------------------------ config
def _fields_from_env() -> dict[str, list[str]]:
    fields = {k: list(v) for k, v in DEFAULT_FIELDS.items()}
    for key in fields:
        override = os.getenv(f"FIELD_{key.upper()}", "").strip()
        if override:
            fields[key] = [x.strip() for x in override.split(",") if x.strip()]
    return fields


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    path = os.getenv("LOG_PATH", "/logs/access.json.log")
    port = int(os.getenv("EXPORTER_PORT", "9102"))
    poll = float(os.getenv("POLL_INTERVAL_S", "0.5"))
    known = [r.strip() for r in os.getenv("KNOWN_ROUTES", "").split(",")]

    metrics = LogMetrics()
    proc = LineProcessor(metrics, RouteRegistry(int(os.getenv("MAX_ROUTES", "40")), known), _fields_from_env())
    tailer = FileTailer(path, start_at_end=os.getenv("LOG_START_AT", "end").lower() != "beginning")

    start_http_server(port)
    log.info("metrics on :%d, log %s", port, path)

    running = True

    def _stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    reopens_seen = 0
    while running:
        lines = tailer.read_lines()
        for line in lines:
            proc.process(line)
        if tailer.reopens > reopens_seen:
            metrics.reopens.inc(tailer.reopens - reopens_seen)
            reopens_seen = tailer.reopens
        if not lines:
            time.sleep(poll)
    log.info("stopped")


if __name__ == "__main__":
    main()
