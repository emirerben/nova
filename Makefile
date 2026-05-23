.PHONY: dev dev-web dev-api test build lint \
        local-render local-render-build local-render-up local-render-down \
        local-render-logs local-render-migrate \
        workspace-pull workspace-push workspace-status

# ── Local dev ──────────────────────────────────────────────────────────────────

dev:
	docker-compose up

dev-web:
	docker-compose up web

dev-api:
	docker-compose up api worker redis db

# ── Local-render parity (runs the prod Dockerfile locally) ────────────────────
# Usage:
#   make local-render CLIP=/path/to/video.mp4 TEMPLATE=<uuid> \
#       [MODE=template|music] [INPUTS='{"location":"Tokyo"}']
# See docker-compose.local-render.yml and CLAUDE.md → "Local-render parity".

LOCAL_RENDER_COMPOSE := docker-compose -f docker-compose.local-render.yml
MODE   ?= template
INPUTS ?= {}

local-render-build:
	@if [ ! -f .env.local-render ]; then \
		echo "ERROR: .env.local-render not found. Run: cp .env.local-render.example .env.local-render"; \
		exit 2; \
	fi
	$(LOCAL_RENDER_COMPOSE) build

local-render-up: local-render-build
	$(LOCAL_RENDER_COMPOSE) up -d db redis api worker
	@echo "→ waiting for api at http://localhost:8001/health…"
	@until curl -sf http://localhost:8001/health >/dev/null 2>&1; do sleep 1; done
	@echo "→ api is up"

local-render-migrate: local-render-up
	$(LOCAL_RENDER_COMPOSE) exec -T api python -m alembic upgrade head

local-render-down:
	$(LOCAL_RENDER_COMPOSE) down

local-render-logs:
	$(LOCAL_RENDER_COMPOSE) logs -f --tail=200 api worker

local-render: local-render-migrate
	@if [ -z "$(CLIP)" ] || [ -z "$(TEMPLATE)" ]; then \
		echo "Usage: make local-render CLIP=/path/to/video.mp4 TEMPLATE=<uuid> [MODE=template|music] [INPUTS='{\"location\":\"Tokyo\"}']"; \
		exit 2; \
	fi
	python3 scripts/local-render.py \
		--clip "$(CLIP)" \
		--template "$(TEMPLATE)" \
		--mode "$(MODE)" \
		--inputs '$(INPUTS)'

# ── Tests ──────────────────────────────────────────────────────────────────────

test:
	(cd src/apps/web && pnpm test)
	(cd src/apps/api && python -m pytest tests/ --ignore=tests/quality -v)

test-quality:
	(cd src/apps/api && python -m pytest tests/quality/ -v)

migrate:
	(cd src/apps/api && alembic upgrade head)

migrate-new:
	(cd src/apps/api && alembic revision --autogenerate -m "$(msg)")

# ── Build ──────────────────────────────────────────────────────────────────────

build:
	(cd src/apps/web && pnpm build)
	(cd src/apps/api && docker build -t nova-api .)

# ── Lint ───────────────────────────────────────────────────────────────────────

lint:
	(cd src/apps/web && pnpm lint)
	(cd src/apps/api && ruff check .)

# ── nova-workspace sync ────────────────────────────────────────────────────────
# Syncs product docs (PROJECT.md, TASKS.md, PRD.md, etc.)
# Technical agent context (agents/) is in this repo — no sync needed

WORKSPACE_DIR := $(HOME)/.openclaw/workspace/startups/nova

workspace-pull:
	git -C $(WORKSPACE_DIR) pull

workspace-push:
	git -C $(WORKSPACE_DIR) add -A && \
	git -C $(WORKSPACE_DIR) commit -m "workspace sync $$(date +%Y-%m-%d\ %H:%M)" && \
	git -C $(WORKSPACE_DIR) push

workspace-status:
	git -C $(WORKSPACE_DIR) status
