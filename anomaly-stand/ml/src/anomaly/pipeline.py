"""
ML pipeline CLI:  python -m anomaly.pipeline {prepare|train|test|run}

  prepare  nginx JSON log -> 15s windows -> features + GT labels + blocked split
           -> data/ml/datasets/features.csv (+ summary.json)
  train    grid over IsolationForest hyper-parameters, fit on (clean) train windows,
           choose params by validation average precision, threshold = best val F1
           (never below a quantile of clean train scores) -> data/ml/models/model.joblib
  test     score all windows, window + event metrics per split (k-of-n alert rule
           identical to the online detector) -> data/ml/reports/{latest.json,latest.md,scores.png}
  run      all three

Split: contiguous blocks of BLOCK_WINDOWS windows assigned cyclically by
SPLIT_PATTERN (default train,train,train,val,test). Every split covers all phases
of the simulated day, and neighbouring windows rarely leak across splits.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score

from .config import Settings
from .evaluation import best_f1_threshold, evaluate_split
from .features import FEATURES, build_feature_frame
from .labels import label_windows, load_ground_truth
from .logs import load_logs
from .model import ModelBundle, anomaly_score, explain_stats, load_bundle, make_pipeline, save_bundle

log = logging.getLogger("anomaly.pipeline")
SPLITS = ("train", "val", "test")


class PipelineError(RuntimeError):
    """Expected failure (not enough data etc.): logged without a traceback."""


# --------------------------------------------------------------------------- helpers
def _clean(o):
    """Make an object strict-JSON serialisable (numpy types, NaN -> null)."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.generic):
        o = o.item()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o


def _write_json(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_clean(obj), indent=2), encoding="utf-8")


