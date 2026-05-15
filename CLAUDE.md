# Nova

Nova transforms raw real-life videos into viral short-form content (TikTok, Reels, Shorts).

## Session workflow: isolate in a worktree

**Before starting non-trivial work, create a git worktree.** The main checkout (`/Users/emirerben/Projects/nova`) is shared across sessions and uncommitted edits collide. Symptoms seen in the wild: stray `.venv` deletions, duplicate migration files, mixed `git status` spanning unrelated branches.

```bash
# From repo root, branch off main into a sibling checkout
git fetch origin main
git worktree add -b feat/<topic>-$(date +%Y-%m-%d) ../nova-<topic> origin/main
cd ../nova-<topic>
```

Rules:
- Run all edits, tests, and commits from the worktree path — never from `/Users/emirerben/Projects/nova` directly.
- One worktree per logical change. Don't reuse a worktree for an unrelated feature.
- When done, `git worktree remove ../nova-<topic>` after the PR merges. List active worktrees with `git worktree list`.
- `.claude/worktrees/agent-*` are auto-managed by the Agent tool (`isolation: "worktree"`) — leave those alone.
- Skip the worktree only for read-only investigation or single-line config tweaks confined to one file.

## Stack
- Frontend: Next.js (src/apps/web/) — TypeScript, React
- Backend: Python FastAPI + Celery (src/apps/api/) — video processing pipeline
- Queue: Redis (job queue for async processing)
- Storage: GCS or S3 — raw uploads + processed outputs NEVER in git
- DB: PostgreSQL (job metadata, user state)

## Key paths
- src/apps/web/  — Next.js frontend (upload UI, progress tracker, result viewer)
- src/apps/web/src/app/admin/templates/[id]/components/ — visual overlay editor (OverlayPreview, OverlayTimeline, PropertyPanel, overlay-constants.ts)
- src/apps/web/src/app/music/ — music gallery page (browse tracks, select, upload clips, poll result)
- src/apps/web/src/app/admin/music/ — admin music management (upload tracks, monitor analysis, publish/archive)
- src/apps/web/src/lib/music-api.ts — typed API client for music routes
- src/apps/api/  — Python API (upload endpoint, job queue, FFmpeg pipeline)
- src/apps/api/app/routes/admin_music.py — admin music-track CRUD + publish/reanalyze endpoints
- src/apps/api/app/routes/music.py — public music-track gallery endpoint
- src/apps/api/app/routes/music_jobs.py — beat-sync job submission + status polling
- src/apps/api/app/pipeline/music_recipe.py — beat-snap recipe generator (slot layout from beats)
- src/apps/api/app/tasks/music_orchestrate.py — Celery tasks: beat analysis + music job orchestration
- src/apps/api/app/services/audio_download.py — yt-dlp audio download + beat detection via FFmpeg
- src/apps/api/prompts/ — LLM prompt templates (template analysis, transcription)
- agents/        — project-level agent context (VIDEO_CONTEXT.md, STACK.md, DECISIONS.md)

## Local dev
cp .env.example .env            # fill in values
./scripts/dev-auto.sh            # single-command dev env with hot reload

`dev-auto.sh` starts redis + postgres via `docker-compose up -d redis db` (infra only — not the app), runs migrations, then launches API (`uvicorn --reload`), Celery worker (`watchfiles` auto-restart on `.py` edits), and Next.js (HMR) natively. Logs go to `.dev/<service>.log`. Do NOT restart servers manually — hot reload handles all code edits. Stop with `./scripts/dev-stop.sh`.

Alternatives:
- `./scripts/dev-no-docker.sh` — same flow but assumes postgres@16 + redis are already running (e.g. via `brew services`)
- `docker-compose up` — runs the full stack (web/api/worker) in containers; rarely needed since hot-reload native dev is faster

Production uses repo-root `Dockerfile` (Python 3.11 + FFmpeg/libheif/libmagic) deployed to Fly.io as both `api` and `worker` processes. Note: `src/apps/api/Dockerfile` and `src/apps/web/Dockerfile` are only referenced by docker-compose; production does not build them.

## Quality checks
- Backend tests: `cd src/apps/api && pytest` (asyncio mode auto; tests live in `src/apps/api/tests/`)
- Backend lint: `cd src/apps/api && ruff check . && ruff format --check .`
- Frontend lint: `cd src/apps/web && npm run lint`
- Frontend typecheck: `cd src/apps/web && npx tsc --noEmit`
- Frontend tests: `cd src/apps/web && npm test` (Jest)

