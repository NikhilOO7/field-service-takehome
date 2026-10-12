# Field Service Copilot. Run from this folder. Python commands go through uv.
COMPOSE = docker compose
API = cd backend && uv run

.PHONY: up down db install migrate ingest eval leak-test test api web demo-reset

up:            ## start Postgres, the API and the web app in Docker
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

db:            ## start only Postgres (for running the API locally with uv)
	$(COMPOSE) up -d db

install:       ## local Python + JS dependencies
	cd backend && uv sync
	cd frontend && npm ci

migrate:
	$(API) alembic upgrade head

ingest:        ## parse, chunk, embed the corpus (incremental); writes reports/ingestion_report.md
	$(API) copilot ingest

eval:          ## run the 10 eval questions and grade them; writes reports/eval_<timestamp>.md
	$(API) copilot eval

leak-test:     ## ACL matrix + hand-written permission probes
	$(API) copilot leak-test

test:          ## unit/integration tests against the copilot_test database (no LLM calls)
	$(API) pytest

api:           ## run the API locally on :8000
	$(API) uvicorn app.main:app --reload --port 8000

web:           ## run the web app locally on :5173 (proxies /api to :8000)
	cd frontend && npm run dev

demo-reset:    ## clear conversations, work orders and the outbox (keeps the ingested corpus)
	$(COMPOSE) exec -T db psql -U copilot -d copilot -c "TRUNCATE outbox, work_order_events, work_orders, messages, conversations, audit_log CASCADE"
	rm -rf var/outbox
