"""
nginx JSON access-log handling: parsing, route normalisation, batch loading and
online tailing.

parse_line() is the single parser used by the training pipeline AND the online
detector, so offline and online features come from identical records.
Log format: `json_analytics` from business/nginx/nginx.conf (step 1).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# Flat record produced from one log line  (nginx field in brackets)
RECORD_COLUMNS = [
    "ts",           # completion time, epoch s          [msec]
    "method",       # HTTP method                       [request_method]
    "path",         # URI without query string          [request_uri]
    "route",        # normalised route template         [request_uri]
    "status",       # HTTP status                       [status]
    "bytes",        # response body size                [body_bytes_sent]
    "req_len",      # request size incl. headers        [request_length]
    "rt",           # total request time, s             [request_time]
    "upstream_rt",  # backend time, s (NaN = static)    [upstream_response_time]
    "client",       # synthetic client IP               [x_forwarded_for | remote_addr]
    "conn_req",     # requests on this keep-alive conn  [connection_requests]
]

# Ids are not features: collapse them into templates (bounded cardinality)
_TEMPLATED_ROUTES = [
    (re.compile(r"^/api/products/[^/]+$"), "/api/products/{id}"),
    (re.compile(r"^/api/orders/[^/]+$"), "/api/orders/{id}"),
]

# Everything the Demo Shop legitimately serves; anything else becomes "other"
KNOWN_ROUTES = frozenset({
    "/", "/index.html", "/static/app.js", "/static/style.css",
    "/api/products", "/api/orders", "/api/products/{id}", "/api/orders/{id}",
    "/api/health", "/api/chaos", "/docs", "/redoc", "/openapi.json",
})


def normalize_route(path: str) -> str:
    for pattern, template in _TEMPLATED_ROUTES:
        if pattern.match(path):
            return template
    return path if path in KNOWN_ROUTES else "other"


def parse_upstream_time(value) -> float:
    """
    nginx upstream timings are strings: '' / '-' (no upstream, e.g. static files),
    '0.031', '0.002, 0.031' (retries) or '0.01 : 0.02' (internal redirects).
    Returns the summed time or NaN when no upstream was involved.
    """
    if value is None:
        return math.nan
    if isinstance(value, (int, float)):
        return float(value)
    total, found = 0.0, False
    for part in re.split(r"[,:]", str(value)):
        try:
            total += float(part.strip())
            found = True
        except ValueError:
            continue
    return total if found else math.nan


def parse_line(line: bytes | str) -> dict | None:
    """Parse one JSON log line into a flat record; None for malformed lines."""
    try:
        rec = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(rec, dict) or "msec" not in rec:
        return None
    uri = rec.get("request_uri") or ""
    path = uri.split("?", 1)[0] or "/"
    xff = rec.get("x_forwarded_for") or ""
    client = xff.split(",")[0].strip() or str(rec.get("remote_addr", ""))
    try:
        return {
            "ts": float(rec["msec"]),
            "method": str(rec.get("request_method", "")),
            "path": path,
            "route": normalize_route(path),
            "status": int(rec.get("status", 0)),
            "bytes": int(rec.get("body_bytes_sent", 0) or 0),
            "req_len": int(rec.get("request_length", 0) or 0),
            "rt": float(rec.get("request_time", 0.0) or 0.0),
            "upstream_rt": parse_upstream_time(rec.get("upstream_response_time")),
            "client": client,
            "conn_req": int(rec.get("connection_requests", 1) or 1),
        }
    except (TypeError, ValueError):
        return None


def load_logs(path: Path, since_ts: float | None = None) -> tuple[pd.DataFrame, int]:
    """Batch-load the log (streamed line by line). Returns (records sorted by ts, bad lines)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"nginx log not found: {path}")
    records, bad = [], 0
    with open(path, "rb") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = parse_line(line)
            if rec is None:
                bad += 1
            elif since_ts is None or rec["ts"] >= since_ts:
                records.append(rec)
    df = pd.DataFrame.from_records(records, columns=RECORD_COLUMNS)
    df = df.sort_values("ts", kind="stable").reset_index(drop=True)
    if bad:
        log.warning("Skipped %d malformed log lines", bad)
    return df, bad


def read_tail_records(path: Path, max_bytes: int) -> list[dict]:
    """Parse the last `max_bytes` of the log (used to prime the online history)."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - max_bytes))
            if size > max_bytes:
                fh.readline()  # first line is probably partial
            data = fh.read()
    except FileNotFoundError:
        return []
    return [r for r in (parse_line(l) for l in data.splitlines() if l.strip()) if r is not None]


def log_time_span(path: Path, probe_bytes: int = 65536) -> float:
    """Seconds between the first and last log line (cheap: reads head + tail only)."""
    try:
        with open(path, "rb") as fh:
            first = None
            for line in fh:
                first = parse_line(line)
                if first:
                    break
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - probe_bytes))
            tail = [parse_line(l) for l in fh.read().splitlines() if l.strip()]
    except FileNotFoundError:
        return 0.0
    tail = [r for r in tail if r]
    if not first or not tail:
        return 0.0
    return max(0.0, tail[-1]["ts"] - first["ts"])


class LogTailer:
    """`tail -F` for the access log: survives rotation (inode change) and truncation,
    buffers partial lines. Polling-based, no inotify needed (works on Docker Desktop)."""

    def __init__(self, path: Path, start_at_end: bool = True) -> None:
        self.path = Path(path)
        self._start_at_end = start_at_end
        self._fh = None
        self._inode = None
        self._buf = b""
        self.reopens = 0

    def _open(self, at_end: bool) -> bool:
        try:
            fh = open(self.path, "rb")
        except FileNotFoundError:
            return False
        if at_end:
            fh.seek(0, os.SEEK_END)
        self._fh, self._inode, self._buf = fh, os.fstat(fh.fileno()).st_ino, b""
        return True

    def _drain(self) -> list[bytes]:
        data = self._fh.read()
        if not data:
            return []
        data = self._buf + data
        *lines, self._buf = data.split(b"\n")
        return [l for l in lines if l.strip()]

    def read_lines(self) -> list[bytes]:
        """Complete new lines since the previous call."""
        if self._fh is None:
            if not self._open(at_end=self._start_at_end):
                return []
            self._start_at_end = False  # a file created later is read from the start
            return self._drain()
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return self._drain()  # rotated away, new file not created yet
        if st.st_ino != self._inode or st.st_size < self._fh.tell():
            lines = self._drain()  # rest of the old file
            self._fh.close()
            self._fh = None
            self.reopens += 1
            if self._open(at_end=False):
                lines += self._drain()
            return lines
        return self._drain()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None