## Domain context
- Target output: 9:16 aspect ratio, sub-60s, H.264/AAC, 1080x1920
- Hook window: first 2-3 seconds must create a question in the viewer's mind
- Processing is ASYNC — a 5-min source video takes 2-5 min to process
- Jobs polled via GET /jobs/:id/status or websocket
- Raw uploads and processed outputs are NEVER committed to git
- Read agents/VIDEO_CONTEXT.md for full video domain context and FFmpeg patterns
- Read agents/DECISIONS.md for why key choices were made

## Storage retention
- Per-job GCS objects (`dev-user/*`, `music-jobs/*`) are deleted by a bucket lifecycle rule 24h after upload. Config lives at `infra/gcs-lifecycle.json`; apply once with `gsutil lifecycle set infra/gcs-lifecycle.json gs://$STORAGE_BUCKET` (not part of CI deploy).
- Curated assets (`music/*` track library, `templates/*` poster/audio) are NOT matched by the rule and persist forever.
- Signed-URL TTL in `storage.py` is 1 day to match the object lifetime — a 7-day URL pointing at a 1-day object is a lie. Template poster URLs in `routes/templates.py` keep their 7-day TTL because the underlying `templates/*` blobs persist.
- `JobClip.storage_expires_at` is informational only — no sweeper reads it. Kept truthful (1 day) so a future sweeper isn't surprised.
- **When auth/login lands:** revisit `infra/gcs-lifecycle.json`. Put authenticated-user content under a new prefix (e.g. `users/{user_id}/`) that the delete rule does NOT match, and grow retention to match whatever the gallery UX promises.

## ⚠️ Anti-pattern: do NOT use MoviePy / VideoFileClip
VideoFileClip(path) buffers the entire video into RAM. On a 2GB source file this crashes.
Use subprocess FFmpeg directly. See agents/VIDEO_CONTEXT.md for patterns.

## Music beat-sync pipeline
- Beat detection: `_detect_audio_beats()` in `audio_download.py` uses FFmpeg `silencedetect`/`astats` energy-peak analysis on the downloaded audio file
- Best-section auto-select: `_auto_best_section()` scores 30s windows by beat density; result stored in `MusicTrack.track_config` as `best_start_s`/`best_end_s`/`slot_every_n_beats`
- Recipe generation: `generate_music_recipe()` in `music_recipe.py` slices the best section into N slots where N = beat_count / slot_every_n_beats; each slot target duration = beats-per-slot × beat interval
- Job orchestration: `orchestrate_music_job` Celery task runs parallel Gemini clip analysis → `template_matcher.match` → `_assemble_clips` with beat-snap → `_mix_template_audio`
- Audio download: `audio_download.py` uses yt-dlp subprocess (not yt-dlp Python API) to avoid RAM buffering; downloads to a temp path in GCS-mounted storage
- Track lifecycle: `pending` → `analyzing` → `ready` | `failed`; only `ready`+`published` tracks appear in the public gallery
- Admin proxy: Next.js `/api/admin/[...path]` route proxies to Fly.io API, keeping the admin token server-side only (never exposed to browser)
- Clip count: `slot_count` returned by `/music-tracks` tells the frontend exactly how many clips to collect; `POST /music-jobs` validates clip count matches before enqueuing
- Beat-sync guard: tracks with 0 detected beats are marked `failed` at analysis time; `POST /music-jobs` rejects non-ready or non-published tracks

