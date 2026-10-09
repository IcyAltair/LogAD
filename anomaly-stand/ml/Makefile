# Helper targets for the ML layer (run from anomaly-stand/ml)
COMPOSE ?= docker compose

.PHONY: up down logs train prepare fit evaluate report reload test-py

up:            ## build + start the online detector
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100 ml-detector

train:         ## full pipeline: prepare -> train -> test (detector hot-reloads the model)
	$(COMPOSE) run --rm ml-trainer

prepare:       ## individual steps
	$(COMPOSE) run --rm ml-trainer python -m anomaly.pipeline prepare
fit:
	$(COMPOSE) run --rm ml-trainer python -m anomaly.pipeline train
evaluate:
	$(COMPOSE) run --rm ml-trainer python -m anomaly.pipeline test

report:        ## last test report
	cat ../data/ml/reports/latest.md

reload:        ## force model reload in the detector
	curl -fsS -X POST http://localhost:9200/api/reload && echo

test-py:       ## unit + end-to-end tests on synthetic logs
	$(COMPOSE) run --rm ml-trainer sh -c "pip install -q pytest==8.3.4 && pytest -q /srv/tests"