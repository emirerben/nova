.PHONY: dev dev-web dev-api api-install-dev test test-api test-quality build lint verify \
        local-render local-render-build local-render-up local-render-down \
        local-render-logs local-render-migrate verify-overlays \
        workspace-pull workspace-push workspace-status

PYTHON ?= python3
API_DIR := src/apps/api
API_VENV ?= $(API_DIR)/.venv
API_PYTHON := $(API_VENV)/bin/python
API_LOCAL_PYTHON := .venv/bin/python

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
#
#   # generative mode (no template; song auto-matched; renders all 3 variants):
#   make local-render MODE=generative CLIPS="a.mp4 b.mp4 c.mp4"
#   # output length is DERIVED from the footage — there is no target-length knob.
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
	@if [ "$(MODE)" = "generative" ]; then \
		if [ -z "$(CLIPS)" ]; then \
			echo "Usage: make local-render MODE=generative CLIPS=\"a.mp4 b.mp4 c.mp4\""; \
			exit 2; \
		fi; \
		python3 scripts/local-render.py --mode generative \
			$(foreach c,$(CLIPS),--clip "$(c)") \
			$(if $(EDIT_FORMAT),--edit-format $(EDIT_FORMAT)) \
			$(if $(VOICEOVER),--voiceover "$(VOICEOVER)"); \
	else \
		if [ -z "$(CLIP)" ] || [ -z "$(TEMPLATE)" ]; then \
			echo "Usage: make local-render CLIP=/path/to/video.mp4 TEMPLATE=<uuid> [MODE=template|music] [INPUTS='{\"location\":\"Tokyo\"}']"; \
			echo "   or: make local-render MODE=generative CLIPS=\"a.mp4 b.mp4 c.mp4\""; \
			exit 2; \
		fi; \
		python3 scripts/local-render.py \
			--clip "$(CLIP)" \
			--template "$(TEMPLATE)" \
			--mode "$(MODE)" \
			--inputs '$(INPUTS)'; \
	fi

# ── Pre-PR text-overlay verify (renders in the prod image, checks clipping) ──
# Renders a recipe's text overlays through the REAL Skia path inside the prod
# Docker image (so fonts + ffmpeg match prod), then asserts each overlay is
# un-clipped and writes a montage for visual content review. This is CLAUDE.md's
# rule as code: "an agentic/music overlay change is verified against the burned
# Skia output, not the Pillow admin preview." Run BEFORE opening a text PR.
#
# Usage:
#   make verify-overlays ARGS="--fixtures"                 # the regression set
#   make verify-overlays ARGS="--recipe path/to/recipe.json"
#   make verify-overlays ARGS="--template <uuid>"          # host-only (needs admin token)
#
# Outputs to .overlay-verify/: report.json (clipping verdicts) + montage.png.
# Exits non-zero if any overlay is clipped. tesseract is not in the prod image,
# so the content check is the montage (review it / let the agent read it); to
# add automated OCR content matching, run the host stage afterward:
#   cd src/apps/api && python -m app.cli.verify_overlays --stage ocr --out ../../../.overlay-verify
OVERLAY_VERIFY_OUT ?= .overlay-verify

verify-overlays:
	@mkdir -p $(OVERLAY_VERIFY_OUT)
	@# CLI reads no secrets for --fixtures/--recipe, but compose needs the file to parse the service.
	@[ -f .env.local-render ] || touch .env.local-render
	$(LOCAL_RENDER_COMPOSE) build api
	$(LOCAL_RENDER_COMPOSE) run --rm --no-deps \
		-e NOVA_IN_PROD_IMAGE=1 \
		-v "$(CURDIR)/$(OVERLAY_VERIFY_OUT):/app/$(OVERLAY_VERIFY_OUT)" \
		-v "$(CURDIR)/src/apps/api/tests/fixtures/overlay_verify:/app/tests/fixtures/overlay_verify:ro" \
		api python -m app.cli.verify_overlays $(ARGS) --out /app/$(OVERLAY_VERIFY_OUT)

# ── Tests ──────────────────────────────────────────────────────────────────────

api-install-dev:
	@if [ ! -x "$(API_PYTHON)" ]; then \
		$(PYTHON) -m venv "$(API_VENV)"; \
	fi
	$(API_PYTHON) -m pip install --upgrade pip setuptools
	(cd $(API_DIR) && $(API_LOCAL_PYTHON) -m pip install -e ".[dev]")

test: api-install-dev
	(cd src/apps/web && pnpm test)
	(cd $(API_DIR) && $(API_LOCAL_PYTHON) -m pytest tests/ --ignore=tests/quality -v)

test-api: api-install-dev
	(cd $(API_DIR) && $(API_LOCAL_PYTHON) -m pytest tests/ --ignore=tests/quality -v)

test-quality: api-install-dev
	(cd $(API_DIR) && $(API_LOCAL_PYTHON) -m pytest tests/quality/ -v)

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

# ── Verify (one-command local gate: lint + typecheck + all tests) ──────────────

verify: lint
	(cd src/apps/web && npx tsc --noEmit)
	$(MAKE) test

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