## Template pipeline
- Interstitials: `app/pipeline/interstitials.py` detects curtain-close vs fade-to-black via FFmpeg luminance band analysis, renders color holds and `geq` pixel-expression curtain-close animations (drawbox cannot animate bar height over time)
- Transitions: `app/pipeline/transitions.py` translates Gemini vocabulary (whip-pan, zoom-in, dissolve) to internal FFmpeg xfade types
- Font bundle: Playfair Display (Bold + Regular) in `assets/fonts/`, referenced via `fontsdir` in ASS subtitle filters
- Text overlays: `app/pipeline/text_overlay.py` renders gaussian-shadow text (no hard outlines), supports font-cycle and ASS animated overlays
- Font-cycle: acceleration syncs with curtain-close (`font_cycle_accel_at_s`), `text_color` passthrough for colored text, per-size font caching in `_resolve_cycle_fonts()`, `MAX_FONT_CYCLE_FRAMES` (60) safety cap prevents PNG explosion, gap-fill PNG bridges frame cap to cycle_end
- Font-cycle settle: when `font_cycle_accel_at_s` is active, settle phase is skipped entirely (cycling runs to end_s)
- Cross-slot text merge: `_collect_absolute_overlays()` merges same-text+same-position overlays across adjacent slots when gap < `_MERGE_GAP_THRESHOLD_S` (2.0s), inheriting effect and accel from later slots
- Curtain-close minimum: `MIN_CURTAIN_ANIMATE_S=4.0` enforced at both `_assemble_clips` and `_collect_absolute_overlays` call sites, clamped to 60% of slot duration (`_CURTAIN_MAX_RATIO=0.6`, ensures at least 40% visible footage)
- Label config: `_LABEL_CONFIG` in `template_orchestrate.py` differentiates subject labels (large/120px/sans/#F4D03F/font-cycle/accel@8s) from prefix labels (medium/72px). Detection is content-based (`_is_subject_placeholder()` or "welcome" prefix), not role-based, because Gemini inconsistently returns `role=hook` instead of `role=label`. First-slot labels get timing overrides (prefix@2s, subject@3s)
- Clip rotation: `_minimum_coverage_pass()` in `template_matcher.py` pre-assigns most-constrained clips first to maximize variety before the greedy quality pass
- Timing overrides: `start_s_override` / `end_s_override` on overlay JSON correct Gemini's approximate timing; curtain exit clamp always wins
- Beat-snap: `cumulative_s` in `_assemble_clips()` must account for interstitial hold durations to keep beat-snap calculations accurate
- Timing: `_burn_text_overlays()` must not reassign font-cycle multi-PNG timestamps with single overlay timestamps (bug fixed in v0.1.1.0)

## Encoder policy (libx264 preset)
- **Intermediate encodes** (re-encoded downstream by another stage) → `preset="ultrafast"` is fine. Call sites: `reframe_and_export`, `_build_overlay_cmd` in `reframe.py`; `render_color_hold` in `interstitials.py`; `image_clip` rendering; `drive_import` thumbnailing.
- **Final-output encodes** (bytes that ship to the user) → `preset="fast"` or stricter is REQUIRED. `ultrafast` disables `mb-tree`, `psy-rd`, B-frames and trellis quant, which causes visible 16×16 macroblocking on smooth gradients (sky, dark canopy). CRF does NOT compensate — it controls the rate target, not which encoder features run. Call sites: `_concat_demuxer` fallback, `_pre_burn_curtain_slot_text`, `_burn_text_overlays` in `template_orchestrate.py`; `apply_curtain_close_tail` (`fast` + `tune=film`) in `interstitials.py`; `join_with_transitions` in `transitions.py`.
- Locked by `tests/test_encoder_policy.py` — adding a new `_encoding_args(...)` call site forces a conscious quality-budget decision via the allow-list. History: PR #102 (curtain-close → medium), PR #105 (curtain-close → fast + `--concurrency=1` + PNG-overlay), Brazil pixelation fix (propagated `fast` to the three `template_orchestrate.py` sites the prior PRs missed).

## Env vars needed (see .env.example for full list with descriptions)
- STORAGE_BUCKET, STORAGE_PROVIDER
- REDIS_URL
- DATABASE_URL
- OPENAI_API_KEY
- GEMINI_API_KEY — required for clip analysis (music jobs) and template analysis

## Agent evals
- Per-agent quality eval harness lives at `src/apps/api/tests/evals/`. Covers `template_recipe`, `clip_metadata`, `creative_direction` (Big 3). Two layers: deterministic structural assertions + Claude-Sonnet LLM-as-judge.
- Default: `cd src/apps/api && pytest tests/evals/ -v` — structural-only, replay mode, no network. Runs in CI.
- With judge: `... --with-judge` (needs `ANTHROPIC_API_KEY`).
- Live Gemini: `NOVA_EVAL_MODE=live ... --eval-mode=live --with-judge` (needs both keys; ~$2-5/run).
- Manual GH Action: `.github/workflows/agent-evals.yml` (`workflow_dispatch`).
- **Prompt-change rule:** when editing any file under `src/apps/api/prompts/` or any `render_prompt()`, bump the agent's `prompt_version` in its `AgentSpec` AND run `pytest tests/evals/<agent>_evals.py -v --with-judge --eval-mode=live` against current fixtures before merge. Compare scores against the prior version's run.
- See `tests/evals/README.md` for the full prompt-iteration loop.

## Deploy Configuration

### Architecture: Split Deploy
- **Frontend (Next.js)** → Vercel (CDN edge, preview deploys, auto-deploy on push)
- **API + Workers (FastAPI + Celery)** → Fly.io (long-running FFmpeg jobs, up to 18-20 min)

### Vercel — Frontend (`src/apps/web/`)
- Project: `emirerbens-projects/nova`
- Production URL: https://nova-video.vercel.app
- Framework: Next.js (auto-detected)
- Root directory: `src/apps/web/`
- Deploy: auto-deploys on push to `main` via GitHub integration. **Do NOT run `vercel --prod` from a feature branch** — it pushes your local working tree to production, including files not yet in `main`, and can ship a frontend route without its backing API endpoint. For an emergency manual deploy: `git checkout main && git pull`, then `cd src/apps/web && vercel --prod`.
- Env vars: set via `vercel env` CLI (NEXT_PUBLIC_API_URL, NEXT_PUBLIC_WS_URL, NEXT_PUBLIC_DEFAULT_TEMPLATE_ID, NEXT_PUBLIC_GOOGLE_CLIENT_ID, NEXT_PUBLIC_GOOGLE_PICKER_API_KEY, NEXTAUTH_SECRET)
- Deployment Protection: preview-only (production is public)
- Preview deploys: full API access via regex CORS (`allow_origin_regex` in `main.py`)

### Fly.io — API + Workers (configured by /setup-deploy)
- App name: nova-video
- Region: iad
- Production URL: https://nova-video.fly.dev
- Deploy workflow: `fly deploy` from repo root (or GitHub Actions CD)
- Deploy status command: `fly status --app nova-video`
- Merge method: squash
- Process groups: api (FastAPI/uvicorn) + worker (Celery)
- Release command: `python -m alembic upgrade head` (runs migrations on every deploy)
- VM sizing: api = 1 shared CPU / 512MB, worker = 2 shared CPUs / 2048MB
- Dockerfile: repo-root `Dockerfile` (cached dependency layer from pyproject.toml)
- Docker image includes: `app/`, `assets/`, `prompts/`, `alembic.ini`
- CORS: `ALLOWED_ORIGINS` env var — JSON array format, e.g. `'["http://localhost:3000","https://nova-video.vercel.app"]'`

### Custom deploy hooks
- Pre-merge: none
- Deploy trigger (API): `fly deploy` (manual) or GitHub Actions (after CD workflow added)
- Deploy trigger (Frontend): push to `main` (Vercel auto-deploy) or `cd src/apps/web && vercel --prod`
- Deploy status (API): `fly status --app nova-video`
- Health check (API): https://nova-video.fly.dev/health

### Fly.io Secrets (set via `fly secrets set`)
Required before first deploy:
```bash
fly secrets set -a nova-video \
  DATABASE_URL="..." \
  REDIS_URL="..." \
  STORAGE_BUCKET="..." \
  STORAGE_PROVIDER="..." \
  GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}' \
  OPENAI_API_KEY="..." \
  GEMINI_API_KEY="..." \
  ALLOWED_ORIGINS='["http://localhost:3000","https://nova-video.vercel.app"]'
```

## Lessons from prod

### Dockerfile / .dockerignore coupling
Any new `COPY <src> ...` line added to the prod `Dockerfile` must have its source path
verified against `.dockerignore` — the two files do not track each other and there is no
local tool that flags the mismatch. If `.dockerignore` excludes the source path, the
`COPY` will fail with "not found in build context" at Fly's builder, AFTER the PR has
already merged to `main`.

Incident (May 2026): PR #118 (`f156c19`, v0.4.7.0) added
`COPY src/apps/api/tests/evals/rubrics ./tests/evals/rubrics` for the Loop B online
judge. Local pytest + the `test-api` CI job both passed (they run against the source
tree, not the image). The deploy then failed on Fly. PR #119 (`121a6e9`) hotfixed
`.dockerignore` with negation patterns to let the rubrics through.

`.github/workflows/docker-build.yml` runs `docker build` against the prod Dockerfile on
every PR that touches `Dockerfile`, `.dockerignore`, or `src/apps/api/**` — so this
class of bug fails on the PR, not on merge-to-main.

