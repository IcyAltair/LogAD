"""
Evaluation helpers. Two views of quality:
  window level  precision / recall / F1 / ROC-AUC / average precision per window
  event level   what an on-call engineer sees: k-of-n debounced alerts vs GT incidents
                (detected / missed incidents, false alerts per hour, detection delay)
"""
from __future__ import annotations

import numpy as np
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score


def window_metrics(y: np.ndarray, scores: np.ndarray, threshold: float) -> dict:
    y, scores = np.asarray(y), np.asarray(scores)
    mask = y >= 0  # drop ambiguous (-1) windows
    y, scores = y[mask], scores[mask]
    pred = scores >= threshold
    tp = int(np.sum(pred & (y == 1)))
    fp = int(np.sum(pred & (y == 0)))
    fn = int(np.sum(~pred & (y == 1)))
    tn = int(np.sum(~pred & (y == 0)))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else float("nan")
    if np.isnan(recall) or precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    both = len(np.unique(y)) == 2
    return {
        "windows": int(mask.sum()), "positives": int(np.sum(y == 1)),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "recall": recall, "f1": f1,
        "fpr": fp / (fp + tn) if fp + tn else 0.0,
        "roc_auc": float(roc_auc_score(y, scores)) if both else float("nan"),
        "average_precision": float(average_precision_score(y, scores)) if both else float("nan"),
    }


def best_f1_threshold(y: np.ndarray, scores: np.ndarray) -> tuple[float, float]:
    """Threshold maximising window-level F1 (rule: score >= threshold)."""
    y, scores = np.asarray(y), np.asarray(scores)
    mask = y >= 0
    p, r, t = precision_recall_curve(y[mask], scores[mask])
    p, r = p[:-1], r[:-1]  # last point has no threshold
    f1 = np.where(p + r > 0, 2 * p * r / np.maximum(p + r, 1e-12), 0.0)
    i = int(np.argmax(f1))
    return float(t[i]), float(f1[i])


def segment_ids(window_start: np.ndarray, window_s: int) -> np.ndarray:
    """Consecutive windows share an id; a gap starts a new segment (blocked splits, downtime)."""
    ws = np.asarray(window_start, dtype=float)
    if ws.size == 0:
        return np.array([], dtype=int)
    breaks = np.diff(ws) > window_s * 1.5
    return np.concatenate([[0], np.cumsum(breaks)]).astype(int)


def k_of_n(flags: np.ndarray, k: int, n: int, segments: np.ndarray | None = None) -> np.ndarray:
    """alert[t] = at least k of the last n window flags are set (within one segment).
    Identical to the online rule in detector.py."""
    flags = np.asarray(flags, dtype=int)
    segments = np.zeros_like(flags) if segments is None else np.asarray(segments)
    out = np.zeros_like(flags)
    for seg in np.unique(segments):
        idx = np.where(segments == seg)[0]
        f = flags[idx]
        csum = np.concatenate([[0], np.cumsum(f)])
        lo = np.maximum(np.arange(1, f.size + 1) - n, 0)
        out[idx] = (csum[1:] - csum[lo]) >= k
    return out


def intervals_from_flags(window_start: np.ndarray, flags: np.ndarray, window_s: int,
                         segments: np.ndarray) -> list[tuple[float, float]]:
    """Merge consecutive flagged windows into [start, end) intervals."""
    out, cur = [], None
    for t, f, s in zip(np.asarray(window_start, dtype=float), np.asarray(flags), np.asarray(segments)):
        if f:
            if cur is not None and cur[2] == s and t <= cur[1] + 1e-6:
                cur[1] = t + window_s
            else:
                if cur is not None:
                    out.append((cur[0], cur[1]))
                cur = [t, t + window_s, s]
        elif cur is not None:
            out.append((cur[0], cur[1]))
            cur = None
    if cur is not None:
        out.append((cur[0], cur[1]))
    return out


def event_metrics(alerts: list[tuple[float, float]], gt_events: list[tuple[str, float, float]],
                  tolerance_s: float, hours: float) -> dict:
    """
    GT incident detected = any alert overlaps [start, end + tolerance].
    False alert          = alert overlapping no incident (tolerance on both sides).
    Delay                = first overlapping alert start - incident start (>= 0).
    """
    detected, delays, per_scenario = 0, [], {}
    for name, s, e in gt_events:
        hits = [a for a in alerts if a[0] <= e + tolerance_s and a[1] >= s]
        ps = per_scenario.setdefault(name, {"events": 0, "detected": 0, "delays_s": []})
        ps["events"] += 1
        if hits:
            detected += 1
            ps["detected"] += 1
            delay = max(0.0, min(a[0] for a in hits) - s)
            delays.append(delay)
            ps["delays_s"].append(delay)
    false_alerts = sum(
        1 for a in alerts
        if not any(a[0] <= e + tolerance_s and a[1] >= s - tolerance_s for _, s, e in gt_events)
    )
    for ps in per_scenario.values():
        ps["recall"] = ps["detected"] / ps["events"]
        ps["mean_delay_s"] = float(np.mean(ps["delays_s"])) if ps["delays_s"] else None
        del ps["delays_s"]
    n_alerts = len(alerts)
    return {
        "gt_events": len(gt_events), "detected": detected, "missed": len(gt_events) - detected,
        "recall": detected / len(gt_events) if gt_events else float("nan"),
        "alerts": n_alerts, "false_alerts": false_alerts,
        "precision": (n_alerts - false_alerts) / n_alerts if n_alerts else float("nan"),
        "false_alerts_per_hour": false_alerts / hours if hours > 0 else float("nan"),
        "mean_delay_s": float(np.mean(delays)) if delays else None,
        "per_scenario": per_scenario,
    }


def evaluate_split(window_start: np.ndarray, y: np.ndarray, scenario: np.ndarray, scores: np.ndarray,
                   threshold: float, window_s: int, k: int, n: int, tolerance_s: float) -> dict:
    """Window + event metrics for one split (its windows may be non-contiguous)."""
    ws = np.asarray(window_start, dtype=float)
    y = np.asarray(y)
    scenario = np.asarray(scenario, dtype=object)
    seg = segment_ids(ws, window_s)
    flags = (np.asarray(scores) >= threshold).astype(int)
    alerts = intervals_from_flags(ws, k_of_n(flags, k, n, seg), window_s, seg)
    gt_flags = y == 1
    gt_events = []
    for s, e in intervals_from_flags(ws, gt_flags, window_s, seg):
        names = [scenario[i] for i in np.where((ws >= s) & (ws < e) & gt_flags)[0]]
        gt_events.append((max(set(names), key=names.count) if names else "unknown", s, e))
    hours = ws.size * window_s / 3600.0
    return {
        "window": window_metrics(y, scores, threshold),
        "events": event_metrics(alerts, gt_events, tolerance_s, hours),
        "hours": hours,
    }