def assign_splits(n: int, block: int, pattern: list[str]) -> np.ndarray:
    return np.array([pattern[(i // block) % len(pattern)] for i in range(n)], dtype=object)


def dataset_summary(frame: pd.DataFrame) -> dict:
    out = {
        "windows": int(len(frame)),
        "span_hours": float((frame["window_start"].max() - frame["window_start"].min()) / 3600) if len(frame) else 0.0,
        "splits": {},
        "anomalous_windows_by_scenario": frame.loc[frame["label"] == 1, "scenario"].value_counts().to_dict(),
    }
    for sp in SPLITS:
        m = frame["split"] == sp
        out["splits"][sp] = {"windows": int(m.sum()),
                             "anomalous": int((m & (frame["label"] == 1)).sum()),
                             "ambiguous": int((m & (frame["label"] == -1)).sum())}
    return out


def load_dataset(cfg: Settings) -> pd.DataFrame:
    if not cfg.dataset_path.exists():
        raise PipelineError(f"{cfg.dataset_path} not found: run `prepare` first")
    frame = pd.read_csv(cfg.dataset_path)
    frame["scenario"] = frame["scenario"].fillna("").astype(str)
    return frame


# --------------------------------------------------------------------------- steps
def prepare(cfg: Settings) -> pd.DataFrame:
    since = time.time() - cfg.lookback_hours * 3600 if cfg.lookback_hours > 0 else None
    df, bad = load_logs(cfg.log_file, since)
    log.info("Loaded %d log records (%d malformed) from %s", len(df), bad, cfg.log_file)

    frame = build_feature_frame(df, cfg.window_s, cfg.baseline_windows, cfg.min_history, cfg.max_gap_windows)
    if len(frame) < cfg.min_windows:
        raise PipelineError(f"Only {len(frame)} complete {cfg.window_s}s windows, need >= {cfg.min_windows}. "
                            "Let the traffic generator run longer.")

    gt = load_ground_truth(cfg.ground_truth_file)
    labels, scenario = label_windows(frame["window_start"].to_numpy(), cfg.window_s, gt, cfg.label_overlap)
    frame["label"], frame["scenario"] = labels, scenario
    frame["split"] = assign_splits(len(frame), cfg.block_windows, cfg.splits())

    cfg.datasets_dir.mkdir(parents=True, exist_ok=True)
    frame.to_csv(cfg.dataset_path, index=False)
    summary = dataset_summary(frame) | {"log_records": int(len(df)), "malformed_lines": int(bad),
                                        "gt_events_in_file": int(len(gt))}
    _write_json(cfg.datasets_dir / "summary.json", summary)
    log.info("Dataset: %d windows (%.1f h), splits=%s", summary["windows"], summary["span_hours"], summary["splits"])
    return frame


def train(cfg: Settings, frame: pd.DataFrame | None = None) -> ModelBundle:
    frame = load_dataset(cfg) if frame is None else frame
    X = frame[FEATURES].to_numpy(dtype=float)
    y = frame["label"].to_numpy(dtype=int)
    split = frame["split"].to_numpy()

    tr = split == "train"
    tr &= (y == 0) if cfg.train_on_clean else (y >= 0)
    if tr.sum() < cfg.min_train_windows:
        raise PipelineError(f"Only {int(tr.sum())} training windows, need >= {cfg.min_train_windows}")
    va = split == "val"
    va_lab = va & (y >= 0)
    has_val_labels = va_lab.any() and len(np.unique(y[va_lab])) == 2
    if not has_val_labels:
        log.warning("Validation split has no labelled anomalies: threshold = q%.3f of train scores",
                    cfg.threshold_quantile)

    samples_grid, features_grid = cfg.grid()
    candidates = []
    for ms in samples_grid:
        for mf in features_grid:
            pipe = make_pipeline(cfg.n_estimators, min(ms, int(tr.sum())), mf, cfg.seed).fit(X[tr])
            s_tr = anomaly_score(pipe, X[tr])
            floor = float(np.quantile(s_tr, cfg.threshold_floor_quantile))
            if has_val_labels:
                s_va = anomaly_score(pipe, X[va_lab])
                ap = float(average_precision_score(y[va_lab], s_va))
                thr, _ = best_f1_threshold(y[va_lab], s_va)
                thr, source = max(thr, floor), "val_best_f1"
            else:
                ap = float("nan")
                thr, source = float(np.quantile(s_tr, cfg.threshold_quantile)), "train_quantile"
            candidates.append({"pipe": pipe, "max_samples": ms, "max_features": mf, "val_ap": ap,
                               "threshold": thr, "threshold_source": source})
            log.info("  max_samples=%-4s max_features=%-4s val_AP=%.3f thr=%.4f", ms, mf, ap, thr)

    best = max(candidates, key=lambda c: -1.0 if math.isnan(c["val_ap"]) else c["val_ap"])
    center, scale = explain_stats(best["pipe"], X[tr])
    now = datetime.now(timezone.utc)
    bundle = ModelBundle(
        pipeline=best["pipe"], threshold=best["threshold"],
        explain_center=center, explain_scale=scale,
        version=now.strftime("%Y%m%d-%H%M%S"), trained_at=now.isoformat(timespec="seconds"),
        window_s=cfg.window_s, baseline_windows=cfg.baseline_windows, min_history=cfg.min_history,
        params={"n_estimators": cfg.n_estimators, "max_samples": best["max_samples"],
                "max_features": best["max_features"], "train_on_clean": cfg.train_on_clean,
                "train_windows": int(tr.sum()), "threshold_source": best["threshold_source"],
                "alert_rule": f"{cfg.alert_k}-of-{cfg.alert_n}"},
    )
    if va.any():
        bundle.metrics["val"] = evaluate_split(
            frame["window_start"].to_numpy()[va], y[va], frame["scenario"].to_numpy()[va],
            bundle.score(X[va]), bundle.threshold, cfg.window_s, cfg.alert_k, cfg.alert_n, cfg.event_tolerance_s)
    bundle.metrics["grid"] = [{k: v for k, v in c.items() if k != "pipe"} for c in candidates]
    path = save_bundle(bundle, cfg.models_dir)
    log.info("Model %s saved to %s (threshold %.4f, %s)", bundle.version, path, bundle.threshold,
             best["threshold_source"])
    return bundle


def _fmt(v, d=3) -> str:
    return "–" if v is None or (isinstance(v, float) and not math.isfinite(v)) else f"{v:.{d}f}"


def _markdown(report: dict) -> str:
    lines = [f"# Isolation Forest report `{report['version']}`", "",
             f"threshold **{report['threshold']:.4f}** ({report['params']['threshold_source']}), "
             f"alert rule {report['params']['alert_rule']}, params: "
             f"max_samples={report['params']['max_samples']}, max_features={report['params']['max_features']}", "",
             "| split | windows | anomalous | precision | recall | F1 | ROC-AUC | AP | GT incidents | detected "
             "| false alerts/h | mean delay, s |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for sp, r in report["results"].items():
        w, e = r["window"], r["events"]
        lines.append(f"| {sp} | {w['windows']} | {w['positives']} | {_fmt(w['precision'])} | {_fmt(w['recall'])} "
                     f"| {_fmt(w['f1'])} | {_fmt(w['roc_auc'])} | {_fmt(w['average_precision'])} | {e['gt_events']} "
                     f"| {e['detected']} | {_fmt(e['false_alerts_per_hour'], 2)} | {_fmt(e['mean_delay_s'], 0)} |")
    test = report["results"].get("test", {}).get("events", {}).get("per_scenario", {})
    if test:
        lines += ["", "## Test split per scenario", "", "| scenario | incidents | detected | mean delay, s |",
                  "|---|---|---|---|"]
        for name, ps in sorted(test.items()):
            lines.append(f"| {name} | {ps['events']} | {ps['detected']} | {_fmt(ps['mean_delay_s'], 0)} |")
    return "\n".join(lines) + "\n"


def _plot(frame: pd.DataFrame, scores: np.ndarray, threshold: float, path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    t = pd.to_datetime(frame["window_start"], unit="s")
    fig, ax = plt.subplots(figsize=(16, 4.5))
    ax.fill_between(t, 0, 1, where=frame["label"].to_numpy() == 1, step="post", color="purple", alpha=0.25,
                    transform=ax.get_xaxis_transform(), label="ground truth")
    ax.fill_between(t, 0, 1, where=frame["split"].to_numpy() == "test", step="post", color="grey", alpha=0.08,
                    transform=ax.get_xaxis_transform(), label="test split")
    ax.plot(t, scores, lw=0.7, label="anomaly score")
    ax.axhline(threshold, color="red", ls="--", lw=1, label=f"threshold {threshold:.3f}")
    ax.set_ylabel("IF score")
    ax.legend(loc="upper left", ncol=4, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def test(cfg: Settings, frame: pd.DataFrame | None = None, bundle: ModelBundle | None = None) -> dict:
    frame = load_dataset(cfg) if frame is None else frame
    bundle = load_bundle(cfg.model_path) if bundle is None else bundle
    X = frame[FEATURES].to_numpy(dtype=float)
    scores = bundle.score(X)
    ws, y = frame["window_start"].to_numpy(), frame["label"].to_numpy(dtype=int)
    scen, split = frame["scenario"].to_numpy(), frame["split"].to_numpy()

    results = {}
    for sp in SPLITS:
        m = split == sp
        if m.any():
            results[sp] = evaluate_split(ws[m], y[m], scen[m], scores[m], bundle.threshold, cfg.window_s,
                                         cfg.alert_k, cfg.alert_n, cfg.event_tolerance_s)
    bundle.metrics["test"] = results.get("test", {})
    save_bundle(bundle, cfg.models_dir)  # detector picks up test metrics for model_info

    scored = frame.assign(score=scores, flag=(scores >= bundle.threshold).astype(int))
    scored.to_csv(cfg.datasets_dir / "scored.csv", index=False)
    report = {"version": bundle.version, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "threshold": bundle.threshold, "params": bundle.params,
              "dataset": dataset_summary(frame), "results": results}
    _write_json(cfg.reports_dir / f"report-{bundle.version}.json", report)
    _write_json(cfg.reports_dir / "latest.json", report)
    md = _markdown(_clean(report) | {"threshold": bundle.threshold})
    (cfg.reports_dir / "latest.md").write_text(md, encoding="utf-8")
    _plot(frame, scores, bundle.threshold, cfg.reports_dir / "scores.png")
    print(md)
    return report


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m anomaly.pipeline", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=["prepare", "train", "test", "run"])
    args = parser.parse_args(argv)
    cfg = Settings()
    logging.basicConfig(level=cfg.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.step == "prepare":
            prepare(cfg)
        elif args.step == "train":
            train(cfg)
        elif args.step == "test":
            test(cfg)
        else:
            frame = prepare(cfg)
            bundle = train(cfg, frame)
            test(cfg, frame, bundle)
    except (PipelineError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())