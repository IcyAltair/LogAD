"""
Isolation Forest model: sklearn pipeline + persisted bundle.

Pipeline: log1p(heavy-tailed features) -> IsolationForest
(Isolation Forest splits on raw thresholds, so it does not need a scaler, but
log1p spreads out the heavy-tailed latency and size features so the forest
splits on them more evenly.)

Anomaly score = -score_samples(x) = s(x, n) from the paper, in (0, 1]:
~0.5 ordinary point, closer to 1 = easier to isolate = more anomalous.

Explanations: IF has no per-sample attributions, so we report a robust z-score
of every (log-transformed) feature against the clean training data. A heuristic
"which log fields deviate", shown in Grafana next to every alert.
"""
from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer

from .features import FEATURES, LOG_FEATURES

LOG_IDX = [FEATURES.index(f) for f in LOG_FEATURES]
Z_CLIP = 20.0
INFO_LABELS = ["version", "trained_at", "train_mode", "threshold", "val_ap", "test_f1", "test_event_recall"]


def log_transform(X: np.ndarray) -> np.ndarray:
    """log1p on heavy-tailed columns (module-level function -> picklable)."""
    X = np.array(X, dtype=float, copy=True)
    X[:, LOG_IDX] = np.log1p(np.clip(X[:, LOG_IDX], 0, None))
    return X


def make_pipeline(n_estimators: int, max_samples: int, max_features: float, seed: int) -> Pipeline:
    return Pipeline([
        ("log1p", FunctionTransformer(log_transform)),
        ("iforest", IsolationForest(n_estimators=n_estimators, max_samples=max_samples,
                                    max_features=max_features, contamination="auto",
                                    random_state=seed, n_jobs=1)),
    ])


def anomaly_score(pipe: Pipeline, X: np.ndarray) -> np.ndarray:
    return -pipe.score_samples(np.asarray(X, dtype=float))


def explain_stats(pipe: Pipeline, X_clean: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Robust centre/scale of transformed clean training features (for z-scores)."""
    T = pipe[:-1].transform(np.asarray(X_clean, dtype=float))
    center = np.median(T, axis=0)
    q75, q25 = np.percentile(T, [75, 25], axis=0)
    scale = np.maximum.reduce([(q75 - q25) / 1.349, T.std(axis=0), np.full(T.shape[1], 1e-3)])
    return center, scale


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v:.3f}"


@dataclass
class ModelBundle:
    """Everything the detector needs to score exactly like the pipeline did."""
    pipeline: Pipeline
    threshold: float
    explain_center: np.ndarray
    explain_scale: np.ndarray
    version: str
    trained_at: str
    window_s: int
    baseline_windows: int
    min_history: int
    params: dict
    features: list = field(default_factory=lambda: list(FEATURES))
    metrics: dict = field(default_factory=dict)

    def score(self, X: np.ndarray) -> np.ndarray:
        return anomaly_score(self.pipeline, X)

    def zscores(self, X: np.ndarray) -> np.ndarray:
        T = self.pipeline[:-1].transform(np.asarray(X, dtype=float))
        return np.clip((T - self.explain_center) / self.explain_scale, -Z_CLIP, Z_CLIP)

    def info_labels(self) -> dict[str, str]:
        """Flat string labels for the nginx_anomaly_model_info metric."""
        val, test = self.metrics.get("val", {}), self.metrics.get("test", {})
        return {
            "version": self.version,
            "trained_at": self.trained_at,
            "train_mode": "clean" if self.params.get("train_on_clean") else "all",
            "threshold": f"{self.threshold:.4f}",
            "val_ap": _fmt(val.get("window", {}).get("average_precision")),
            "test_f1": _fmt(test.get("window", {}).get("f1")),
            "test_event_recall": _fmt(test.get("events", {}).get("recall")),
        }


def save_bundle(bundle: ModelBundle, models_dir: Path) -> Path:
    """Write models/model-<version>.joblib and atomically replace models/model.joblib."""
    models_dir = Path(models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    versioned = models_dir / f"model-{bundle.version}.joblib"
    joblib.dump(bundle, versioned)
    fd, tmp = tempfile.mkstemp(dir=models_dir, suffix=".tmp")
    os.close(fd)
    joblib.dump(bundle, tmp)
    os.replace(tmp, models_dir / "model.joblib")  # detector never sees a half-written file
    return versioned


def load_bundle(path: Path) -> ModelBundle:
    bundle = joblib.load(path)
    if not isinstance(bundle, ModelBundle):
        raise TypeError(f"{path} is not a ModelBundle")
    if list(bundle.features) != FEATURES:
        raise ValueError("Model was trained with a different feature set; retrain it")
    return bundle