# Helper targets for the observability layer (run from the observability/ dir)
COMPOSE ?= docker compose
PROM_IMAGE ?= prom/prometheus:v2.55.1

.PHONY: up down ps logs reload-prometheus check-prometheus test-rules test-py test

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail=100

# Hot-reload Prometheus config/rules (enabled by --web.enable-lifecycle)
reload-prometheus:
	curl -fsS -X POST http://localhost:9090/-/reload && echo "reloaded"

# Static validation of prometheus.yml and rule files
check-prometheus:
	docker run --rm -v "$(CURDIR)/prometheus:/etc/prometheus:ro" --entrypoint promtool $(PROM_IMAGE) check config /etc/prometheus/prometheus.yml

# Unit tests for recording + alerting rules
test-rules:
	docker run --rm -v "$(CURDIR)/prometheus:/etc/prometheus:ro" -w /etc/prometheus/tests --entrypoint promtool $(PROM_IMAGE) test rules rules_test.yml

# Unit tests for the Python exporters
test-py:
	docker run --rm -v "$(CURDIR)/exporters:/src" -w /src python:3.12-slim sh -c "pip install -q -r requirements-dev.txt && pytest -q"

test: check-prometheus test-rules test-py
