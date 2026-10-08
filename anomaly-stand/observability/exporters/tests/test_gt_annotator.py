import json
from prometheus_client import CollectorRegistry

import gt_annotator as gt

REC = {"scenario": "error_burst", "start": "2026-10-08T12:10:00+00:00", "end": "2026-10-08T12:12:30+00:00",
       "start_ts": 1791461400.0, "end_ts": 1791461550.0, "duration_s": 150,
       "params": {"error_rate": 0.35, "error_codes": [500, 503]}}


def test_parse_window_epoch_and_iso():
    w = gt.parse_window(json.dumps(REC))
    assert (w.scenario, w.start_ts, w.end_ts) == ("error_burst", 1791461400.0, 1791461550.0)
    assert w.gtid == "gtid:error_burst-1791461400"
    iso_only = {k: v for k, v in REC.items() if not k.endswith("_ts")}
    assert gt.parse_window(json.dumps(iso_only)) == w
    no_end = {"scenario": "traffic_spike", "start_ts": 100, "duration_s": 60}
    assert gt.parse_window(json.dumps(no_end)).end_ts == 160


def test_parse_window_bad():
    assert gt.parse_window("oops") is None
    assert gt.parse_window(json.dumps({"start_ts": 1})) is None


def test_jsonl_reader_incremental_and_reset(tmp_path):
    p = tmp_path / "a.jsonl"
    r = gt.JsonlReader(str(p))
    assert r.read_new() == ([], False)               # file missing
    p.write_text('{"a":1}\n{"b"')
    assert r.read_new() == (['{"a":1}'], False)
    with open(p, "a") as f:
        f.write(':2}\n')
    assert r.read_new() == (['{"b":2}'], False)
    p.write_text('{"c":3}\n')                        # shrink -> reset
    assert r.read_new() == (['{"c":3}'], True)


def test_update_gauges():
    reg = CollectorRegistry()
    m = gt.GTMetrics(reg)
    w = gt.parse_window(json.dumps(REC))
    gt.update_gauges(m, [w], now=1791461500.0)
    assert reg.get_sample_value("stand_ground_truth_anomaly_active", {"scenario": "error_burst"}) == 1
    assert reg.get_sample_value("stand_ground_truth_anomaly_active", {"scenario": "traffic_spike"}) == 0
    gt.update_gauges(m, [w], now=1791461550.0)       # end is exclusive
    assert reg.get_sample_value("stand_ground_truth_anomaly_active", {"scenario": "error_burst"}) == 0
    assert reg.get_sample_value("stand_ground_truth_windows", {"scenario": "error_burst"}) == 1
