"""
Online anomaly detector (FastAPI + Prometheus).

Loop (background thread):
  tail nginx JSON log -> bucket by $msec into WINDOW_S windows -> when a window is
  complete (+GRACE_S): features (same code as training) -> IsolationForest score
  -> raw flag (score >= threshold) -> k-of-n debounced alert -> Prometheus gauges.

Model lifecycle: hot reload when data/ml/models/model.joblib changes; with
AUTO_TRAIN=true the first model is trained automatically (pipeline subprocess)
once the log covers AUTO_TRAIN_MIN_SPAN_S.

Endpoints: /health, /metrics, /api/status, /api/windows, /api/alerts,
           POST /api/reload, POST /api/train
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from .config import Settings
from .features import FEATURES, FeatureBuilder, WindowAggregator, WindowData
from .logs import LogTailer, log_time_span, parse_line, read_tail_records
from .model import INFO_LABELS, ModelBundle, load_bundle

cfg = Settings()
logging.basicConfig(level=cfg.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ml-detector")

# ------------------------------------------------------------------ Prometheus metrics
SCORE = Gauge("nginx_anomaly_score", "Isolation Forest score of the last complete window (higher = more anomalous)")
THRESHOLD = Gauge("nginx_anomaly_threshold", "Decision threshold of the loaded model")
IS_ANOMALY = Gauge("nginx_anomaly_is_anomaly", "1 if the last window score >= threshold (raw, not debounced)")
ALERT = Gauge("nginx_anomaly_alert", "1 while the k-of-n debounced alert is active")
FEATURE_VALUE = Gauge("nginx_anomaly_feature_value", "Feature value of the last window", ["feature"])
FEATURE_Z = Gauge("nginx_anomaly_feature_zscore", "Robust z-score of the feature vs clean training data", ["feature"])
WINDOW_REQUESTS = Gauge("nginx_anomaly_window_requests", "Log lines in the last complete window")
LAST_WINDOW = Gauge("nginx_anomaly_last_window_timestamp_seconds", "End time of the last processed window")
WINDOWS = Counter("nginx_anomaly_windows", "Complete windows processed")
WINDOWS_ANOM = Counter("nginx_anomaly_windows_anomalous", "Windows with score >= threshold")
ALERTS = Counter("nginx_anomaly_alerts", "Alerts raised (rising edges)")
LINES = Counter("nginx_anomaly_log_lines", "Parsed log lines")
PARSE_ERRORS = Counter("nginx_anomaly_parse_errors", "Unparseable log lines")
LATE_LINES = Counter("nginx_anomaly_late_lines", "Lines that arrived after their window was scored")
MODEL_LOADED = Gauge("nginx_anomaly_model_loaded", "1 if a model is loaded")
MODEL_INFO = Gauge("nginx_anomaly_model_info", "Loaded model metadata (value is always 1)", INFO_LABELS)
TRAINING = Gauge("nginx_anomaly_training_in_progress", "1 while a training pipeline subprocess runs")


class Detector:
    def __init__(self, settings: Settings) -> None:
        self.cfg = settings
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.bundle: ModelBundle | None = None
        self._model_mtime: float | None = None
        self.tailer = LogTailer(settings.log_file, start_at_end=True)
        self.builder = FeatureBuilder(settings.window_s, settings.baseline_windows, settings.min_history)
        self.agg: WindowAggregator | None = None
        self.flags: deque[int] = deque(maxlen=settings.alert_n)
        self.recent: deque[dict] = deque(maxlen=settings.recent_windows)
        self.alerts: deque[dict] = deque(maxlen=200)
        self.open_alert: dict | None = None
        self._late_seen = 0
        self._proc: subprocess.Popen | None = None
        self._next_train_try = 0.0
        self.last_train: dict | None = None
        MODEL_LOADED.set(0)
        ALERT.set(0)

    # ---------------------------------------------------------- model lifecycle
    def maybe_reload(self, force: bool = False) -> bool:
        path = self.cfg.model_path
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return False
        if not force and mtime == self._model_mtime:
            return False
        self._model_mtime = mtime
        try:
            bundle = load_bundle(path)
        except Exception as exc:  # corrupt / incompatible model: keep the old one
            log.error("Cannot load model %s: %r", path, exc)
            return False
        if bundle.window_s != self.cfg.window_s:
            log.error("Model window_s=%s != WINDOW_S=%s: retrain with the current settings",
                      bundle.window_s, self.cfg.window_s)
            return False
        with self.lock:
            self.bundle = bundle
            self.flags.clear()
        MODEL_LOADED.set(1)
        THRESHOLD.set(bundle.threshold)
        MODEL_INFO.clear()
        MODEL_INFO.labels(**bundle.info_labels()).set(1)
        log.info("Loaded model %s (threshold %.4f)", bundle.version, bundle.threshold)
        return True

    def start_training(self, reason: str) -> bool:
        if self._proc is not None:
            return False
        log.warning("Starting training pipeline (%s)", reason)
        self._proc = subprocess.Popen([sys.executable, "-m", "anomaly.pipeline", "run"])
        self.last_train = {"reason": reason, "started_at": time.time(), "returncode": None}
        TRAINING.set(1)
        return True

    def _check_training(self) -> None:
        if self._proc is None or self._proc.poll() is None:
            return
        rc = self._proc.returncode
        self._proc = None
        TRAINING.set(0)
        self.last_train["returncode"] = rc
        if rc != 0:
            self._next_train_try = time.time() + self.cfg.auto_train_retry_s
            log.error("Training failed (rc=%s), next auto attempt in %.0fs", rc, self.cfg.auto_train_retry_s)
        self.maybe_reload()

    def _maybe_auto_train(self) -> None:
        if not self.cfg.auto_train or self.bundle is not None or self._proc is not None:
            return
        if time.time() < self._next_train_try:
            return
        span = log_time_span(self.cfg.log_file)
        if span >= self.cfg.auto_train_min_span_s:
            self.start_training(f"auto: log span {span:.0f}s")
        else:
            log.info("No model yet: log span %.0fs < %.0fs, waiting for more data",
                     span, self.cfg.auto_train_min_span_s)
            self._next_train_try = time.time() + self.cfg.auto_train_check_s

    # ---------------------------------------------------------- streaming
    def _prime(self) -> None:
        """Seed rps_rel history from the log tail, so scores are valid right after start."""
        W = self.cfg.window_s
        now = time.time()
        records = read_tail_records(self.cfg.log_file, self.cfg.prime_bytes)
        self.tailer.read_lines()                      # open the log at its current end
        self.agg = WindowAggregator(W, self.cfg.grace_s, now)
        if not records:
            return
        counts: dict[int, int] = {}
        for r in records:
            k = int(r["ts"] // W)
            counts[k] = counts.get(k, 0) + 1
        cur = int(now // W)
        first = min(counts) + 1                       # first window of the tail may be partial
        ks = range(max(first, cur - self.cfg.baseline_windows), cur)
        self.builder.prime(counts.get(k, 0) for k in ks)
        log.info("Primed rps history with %d windows", len(ks))

    def _process_window(self, ws: float, records: list[dict]) -> None:
        W = self.cfg.window_s
        w = WindowData.from_records(records)
        feats = self.builder.push(w)
        WINDOWS.inc()
        WINDOW_REQUESTS.set(w.n)
        LAST_WINDOW.set(ws + W)
        for name in FEATURES:
            FEATURE_VALUE.labels(name).set(feats[name])
        row = {"window_start": ws, "window_end": ws + W, "n_requests": w.n,
               "features": {k: round(v, 4) for k, v in feats.items()}}
        with self.lock:
            bundle = self.bundle
            if bundle is not None:
                x = np.array([[feats[f] for f in FEATURES]], dtype=float)
                score = float(bundle.score(x)[0])
                z = bundle.zscores(x)[0]
                flag = int(score >= bundle.threshold)
                self.flags.append(flag)
                alert = sum(self.flags) >= self.cfg.alert_k
                top = [{"feature": FEATURES[i], "z": round(float(z[i]), 2)} for i in np.argsort(-np.abs(z))[:3]]

                SCORE.set(score)
                IS_ANOMALY.set(flag)
                ALERT.set(int(alert))
                if flag:
                    WINDOWS_ANOM.inc()
                for i, name in enumerate(FEATURES):
                    FEATURE_Z.labels(name).set(float(z[i]))

                # alert intervals for the REST API
                if alert and self.open_alert is None:
                    ALERTS.inc()
                    self.open_alert = {"start": ws, "end": ws + W, "max_score": score,
                                       "top_features": top, "ongoing": True}
                    self.alerts.append(self.open_alert)
                    log.warning("ALERT start score=%.3f top=%s", score, top)
                elif alert:
                    self.open_alert["end"] = ws + W
                    if score > self.open