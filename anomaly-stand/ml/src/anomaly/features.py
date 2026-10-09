"""
Feature engineering: nginx log fields -> one feature vector per time window.

The SAME compute_features()/FeatureBuilder is used by the training pipeline
(build_feature_frame) and the online detector (WindowAggregator + FeatureBuilder),
so there is no training/serving skew (checked in tests/test_ml.py).

Windows are keyed by log time ($msec = request completion time).
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Ordered list = model input columns
FEATURES = [
    "rps", "rps_rel",
    "ratio_4xx", "ratio_404", "ratio_5xx",
    "rt_mean_ms", "rt_p50_ms", "rt_p95_ms", "rt_max_ms", "upstream_rt_p95_ms",
    "bytes_mean", "bytes_p95", "bytes_max", "req_len_mean",
    "ratio_post", "ratio_api", "ratio_unknown_route", "uri_unique_ratio",
    "clients_unique", "conn_reuse_mean",
]

# Heavy-tailed features get log1p() inside the model pipeline
LOG_FEATURES = [
    "rps", "rps_rel", "rt_mean_ms", "rt_p50_ms", "rt_p95_ms", "rt_max_ms", "upstream_rt_p95_ms",
    "bytes_mean", "bytes_p95", "bytes_max", "req_len_mean", "clients_unique", "conn_reuse_mean",
]

# Feature -> nginx log field(s) it is computed from (docs, reports, Grafana)
FEATURE_SOURCES = {
    "rps": "count(lines) / window  [msec]",
    "rps_rel": "count / median(count of previous BASELINE_WINDOWS windows)  [msec]",
    "ratio_4xx": "share of 4xx  [status]",
    "ratio_404": "share of 404  [status]",
    "ratio_5xx": "share of 5xx  [status]",
    "rt_mean_ms": "mean  [request_time]",
    "rt_p50_ms": "median  [request_time]",
    "rt_p95_ms": "p95  [request_time]",
    "rt_max_ms": "max  [request_time]",
    "upstream_rt_p95_ms": "p95 of proxied requests  [upstream_response_time]",
    "bytes_mean": "mean  [body_bytes_sent]",
    "bytes_p95": "p95  [body_bytes_sent]",
    "bytes_max": "max  [body_bytes_sent]",
    "req_len_mean": "mean  [request_length]",
    "ratio_post": "share of POST  [request_method]",
    "ratio_api": "share of /api/*  [request_uri]",
    "ratio_unknown_route": "share of routes the shop does not serve  [request_uri]",
    "uri_unique_ratio": "distinct paths / requests  [request_uri]",
    "clients_unique": "distinct clients  [x_forwarded_for]",
    "conn_reuse_mean": "mean keep-alive reuse  [connection_requests]",
}


@dataclass
class WindowData:
    """Column arrays of all log records that completed inside one window."""
    status: np.ndarray
    rt: np.ndarray
    upstream_rt: np.ndarray
    bytes: np.ndarray
    req_len: np.ndarray
    is_post: np.ndarray
    is_api: np.ndarray
    unknown_route: np.ndarray
    path: np.ndarray
    client: np.ndarray
    conn_req: np.ndarray

    @property
    def n(self) -> int:
        return int(self.status.shape[0])

    @classmethod
    def empty(cls) -> "WindowData":
        f, o, b = np.array([], dtype=float), np.array([], dtype=object), np.array([], dtype=bool)
        return cls(status=np.array([], dtype=int), rt=f, upstream_rt=f, bytes=f, req_len=f,
                   is_post=b, is_api=b, unknown_route=b, path=o, client=o, conn_req=f)

    @classmethod
    def from_columns(cls, cols: dict) -> "WindowData":
        path = np.asarray(cols["path"], dtype=object)
        return cls(
            status=np.asarray(cols["status"], dtype=int),
            rt=np.asarray(cols["rt"], dtype=float),
            upstream_rt=np.asarray(cols["upstream_rt"], dtype=float),
            bytes=np.asarray(cols["bytes"], dtype=float),
            req_len=np.asarray(cols["req_len"], dtype=float),
            is_post=np.asarray(cols["method"], dtype=object) == "POST",
            is_api=np.array([str(p).startswith("/api/") for p in path], dtype=bool),
            unknown_route=np.asarray(cols["route"], dtype=object) == "other",
            path=path,
            client=np.asarray(cols["client"], dtype=object),
            conn_req=np.asarray(cols["conn_req"], dtype=float),
        )

    @classmethod
    def from_records(cls, records: list[dict]) -> "WindowData":
        if not records:
            return cls.empty()
        keys = ("status", "rt", "upstream_rt", "bytes", "req_len", "method", "path", "route", "client", "conn_req")
        return cls.from_columns({k: [r[k] for r in records] for k in keys})


def compute_features(w: WindowData, hist_median: float | None, window_s: int) -> dict[str, float]:
    """Feature vector of one window. hist_median=None -> not enough history yet."""
    n = w.n
    f = dict.fromkeys(FEATURES, 0.0)
    f["rps"] = n / window_s
    f["rps_rel"] = n / max(hist_median, 1.0) if hist_median is not None else 1.0
    if n == 0:  # traffic stopped: the rest stays 0 (that is itself an anomaly signature)
        return f

    s = w.status
    f["ratio_4xx"] = float(np.mean((s >= 400) & (s < 500)))
    f["ratio_404"] = float(np.mean(s == 404))
    f["ratio_5xx"] = float(np.mean(s >= 500))

    rt_ms = w.rt * 1000.0
    p50, p95 = np.percentile(rt_ms, [50, 95])
    f["rt_mean_ms"], f["rt_p50_ms"], f["rt_p95_ms"] = float(rt_ms.mean()), float(p50), float(p95)
    f["rt_max_ms"] = float(rt_ms.max())
    up = w.upstream_rt[np.isfinite(w.upstream_rt)] * 1000.0
    f["upstream_rt_p95_ms"] = float(np.percentile(up, 95)) if up.size else 0.0

    b = w.bytes
    f["bytes_mean"], f["bytes_p95"], f["bytes_max"] = float(b.mean()), float(np.percentile(b, 95)), float(b.max())
    f["req_len_mean"] = float(w.req_len.mean())

    f["ratio_post"] = float(w.is_post.mean())
    f["ratio_api"] = float(w.is_api.mean())
    f["ratio_unknown_route"] = float(w.unknown_route.mean())
    f["uri_unique_ratio"] = len(set(w.path.tolist())) / n
    f["clients_unique"] = float(len(set(w.client.tolist())))
    f["conn_reuse_mean"] = float(w.conn_req.mean())
    return f


class FeatureBuilder:
    """Stateful wrapper: keeps the request-count history needed by rps_rel."""

    def __init__(self, window_s: int, baseline_windows: int, min_history: int) -> None:
        self.window_s = window_s
        self.min_history = min_history
        self.history: deque[int] = deque(maxlen=baseline_windows)

    def reset(self) -> None:
        self.history.clear()

    def prime(self, counts) -> None:
        for c in counts:
            self.history.append(int(c))

    def push(self, w: WindowData) -> dict[str, float]:
        med = float(np.median(self.history)) if len(self.history) >= self.min_history else None
        feats = compute_features(w, med, self.window_s)
        self.history.append(w.n)
        return feats


class WindowAggregator:
    """Online: buckets records into windows and releases a window once it is complete
    (window end + grace has passed). The window in progress at start is skipped."""

    def __init__(self, window_s: int, grace_s: float, start_ts: float, max_catchup_windows: int = 240) -> None:
        self.window_s = window_s
        self.grace_s = grace_s
        self.next_idx = int(start_ts // window_s) + 1
        self.buckets: dict[int, list[dict]] = defaultdict(list)
        self.max_catchup = max_catchup_windows
        self.late = 0      # lines whose window was already scored
        self.skipped = 0   # windows dropped after a long stall

    def add(self, rec: dict) -> None:
        k = int(rec["ts"] // self.window_s)
        if k < self.next_idx:
            self.late += 1
            return
        self.buckets[k].append(rec)

    def flush(self, now: float) -> list[tuple[float, list[dict]]]:
        """[(window_start_ts, records)] for every complete window, in order."""
        W = self.window_s
        ready_until = int((now - self.grace_s) // W)  # idx < ready_until are complete
        if ready_until - self.next_idx > self.max_catchup:  # host suspended / detector stalled
            new_idx = ready_until - self.max_catchup
            self.skipped += new_idx - self.next_idx
            for k in [k for k in self.buckets if k < new_idx]:
                del self.buckets[k]
            self.next_idx = new_idx
        out = []
        while self.next_idx < ready_until:
            k = self.next_idx
            out.append((float(k * W), self.buckets.pop(k, [])))
            self.next_idx += 1
        return out


def build_feature_frame(df: pd.DataFrame, window_s: int, baseline_windows: int,
                        min_history: int, max_gap_windows: int) -> pd.DataFrame:
    """
    Offline: records (logs.load_logs) -> DataFrame [window_start, n_requests, FEATURES...].
    Partial first/last windows are dropped. Runs of more than max_gap_windows empty
    windows mean "stand was down": skipped, history reset (like a detector restart).
    """
    cols = ["window_start", "n_requests"] + FEATURES
    if df.empty:
        return pd.DataFrame(columns=cols)
    idx = np.floor(df["ts"].to_numpy() / window_s).astype(np.int64)
    arrays = {c: df[c].to_numpy() for c in df.columns}
    builder = FeatureBuilder(window_s, baseline_windows, min_history)
    rows: list[dict] = []
    empty_run: list[int] = []

    def emit(k: int, w: WindowData) -> None:
        rows.append({"window_start": float(k * window_s), "n_requests": w.n, **builder.push(w)})

    def flush_empty() -> None:
        if len(empty_run) > max_gap_windows:
            builder.reset()
        else:
            for k in empty_run:
                emit(k, WindowData.empty())
        empty_run.clear()

    for k in range(int(idx[0]) + 1, int(idx[-1])):
        lo, hi = np.searchsorted(idx, k, "left"), np.searchsorted(idx, k, "right")
        if hi == lo:
            empty_run.append(k)
            continue
        if empty_run:
            flush_empty()
        emit(k, WindowData.from_columns({c: a[lo:hi] for c, a in arrays.items()}))
    if empty_run:
        flush_empty()
    return pd.DataFrame(rows, columns=cols)