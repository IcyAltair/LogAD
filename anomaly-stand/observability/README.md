# Anomaly Detection Stand — Step 2: Observability layer

Prometheus + Grafana + exporters that watch the business layer from step 1
(nginx + FastAPI + traffic-generator). **Non-PROD stand**: default credentials,
anonymous Viewer access, no TLS.

## Architecture

```
            anomaly-stand-net (external, created by step 1)
 ┌─────────────── step 1 ───────────────┐   ┌──────────────── step 2 ─────────────────┐
 │ traffic-generator ─► nginx :80 ─► app:8000/metrics ◄────────── prometheus :9090      │
 │                      │  :8081/stub_status ◄── nginx-exporter :9113 ◄──┤              │
 │                      ▼                                                 │              │
 │        data/nginx-logs/access.json.log ──► nginx-log-exporter :9102 ◄──┤              │
 │        data/ground-truth/anomalies.jsonl ─► gt-annotator :9103 ◄───────┘              │
 └──────────────────────────────────────┘          │ annotations API        ▲ PromQL    │
                                                   └──────────► grafana :3000 ┘          │
                                                   └─────────────────────────────────────┘
```

| Service | Purpose |
|---|---|
| `prometheus` | TSDB (5 s scrape), recording rules = features, baseline alerts |
| `grafana` | Provisioned datasource + 2 dashboards, plugin dir for step 3 |
| `nginx-exporter` | Official exporter for nginx `stub_status` (connections, total requests) |
| `nginx-log-exporter` | **Python.** Tails the JSON access log → status/route/latency/bytes metrics |
| `gt-annotator` | **Python.** `anomalies.jsonl` → Grafana region annotations + `stand_ground_truth_*` metrics |

Why a log exporter: `stub_status` only knows connections. All five anomaly
scenarios (`traffic_spike`, `latency_degradation`, `error_burst`,
`not_found_storm`, `heavy_payload`) are visible only in the access log
(`status`, `request_time`, `body_bytes_sent`). These are the same signals the Isolation Forest will use in step 3.

## Layout

```
observability/
├── docker-compose.yml          # separate compose project, joins anomaly-stand-net
├── .env                        # versions, ports, credentials, exporter settings
├── Makefile                    # up/down/reload/check/test helpers
├── exporters/
│   ├── Dockerfile              # one image for both exporters
│   ├── nginx_log_exporter.py   # log tailer (rotation/truncation safe, cardinality guard)
│   ├── gt_annotator.py         # ground truth -> Grafana annotations (idempotent)
│   ├── requirements*.txt
│   └── tests/                  # pytest, 18 tests
├── prometheus/
│   ├── prometheus.yml          # scrape configs + file_sd extension point
│   ├── rules/recording.yml     # stand:* features (rate, error ratios, p50/95/99, size)
│   ├── rules/alerts.yml        # health + baseline detectors (one per scenario)
│   ├── tests/rules_test.yml    # promtool unit tests
│   └── targets/                # step 3: drop ml-detector.yml here
└── grafana/
    ├── provisioning/{datasources,dashboards}/
    ├── dashboards/stand/{business-overview,stand-health}.json
    └── plugins/                # step 3: custom anomaly panel plugin
```

The exporters mount `../data/...` read-only.

## Quick start

```bash
# 1. business layer must be running (creates network anomaly-stand-net and ./data)
docker compose up -d --build

# 2. observability layer
cd observability
docker compose up -d --build        # or: make up
docker compose ps                   # all services healthy in about 30 s
```

| URL | What |
|---|---|
| http://localhost:3000 | Grafana (`admin`/`admin`; anonymous users get Viewer). Home = *Business overview* |
| http://localhost:9090/targets | Prometheus targets: `app`, `nginx`, `nginx-logs`, `ground-truth`, `grafana`, `prometheus` should be **UP** |
| http://localhost:9090/alerts | Health + baseline alerts |

Smoke checks:

```bash
curl -s 'localhost:9090/api/v1/query?query=stand:requests:rate1m' | jq '.data.result[0].value'   # about 8 rps
curl -s 'localhost:9090/api/v1/query?query=stand_ground_truth_anomaly_active' | jq '.data.result[] | [.metric.scenario, .value[1]]'
docker compose exec nginx-log-exporter python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:9102/metrics').read().decode()[:800])"
```

## Metrics

**nginx-log-exporter** (`job="nginx-logs"`)

| Metric | Type | Labels |
|---|---|---|
| `nginx_log_requests_total` | counter | `method, route, status, status_class` |
| `nginx_log_request_duration_seconds` | histogram (5 ms … 10 s) | `route` |
| `nginx_log_response_size_bytes` | histogram (256 B … 16 MiB) | `route` |
| `nginx_log_lines_total`, `nginx_log_parse_errors_total`, `nginx_log_file_reopens_total` | counter | — |
| `nginx_log_last_event_timestamp_seconds`, `nginx_log_lag_seconds`, `nginx_log_routes_tracked` | gauge | — |

