"""
gt-annotator: turns ground-truth anomaly windows into Grafana annotations and
Prometheus metrics.

Input : data/ground-truth/anomalies.jsonl written by the step-1 traffic-generator,
        one JSON object per injected scenario (scenario, start_ts, end_ts, params).
Output: * Grafana region annotations tagged `ground-truth` + `<scenario>`
          (dashboards show them as shaded areas => visual check of detectors);
        * metrics on EXPORTER_PORT (default 9103):
            stand_ground_truth_anomaly_active{scenario}     1 while now is inside a window
            stand_ground_truth_windows{scenario}            windows read from the file
            stand_ground_truth_annotations_pushed_total     annotations created in Grafana
            stand_ground_truth_push_errors_total            failed Grafana API calls
            stand_ground_truth_last_window_end_timestamp_seconds

Idempotent: every annotation carries a `gtid:<scenario>-<start_ts>` tag; existing
ids are loaded from Grafana at start, so restarts never duplicate annotations.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests
from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, start_http_server

log = logging.getLogger("gt-annotator")

SCENARIOS = ("traffic_spike", "latency_degradation", "error_burst", "not_found_storm", "heavy_payload")
TAG = "ground-truth"


@dataclass(frozen=True)
class Window:
    scenario: str
    start_ts: float
    end_ts: float
    params: str  # compact JSON for the annotation text

    @property
    def gtid(self) -> str:
        return f"gtid:{self.scenario}-{int(self.start_ts)}"

    def active(self, now: float) -> bool:
        return self.start_ts <= now < self.end_ts


def _ts(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def parse_window(line: str) -> Window | None:
    """One jsonl record -> Window. Prefers *_ts epoch fields, falls back to ISO strings."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or not obj.get("scenario"):
        return None
    start = _ts(obj.get("start_ts")) or _ts(obj.get("start"))
    end = _ts(obj.get("end_ts")) or _ts(obj.get("end"))
    if start is None:
        return None
    if end is None:
        end = start + float(obj.get("duration_s") or 0)
    return Window(str(obj["scenario"]), start, max(end, start),
                  json.dumps(obj.get("params", {}), separators=(",", ":"), sort_keys=True))


class JsonlReader:
    """Incrementally reads complete lines; restarts from 0 if the file shrinks (data reset)."""

    def __init__(self, path: str):
        self.path, self.offset, self._buf = path, 0, b""

    def read_new(self) -> tuple[list[str], bool]:
        """Returns (new_lines, was_reset)."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return [], False
        reset = size < self.offset
        if reset:
            self.offset, self._buf = 0, b""
        if size == self.offset:
            return [], reset
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            data = fh.read()
        self.offset += len(data)
        data = self._buf + data
        *complete, self._buf = data.split(b"\n")
        return [c.decode("utf-8", "replace") for c in complete if c.strip()], reset


class GrafanaClient:
    def __init__(self, url: str, user: str, password: str, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.auth = (user, password)
        self.timeout = timeout

    def wait_ready(self, stop) -> None:
        while not stop():
            try:
                if self.s.get(f"{self.url}/api/health", timeout=self.timeout).ok:
                    return
            except requests.RequestException:
                pass
            log.info("waiting for Grafana at %s ...", self.url)
            time.sleep(3)

    def existing_ids(self) -> set[str]:
        r = self.s.get(f"{self.url}/api/annotations",
                       params={"tags": TAG, "type": "annotation", "limit": 100000}, timeout=self.timeout)
        r.raise_for_status()
        return {t for a in r.json() for t in a.get("tags", []) if t.startswith("gtid:")}

    def create(self, w: Window) -> None:
        body = {  # organisation-wide region annotation (no dashboardUID)
            "time": int(w.start_ts * 1000),
            "timeEnd": int(w.end_ts * 1000),
            "tags": [TAG, w.scenario, w.gtid],
            "text": f"<b>GT: {w.scenario}</b> ({int(w.end_ts - w.start_ts)} s)<br/>params: {w.params}",
        }
        self.s.post(f"{self.url}/api/annotations", json=body, timeout=self.timeout).raise_for_status()


class GTMetrics:
    def __init__(self, registry: CollectorRegistry = REGISTRY):
        self.active = Gauge("stand_ground_truth_anomaly_active", "1 while a ground-truth window is active",
                            ["scenario"], registry=registry)
        self.windows = Gauge("stand_ground_truth_windows", "Ground-truth windows in the file",
                             ["scenario"], registry=registry)
        self.pushed = Counter("stand_ground_truth_annotations_pushed", "Annotations created", registry=registry)
        self.errors = Counter("stand_ground_truth_push_errors", "Grafana API errors", registry=registry)
        self.last_end = Gauge("stand_ground_truth_last_window_end_timestamp_seconds",
                              "End of the latest window", registry=registry)
        for s in SCENARIOS:  # pre-create series so panels show 0 instead of "No data"
            self.active.labels(s).set(0)
            self.windows.labels(s).set(0)


def update_gauges(m: GTMetrics, windows: list[Window], now: float) -> None:
    scen = set(SCENARIOS) | {w.scenario for w in windows}
    for s in scen:
        ws = [w for w in windows if w.scenario == s]
        m.active.labels(s).set(1 if any(w.active(now) for w in ws) else 0)
        m.windows.labels(s).set(len(ws))
    if windows:
        m.last_end.set(max(w.end_ts for w in windows))


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    path = os.getenv("GT_PATH", "/ground-truth/anomalies.jsonl")
    poll = float(os.getenv("POLL_INTERVAL_S", "5"))
    running = True

    def _stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    m = GTMetrics()
    start_http_server(int(os.getenv("EXPORTER_PORT", "9103")))
    graf = GrafanaClient(os.getenv("GRAFANA_URL", "http://grafana:3000"),
                         os.getenv("GRAFANA_USER", "admin"), os.getenv("GRAFANA_PASSWORD", "admin"))
    graf.wait_ready(lambda: not running)

    pushed: set[str] = set()
    synced = False
    reader = JsonlReader(path)
    windows: list[Window] = []
    pending: list[Window] = []

    while running:
        if not synced:  # load ids already in Grafana (restart-safe)
            try:
                pushed = graf.existing_ids()
                synced = True
                log.info("%d ground-truth annotations already in Grafana", len(pushed))
            except requests.RequestException as exc:
                m.errors.inc()
                log.warning("cannot list annotations: %s", exc)

        lines, reset = reader.read_new()
        if reset:
            log.warning("%s shrank -> data reset, re-reading", path)
            windows.clear()
        for line in lines:
            w = parse_window(line)
            if w is None:
                log.warning("skip bad ground-truth line: %r", line[:200])
                continue
            windows.append(w)
            pending.append(w)

        if synced and pending:
            still: list[Window] = []
            for w in pending:
                if w.gtid in pushed:
                    continue
                try:
                    graf.create(w)
                    pushed.add(w.gtid)
                    m.pushed.inc()
                    log.info("annotation created: %s", w.gtid)
                except requests.RequestException as exc:
                    m.errors.inc()
                    still.append(w)
                    log.warning("annotation %s failed: %s (will retry)", w.gtid, exc)
            pending = still

        update_gauges(m, windows, time.time())
        time.sleep(poll)


if __name__ == "__main__":
    main()
