# INPROOM developer commands. Run `make help` for the list.
SHELL := /bin/bash
COMPOSE := docker compose --env-file .env -f infra/local/docker-compose.yml
API := cd apps/api &&
WEB := cd apps/web &&

ifneq (,$(wildcard .env))
include .env
export
endif

.PHONY: help doctor setup up down reset migrate api web test test-api test-security e2e lint format contracts contracts-check rbac-matrix rbac-matrix-check

help: ## List available commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

doctor: ## Check local prerequisites
	@bash scripts/doctor.sh

setup: ## Create .env and install API + web dependencies
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")
	$(API) uv sync --frozen
	$(WEB) npm ci --no-audit --no-fund

up: ## Start PostgreSQL, Keycloak and object store; wait until healthy
	$(COMPOSE) up -d --wait

down: ## Stop the local stack (keeps data)
	$(COMPOSE) down

reset: ## Stop the local stack and delete its data volumes
	$(COMPOSE) down -v

migrate: ## Apply database migrations
	$(API) uv run alembic upgrade head

api: ## Run the API with auto-reload on :8000
	$(API) uv run uvicorn app.main:app --reload --port $${API_PORT:-8000}

web: ## Run the web app on :3000
	$(WEB) INPROOM_API_URL=http://localhost:$${API_PORT:-8000} npm run dev -- --port $${WEB_PORT:-3000}

test: test-api contracts-check rbac-matrix-check ## Run all tests and checks (stack must be up and migrated)
	$(WEB) npm run typecheck

test-api: ## API unit + integration tests
	$(API) uv run pytest -v

test-security: ## Tenant isolation suite; writes apps/api/build/isolation-matrix.md
	$(API) INPROOM_ISOLATION_REPORT=build/isolation-matrix.md uv run pytest tests/security -v

e2e: ## Playwright end-to-end tests (stack up + migrated; starts API and web if not running)
	$(WEB) npx playwright test

lint: ## Lint and format-check
	$(API) uv run ruff check . && uv run ruff format --check .
	$(WEB) npm run typecheck

format: ## Auto-format API code
	$(API) uv run ruff format . && uv run ruff check --fix .

contracts: ## Regenerate packages/contracts/openapi.json
	$(API) uv run python scripts/export_openapi.py

contracts-check: ## Fail if the committed OpenAPI contract is out of date
	$(API) uv run python scripts/export_openapi.py --check

rbac-matrix: ## Regenerate docs/security/rbac-matrix.md from the authorization policy
	$(API) uv run python scripts/export_rbac_matrix.py

rbac-matrix-check: ## Fail if the committed role matrix doc is out of date
	$(API) uv run python scripts/export_rbac_matrix.py --check
