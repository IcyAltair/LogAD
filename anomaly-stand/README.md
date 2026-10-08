# LogAD
Anomaly Detection in logs

# Anomaly Detection Stand — Step 1: Business Layer

Experimental (non-PROD) stand for detecting **operational** anomalies
(latency degradation, error bursts, traffic spikes, etc.; not security attacks)
in nginx logs with an Isolation Forest.

| Step | Layer | Status |
|------|-------|--------|
| 1 | Business layer: nginx + Demo Shop API + UI + traffic generator | ✅ this step |
| 2 | Observability: Prometheus, Grafana, nginx/app exporters | next |
| 3 | ML: Isolation Forest pipeline (train/test/validate), online scoring, Grafana alerts plugin | next |

## Components

| Service | Purpose | Port |
|---|---|---|
| `nginx` | Reverse proxy, static UI, **JSON access logs** | `8080` (host), `8081` stub_status (internal) |
| `app` | FastAPI Demo Shop: business API, chaos API, `/metrics` | `8000` (internal) |
| `traffic-generator` | Synthetic users with a daily pattern + scheduled anomalies + ground truth | — |

## Quick start
```bash
docker compose up -d --build
docker compose ps

# all services should be healthy

open http://localhost:8080

# UI
open http://localhost:8080/docs

# Swagger

tail -f data/nginx-logs/access.json.log | jq -c '{t:.time_iso8601,m:.request_method,u:.request_uri,s:.status,rt:.request_time}'
cat data/ground-truth/anomalies.jsonl | jq .
docker compose logs -f traffic-generator
```

Quick demo (anomalies within minutes): set in `.env`
`WARMUP_S=60`, `ANOMALY_MIN_GAP_S=60`, `ANOMALY_MAX_GAP_S=120`, then `docker compose up -d`.

For ML training, collect **at least 2–3 simulated days** (`DAY_PERIOD_S`).
The warm-up period (`WARMUP_S`) is anomaly-free and can be used as a clean training set.

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Liveness + stats |
| GET | `/api/products?category=&limit=&offset=` | Paginated catalog |
| GET | `/api/products/{id}` | Product details (404 if missing) |
| POST | `/api/orders` `{"product_id":1,"quantity":2}` | Create order (201 / 404 / 409 / 422) |
| GET | `/api/orders/{id}` | Order details |
| GET/POST/DELETE | `/api/chaos` | Fault injection state |
| GET | `/metrics` | Prometheus metrics (app container only, not exposed via nginx) |

### Chaos API example
```bash
curl -X POST localhost:8080/api/chaos -H 'Content-Type: application/json' \
  -d '{"latency_ms":1500,"latency_jitter_ms":300,"error_rate":0.1,"endpoints":["/api/products"],"duration_s":120}'
curl -X DELETE localhost:8080/api/chaos
```

Every chaos config has a TTL (`duration_s`), so it switches itself off.
Chaos set manually (UI/curl) is **not** written to ground truth.

## nginx log format (`data/nginx-logs/access.json.log`)

One JSON object per line:
```json
{"time_iso8601":"2026-10-08T12:00:01+00:00","msec":1791460801.123,"request_id":"5f0c...",
 "remote_addr":"172.20.0.4","x_forwarded_for":"198.51.100.17","request_method":"GET",
 "request_uri":"/api/products?limit=10&amp;offset=0","server_protocol":"HTTP/1.1","status":200,
 "body_bytes_sent":1432,"request_length":182,"request_time":0.031,"upstream_addr":"172.20.0.2:8000",
 "upstream_status":"200","upstream_connect_time":"0.000","upstream_response_time":"0.031",
 "http_referer":"","http_user_agent":"Mozilla/5.0 ...","connection":42,"connection_requests":7}
```

Notes for parsing: `upstream_*` fields are strings (empty for static files, may contain
comma-separated lists on retries). `/healthz` and `/favicon.ico` are not logged.

## Traffic model

* Rate = `BASE_RPS × diurnal(t) × noise`, Poisson arrivals, one simulated day = `DAY_PERIOD_S`.
* Action mix: page load 10%, catalog 35%, product view 30%, order 15%, order check 10%.
* Natural background errors: ~2% product 404s, ~3% order 422s (normal, not anomalies).

### Anomaly scenarios (ground truth)

| Scenario | Mechanism | Expected log signature |
|---|---|---|
| `traffic_spike` | client rate ×4–8 | request count ↑ |
| `latency_degradation` | +0.8–2.5 s on `/api/products*` | `request_time` ↑ (and possibly 504s) |
| `error_burst` | 20–50% of API calls return 500/503 | 5xx ratio ↑ |
| `not_found_storm` | +10–25 rps to missing resources | 4xx ratio ↑, new URIs |
| `heavy_payload` | catalog response ×20–100 | `body_bytes_sent` ↑ |

`data/ground-truth/anomalies.jsonl`:
```json
{"scenario":"error_burst","start":"2026-10-08T12:10:00+00:00","end":"2026-10-08T12:12:30+00:00",
 "start_ts":1791461400.0,"end_ts":1791461550.0,"duration_s":150,"params":{"error_rate":0.35,"error_codes":[500,503]}}
```

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `NGINX_HTTP_PORT` | 8080 | Host port |
| `PRODUCT_COUNT` / `SEED` | 50 / 42 | Catalog size / deterministic seed |
| `BASE_RPS` | 8 | Mean load |
| `DAY_PERIOD_S` | 3600 | Simulated day length |
| `DIURNAL_AMPLITUDE` | 0.6 | Day/night amplitude (0..1) |
| `WARMUP_S` | 1800 | Clean period before the first anomaly |
| `ANOMALIES_ENABLED` | true | Turn scenarios on/off |
| `ANOMALY_MIN_GAP_S` / `ANOMALY_MAX_GAP_S` | 300 / 900 | Pause between scenarios |
| `SCENARIOS` | all | Comma-separated subset |

## Operations
```bash
docker compose stop traffic-generator

# pause load
docker compose down

# stop stand (logs stay in ./data)
rm -rf data/

# reset collected data
```

## Limitations (non-PROD by design)

* No TLS, no auth (chaos API is open), no log rotation: clean up `data/` periodically.
* In-memory state: restart resets orders.
* Single uvicorn worker (chaos state is per-process).
* If the `app` container is recreated, restart nginx (`docker compose restart nginx`) to re-resolve the upstream.