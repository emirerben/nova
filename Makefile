.PHONY: dev dev-web dev-api test build lint \
        workspace-pull workspace-push workspace-status

# ── Local dev ──────────────────────────────────────────────────────────────────

dev:
	docker-compose up

dev-web:
	docker-compose up web

dev-api:
	docker-compose up api worker redis db

# ── Tests ──────────────────────────────────────────────────────────────────────

test:
	(cd src/apps/web && pnpm test)
	(cd src/apps/api && python -m pytest)

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
