.PHONY: help setup test demo api pwa build lint migrate verify rollback

help: ## show this help
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## install backend + pwa dependencies
	python3 -m venv .venv
	./.venv/bin/pip install -e "backend[dev]"
	cd pwa && npm install

test: ## run the backend test suite
	cd backend && ../.venv/bin/python -m pytest -q

lint: ## ruff the backend, tsc the frontend
	cd backend && ../.venv/bin/ruff check app tests
	cd pwa && npx tsc --noEmit

api: ## run the API on :8000 (SQLite, no credentials needed)
	cd backend && ../.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

pwa: ## run the Patwari PWA on :5173, proxying /v1 to :8000
	cd pwa && npx vite --host 0.0.0.0 --port 5173

build: ## production build of the PWA
	cd pwa && npx vite build

demo: ## six-scene end-to-end demonstration
	cd backend && ../.venv/bin/python scripts/demo.py

verify: ## verify the ledger chain on the running API
	curl -s http://localhost:8000/v1/ledger/verify | python3 -m json.tool

migrate: ## apply SQL migrations (requires psql + KLRI_DB_PASSWORD)
	@for f in db/migrations/*.sql; do echo "== $$f"; \
	  PGPASSWORD=$$KLRI_DB_PASSWORD psql -h $${KLRI_DB_HOST:-localhost} \
	    -U $${KLRI_DB_USER:-klri} -d $${KLRI_DB_NAME:-klri} -f $$f || exit 1; \
	done

rollback: ## EMERGENCY: revert every district to advisory. No deploy, no data loss.
	@echo "Setting KLRI_ENFORCEMENT_ENABLED=false — revert with the same variable set to true."
	@echo "A ROLLBACK_ACTIVATED event is written to the chain, so the record of when"
	@echo "enforcement was on and off is itself tamper-evident."
