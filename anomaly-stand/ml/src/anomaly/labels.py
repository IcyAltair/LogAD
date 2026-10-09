"""
Ground truth -> window labels.

The step-1 traffic-generator appends every injected scenario to
data/ground-truth/anomalies.jsonl ({"scenario", "start_ts", "end_ts", ...}).

Label per window:
   1  >= LABEL_OVERLAP of the window lies inside a GT interval
  -1  partial overlap (boundary window): ambiguous, excluded from training & metrics
   0  normal
Manual chaos from the UI is NOT in ground truth -> it shows up as "false alarms".
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


def load_ground_truth(path: Path) -> pd.DataFrame:
    cols = ["scenario", "start_ts", "end_ts"]
    path = Path(path)
    if not path.exists():
        log.warning("Ground truth %s not found: all windows are treated as normal", path)
        return pd.DataFrame(columns=cols)
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
                rows.append({"scenario": str(r["scenario"]), "start_ts": float(r["start_ts"]),
                             "end_ts": float(r["end_ts"])})
            except (ValueError, KeyError, TypeError):
                continue
    return pd.DataFrame(rows, columns=cols).sort_values("start_ts").reset_index(drop=True)


def label_windows(window_start: np.ndarray, window_s: int, gt: pd.DataFrame,
                  min_overlap: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (labels in {-1,0,1}, scenario name or '' per window)."""
    ws = np.asarray(window_start, dtype=float)
    best = np.zeros(ws.shape[0])
    scenario = np.full(ws.shape[0], "", dtype=object)
    for row in gt.itertuples(index=False):
        overlap = np.clip(np.minimum(row.end_ts, ws + window_s) - np.maximum(row.start_ts, ws), 0, None) / window_s
        better = overlap > best
        best[better] = overlap[better]
        scenario[better] = row.scenario
    labels = np.where(best >= min_overlap, 1, np.where(best > 0, -1, 0)).astype(int)
    return labels, scenario