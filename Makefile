.PHONY: dev dev-web dev-api api-install-dev test test-api test-quality build lint verify \
        local-render local-render-build local-render-up local-render-down \
        local-render-logs local-render-migrate verify-overlays verify-motion-performance \
        carousel-capture carousel-verify verify-editor-timeline \
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
OVERLAY_VERIFY_HOST_OUT := $(abspath $(OVERLAY_VERIFY_OUT))
OVERLAY_VERIFY_FIXTURES ?= src/apps/api/tests/fixtures/overlay_verify
OVERLAY_VERIFY_FIXTURES_HOST := $(abspath $(OVERLAY_VERIFY_FIXTURES))
ARGS ?= --fixtures

verify-overlays:
	@mkdir -p $(OVERLAY_VERIFY_OUT)
	@# CLI reads no secrets for --fixtures/--recipe, but compose needs the file to parse the service.
	@[ -f .env.local-render ] || touch .env.local-render
	$(LOCAL_RENDER_COMPOSE) build api
	$(LOCAL_RENDER_COMPOSE) run --rm --no-deps \
		-e NOVA_IN_PROD_IMAGE=1 \
		-v "$(OVERLAY_VERIFY_HOST_OUT):/app/.overlay-verify" \
		-v "$(OVERLAY_VERIFY_FIXTURES_HOST):/app/tests/fixtures/overlay_verify:ro" \
		api python -m app.cli.verify_overlays $(ARGS) --out /app/.overlay-verify

# Maximum accepted Creator Block workload in the production image. The CLI
# renders all 240 1080x1920 frames and fails above 180s or 2.5GB child RSS.
MOTION_VERIFY_OUT ?= .motion-verify
MOTION_VERIFY_HOST_OUT := $(abspath $(MOTION_VERIFY_OUT))

verify-motion-performance:
	@mkdir -p $(MOTION_VERIFY_OUT)
	@[ -f .env.local-render ] || touch .env.local-render
	$(LOCAL_RENDER_COMPOSE) build api
	$(LOCAL_RENDER_COMPOSE) run --rm --no-deps \
		-e NOVA_IN_PROD_IMAGE=1 \
		-v "$(MOTION_VERIFY_HOST_OUT):/app/.motion-verify" \
		api python -m app.cli.verify_motion_performance --out /app/.motion-verify

# ── Carousel parity (browser reference vs. our Python/Skia render) ────────────
# `carousel-capture` drives the gstack browse daemon through one of the four
# reference HTML pages (tools/carousel_reference/) and dumps its motion trace
# + a reference.mp4. `carousel-verify` then renders our side through the real
# pipeline and compares SSIM + per-frame motion deltas against that capture.
# See tools/carousel_reference/README.md and src/apps/api/tests/quality/carousel_parity.py.
#
# EFFECT uses the Python pipeline's effect names (scale_sweep, cover_flow,
# cards_stack, flipbook — see app/pipeline/carousel/effects.py:EFFECTS), NOT
# capture.sh's own vocabulary, which is the HTML page filenames (scale-sweep,
# cover-flow, cards, flipbook — hyphenated, and "cards" not "cards_stack").
# CAROUSEL_HTML_SLUG below is the one-line translation between the two; do
# not pass a hyphenated slug to EFFECT directly.
#
# Usage:
#   make carousel-capture EFFECT=scale_sweep     # needs the gstack browse daemon
#   make carousel-capture EFFECT=cover_flow
#   make carousel-capture EFFECT=cards_stack
#   make carousel-capture EFFECT=flipbook
#   make carousel-verify  EFFECT=scale_sweep [SSIM_MIN=0.95] [TRACE_TOL_PX=2.0]
CAROUSEL_REF_DIR := tools/carousel_reference
EFFECT ?= scale_sweep
CAROUSEL_OUT_DIR ?= $(CAROUSEL_REF_DIR)/out/$(EFFECT)
CAROUSEL_HTML_SLUG := $(shell echo "$(EFFECT)" | sed -e 's/cards_stack/cards/' -e 's/_/-/g')
SSIM_MIN ?= 0.95
TRACE_TOL_PX ?= 2.0

carousel-capture:
	@echo "carousel-capture: EFFECT=$(EFFECT) (capture.sh slug: $(CAROUSEL_HTML_SLUG)) -> $(CAROUSEL_OUT_DIR)"
	@echo "NOTE: requires the gstack browse daemon — see tools/carousel_reference/README.md 'Capturing' prerequisites."
	(cd $(CAROUSEL_REF_DIR) && ./capture.sh $(CAROUSEL_HTML_SLUG) "$(CURDIR)/$(CAROUSEL_OUT_DIR)")

carousel-verify: api-install-dev
	@if [ ! -f "$(CAROUSEL_OUT_DIR)/trace.json" ]; then \
		echo "ERROR: no trace.json at $(CAROUSEL_OUT_DIR). Run 'make carousel-capture EFFECT=$(EFFECT)' first."; \
		exit 2; \
	fi
	(cd $(API_DIR) && $(API_LOCAL_PYTHON) -m app.cli.verify_carousel \
		--effect $(EFFECT) \
		--reference "$(CURDIR)/$(CAROUSEL_OUT_DIR)" \
		--ssim-min $(SSIM_MIN) \
		--trace-tol-px $(TRACE_TOL_PX))

# Deterministic editor insert/ripple/resize contract, including the desktop
# Guided Story V2 acceptance fixture. Keep this targeted so it is cheap enough
# to run for every future timing feature.
verify-editor-timeline:
	(cd src/apps/web && npm test -- --runInBand \
		src/__tests__/plan/items/CarouselPanel.test.tsx \
		src/__tests__/plan/items/virtual-timeline.test.ts \
		src/__tests__/guided-story-parity.test.ts \
		src/__tests__/plan/items/EditorTimelineBody-carousel.test.tsx \
		src/__tests__/plan/items/carousel-preview-impl/CarouselBlockPreviewImpl.test.tsx \
		src/__tests__/plan/items/carousel-preview-impl/geometry.test.ts \
		src/__tests__/plan/items/editor-timeline-ai-sequence-marker.test.tsx \
		src/__tests__/plan/items/inspector-panel-motion.test.tsx \
		src/__tests__/plan/items/ToolDrawer-visuals.test.tsx \
		src/__tests__/lib/motion-runtime.test.ts \
		src/__tests__/lib/motion-preview-performance.test.ts \
		src/__tests__/lib/motion-runtime-generated-contract.test.ts \
		src/lib/__tests__/carousel-timing.test.ts \
		src/lib/timeline/__tests__/timeline-scale.test.ts && \
		npx playwright test --project=desktop-editor && \
		npx playwright test --project=iphone13 --project=iphone14 mobile-video-editor.spec.ts)
	(cd $(API_DIR) && $(API_LOCAL_PYTHON) -m pytest -q \
		tests/pipeline/carousel/test_choreography.py \
		tests/pipeline/carousel/test_segment_kill_switch.py \
		tests/pipeline/test_motion_scene.py \
		tests/pipeline/test_guided_story.py \
		tests/routes/test_editor_commit.py \
		tests/schemas/test_guided_edit_revision.py \
		tests/schemas/test_guided_story_parity.py \
		tests/tasks/test_motion_scene_cache.py \
		tests/tasks/test_carousel_timed_lane_projection.py)

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