Routes are normalized (`/api/products/17` → `/api/products/:id`). A 404 for a path that was never
seen with a non-404 status becomes `__unknown__`. This way `not_found_storm` does not cause a label
explosion. After `MAX_ROUTES` distinct routes, new ones go to `__other__`.

Log field names are detected automatically (`msec`/`time_iso8601`, `status`,
`request_method`, `uri`/`request_uri`, `request_time`, `body_bytes_sent`).
If your `log_format` uses other names, override them with
`FIELD_TIME`, `FIELD_STATUS`, `FIELD_METHOD`, `FIELD_URI`, `FIELD_REQUEST_TIME`, `FIELD_BYTES`
(comma-separated candidates) in the compose `environment`.

**gt-annotator** (`job="ground-truth"`)

| Metric | Meaning |
|---|---|
| `stand_ground_truth_anomaly_active{scenario}` | 1 while an injected window is active, which lets you compute detector precision and recall in PromQL |
| `stand_ground_truth_windows{scenario}` | number of windows in the file |
| `stand_ground_truth_annotations_pushed_total`, `..._push_errors_total` | Grafana API stats |

Every window becomes an org-wide Grafana **region annotation** tagged
`ground-truth`, `<scenario>`, `gtid:<hash>`. If the service restarts, it does not create duplicate annotations (it checks the existing `gtid` tags).

**Recording rules** (`stand:*`), evaluated every 5 s:
`stand:requests:rate1m`, `…_by_route`, `…_by_status_class`, `stand:errors_5xx:ratio1m`,
`stand:errors_4xx:ratio1m`, `stand:latency_p{50,95,99}_seconds:1m`,
`stand:response_size_bytes:avg1m`, plus 30-minute baselines `…:avg30m`.

## Baseline detectors (the reference for step 3)

| Alert | Rule | Scenario |
|---|---|---|
| `BaselineTrafficSpike` | rps > 2.5 × 30 m avg, for 1 m | traffic_spike |
| `BaselineHighLatencyP95` | p95 > 1 s, for 1 m | latency_degradation |
| `BaselineHighErrorRate` | 5xx ratio > 10 %, for 30 s | error_burst |
| `BaselineNotFoundStorm` | 4xx ratio > 25 %, for 1 m | not_found_storm |
| `BaselineHeavyPayload` | avg size > 5 × 30 m avg, for 1 m | heavy_payload |

They are deliberately simple fixed thresholds labelled `detector="baseline"`.
Step 3 will add `detector="isolation_forest"` and compare both against
the ground truth. Health alerts: `StandTargetDown`, `NginxLogExporterStale`,
`NginxLogParseErrors`. There is no Alertmanager: alerts are visible in Prometheus and in
the dashboard's *Firing alerts* panels, which is enough for the stand.

## Dashboards (folder *Anomaly Stand*)

* **Business overview**: golden-signal stats, the *Ground truth vs baseline detectors* state
  timeline, traffic, errors, latency percentiles, payload vs baseline, per-route breakdown,
  nginx and app runtime, and a table of firing alerts. Annotation layers: *Ground truth* (on)
  and *Baseline alerts firing* (toggle).
* **Stand health**: target `up`, scrape duration, exporter lag and parse errors, and the Prometheus TSDB.

Dashboards are provisioned from JSON. You can edit them in the UI, but the JSON
files are the source of truth. To keep a change, export the dashboard to JSON and replace the file.

## Operations

```bash
make reload-prometheus     # apply rule/config edits without restart
make check-prometheus      # promtool check config
make test-rules            # promtool unit tests for rules/alerts
make test-py               # pytest for exporters (in python:3.12-slim)
make test                  # all of the above
docker compose down        # stop (volumes kept)
docker compose down -v     # stop + wipe Prometheus TSDB & Grafana DB
```

Set `LOG_START_AT=beginning` to replay the whole existing access log once.
Note that replayed samples are counted "now", so their timestamps are not historical. For historical datasets, step 3 reads the logs directly.

## Extension points for step 3

* `prometheus/targets/ml-detector.yml.example` → rename to `ml-detector.yml`.
  Prometheus starts scraping the detector within 30 s (`file_sd`).
* `grafana/plugins/` is mounted into Grafana. Set `GRAFANA_UNSIGNED_PLUGINS=<plugin-id>` in `.env`.
* `--web.enable-admin-api` is on, so you can take TSDB snapshots of feature series for offline experiments.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `network anomaly-stand-net declared as external, but could not be found` | Start step 1 first |
| `nginx-logs` UP but all rates are 0 | Check that `../data/nginx-logs/access.json.log` grows. Check `nginx_log_parse_errors_total` and set `FIELD_*` if needed |
| `nginx` target DOWN | stub_status must listen on `nginx:8081` (step 1 config); see `NGINX_STUB_STATUS_URL` |
| No ground-truth annotations | No anomaly has been injected yet (`WARMUP_S`=1800 s), or check `docker compose logs gt-annotator` |
| Changed Grafana password in `.env` has no effect | It is applied only on the first start: `docker compose down -v` |
