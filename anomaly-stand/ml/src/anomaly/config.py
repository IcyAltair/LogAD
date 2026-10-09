"""Settings shared by the training pipeline and the online detector (env-driven).

Every field reads its environment variable when Settings() is created, so tests
can override values with dataclasses.replace(Settings(), ...).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default, cast=str):
    """Typed env lookup; unset or empty values fall back to the default."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return cast(raw)


def _f(name: str, default, cast=str):
    return field(default_factory=lambda: _env(name, default, cast))


@dataclass(frozen=True)
class Settings:
    # ---------------- data sources (bind mounts, see docker-compose.yml) ----------------
    log_file: Path = _f("NGINX_LOG_FILE", Path("/data/nginx-logs/access.json.log"), Path)
    ground_truth_file: Path = _f("GROUND_TRUTH_FILE", Path("/data/ground-truth/anomalies.jsonl"), Path)
    data_dir: Path = _f("ML_DATA_DIR", Path("/data/ml"), Path)

    # ---------------- feature engineering ----------------
    window_s: int = _f("WINDOW_S", 15, int)                  # aggregation window, seconds
    grace_s: float = _f("GRACE_S", 3.0, float)              # wait for late lines (nginx flush=1s)
    baseline_windows: int = _f("BASELINE_WINDOWS", 40, int)  # history for rps_rel (40 x 15s = 10 min)
    min_history: int = _f("MIN_HISTORY", 5, int)             # rps_rel = 1.0 until this many windows exist
    max_gap_windows: int = _f("MAX_GAP_WINDOWS", 8, int)     # offline: longer empty gaps = stand was down

    # ---------------- dataset ----------------
    lookback_hours: float = _f("TRAIN_LOOKBACK_HOURS", 0.0, float)  # 0 = whole log
    min_windows: int = _f("MIN_WINDOWS", 200, int)
    min_train_windows: int = _f("MIN_TRAIN_WINDOWS", 100, int)
    block_windows: int = _f("BLOCK_WINDOWS", 40, int)        # contiguous blocks for the split
    split_pattern: str = _f("SPLIT_PATTERN", "train,train,train,val,test")
    label_overlap: float = _f("LABEL_OVERLAP", 0.5, float)   # window = anomaly if >=50% inside GT

    # ---------------- model / model selection ----------------
    train_on_clean: bool = _f("TRAIN_ON_CLEAN", True, bool)  # drop GT anomalies from the train set
    n_estimators: int = _f("N_ESTIMATORS", 200, int)
    max_samples_grid: str = _f("MAX_SAMPLES_GRID", "128,256")
    max_features_grid: str = _f("MAX_FEATURES_GRID", "1.0,0.5")
    threshold_quantile: float = _f("THRESHOLD_QUANTILE", 0.995, float)        # no labels in val
    threshold_floor_quantile: float = _f("THRESHOLD_FLOOR_QUANTILE", 0.95, float)  # min threshold
    seed: int = _f("SEED", 42, int)

    # ---------------- alerting (k-of-n debounce) ----------------
    alert_k: int = _f("ALERT_K", 2, int)
    alert_n: int = _f("ALERT_N", 3, int)
    event_tolerance_s: float = _f("EVENT_TOLERANCE_S", 30.0, float)

    # ---------------- online detector ----------------
    auto_train: bool = _f("AUTO_TRAIN", True, bool)
    auto_train_min_span_s: float = _f("AUTO_TRAIN_MIN_SPAN_S", 3900.0, float)
    auto_train_check_s: float = _f("AUTO_TRAIN_CHECK_S", 60.0, float)
    auto_train_retry_s: float = _f("AUTO_TRAIN_RETRY_S", 600.0, float)
    model_reload_s: float = _f("MODEL_RELOAD_S", 15.0, float)
    prime_bytes: int = _f("PRIME_BYTES", 8_000_000, int)     # log tail read at start (rps_rel history)
    poll_interval_s: float = _f("POLL_INTERVAL_S", 0.5, float)
    recent_windows: int = _f("RECENT_WINDOWS", 480, int)      # kept for REST API (2 h)
    log_level: str = _f("LOG_LEVEL", "INFO")

    # ---------------- derived ----------------
    @property
    def datasets_dir(self) -> Path:
        return self.data_dir / "datasets"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def dataset_path(self) -> Path:
        return self.datasets_dir / "features.csv"

    @property
    def model_path(self) -> Path:
        return self.models_dir / "model.joblib"

    def grid(self) -> tuple[list[int], list[float]]:
        samples = [int(x) for x in self.max_samples_grid.split(",") if x.strip()]
        feats = [float(x) for x in self.max_features_grid.split(",") if x.strip()]
        return samples, feats

    def splits(self) -> list[str]:
        return [s.strip() for s in self.split_pattern.split(",") if s.strip()]