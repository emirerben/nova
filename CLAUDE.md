# Nova

Nova transforms raw real-life videos into viral short-form content (TikTok, Reels, Shorts).

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

`dev-auto.sh` starts redis + postgres (docker), runs migrations, and launches API (`uvicorn --reload`), Celery worker (`watchfiles` auto-restart on `.py` edits), and Next.js (HMR). Logs go to `.dev/<service>.log`. Do NOT restart servers manually — hot reload handles all code edits. Stop with `./scripts/dev-stop.sh`.

Alternative: `docker-compose up` runs everything in containers (no hot reload for worker/api).

## Domain context
- Target output: 9:16 aspect ratio, sub-60s, H.264/AAC, 1080x1920
- Hook window: first 2-3 seconds must create a question in the viewer's mind
- Processing is ASYNC — a 5-min source video takes 2-5 min to process
- Jobs polled via GET /jobs/:id/status or websocket
- Raw uploads and processed outputs are NEVER committed to git
- Read agents/VIDEO_CONTEXT.md for full video domain context and FFmpeg patterns
- Read agents/DECISIONS.md for why key choices were made

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

## Env vars needed (see .env.example for full list with descriptions)
- STORAGE_BUCKET, STORAGE_PROVIDER
- REDIS_URL
- DATABASE_URL
- OPENAI_API_KEY
- GEMINI_API_KEY — required for clip analysis (music jobs) and template analysis

## Deploy Configuration

### Architecture: Split Deploy
- **Frontend (Next.js)** → Vercel (CDN edge, preview deploys, auto-deploy on push)
- **API + Workers (FastAPI + Celery)** → Fly.io (long-running FFmpeg jobs, up to 18-20 min)

### Vercel — Frontend (`src/apps/web/`)
- Project: `emirerbens-projects/nova`
- Production URL: https://nova-video.vercel.app
- Framework: Next.js (auto-detected)
- Root directory: `src/apps/web/`
- Deploy: auto-deploys on push to `main` via GitHub integration. **Do NOT run `vercel --prod` from a feature branch** — it overwrites production with whatever is in your local working tree, including routes/files not in `main`. This shipped a `/template/[id]` page without its backing API endpoint, crashing the global error boundary on every template click. If you need an emergency manual deploy, `git checkout main && git pull` first, then `cd src/apps/web && vercel --prod`.
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

## Paperclip Agent Protocol

If you are running as a Paperclip agent (checked out via `paperclipai checkout`), follow this protocol strictly.

### 1. Identify your role and phase

Check your agent ID to determine your role. Read the issue title prefix to know your phase.

| Agent ID (first 8) | Role | Gstack Skill by Phase |
|---|---|---|
| `dab11201` | CEO | `/plan-ceo-review` |
| `1741ddd3` | CTO | ARCHITECT: `/plan-eng-review` or `/investigate` · REVIEW: `/review` |
| `b8efd434` | Frontend Eng | IMPLEMENT: code · SHIP: `/ship` |
| `3f30b797` | Backend Eng | IMPLEMENT: code · SHIP: `/ship` |
| `4b21e253` | Head of Design | `/plan-design-review` or `/design-consultation` |
| `35cc54f8` | DevOps Lead | `/land-and-deploy` then `/canary` then stack verification |
| `e73f2cd9` | QA Engineer | `/qa` then pipeline test with real videos |

### 2. Run your gstack skill

Your gstack skill IS your workflow. Run it — don't do the work ad-hoc.

### 3. Branch isolation

**Always create a feature branch before writing any code:**
```bash
git checkout main && git pull
git checkout -b nov-<issue-number>/<short-description>
```
If a prior phase already created a branch (check the issue description), use that branch instead.

### 4. Do ONLY your phase

**You execute YOUR phase only.** Do not investigate, implement, review, test, ship, AND deploy. Just do your one phase.

When done:

1. Mark your issue as `done`:
   ```bash
   curl -s -X PATCH "http://127.0.0.1:3100/api/issues/<your-issue-id>" \
     -H "Content-Type: application/json" -d '{"status": "done"}'
   ```
2. Add a completion comment describing what you did
3. **Advance the chain** — find the next issue and move it from `backlog` to `todo` (see below)

### 5. Pre-created chain — advance, don't create

**All issues in the chain are pre-created.** You do NOT need to create the next issue. It already exists with status `backlog`.

**When you finish your phase, find the next issue and set it to `todo`:**

```bash
# List backlog issues to find the next one in the chain
curl -s "http://127.0.0.1:3100/api/companies/585add97-6824-4df2-8b39-5bbc6150829e/issues?status=backlog" \
  | python3 -c "import sys,json; [print(f'{i[\"identifier\"]}: {i[\"title\"]}') for i in json.load(sys.stdin)]"

# Move the next issue to todo
curl -s -X PATCH "http://127.0.0.1:3100/api/issues/<next-issue-id>" \
  -H "Content-Type: application/json" -d '{"status": "todo"}'
```

**How to identify the next issue:**
- Issues in the same chain share a `parentId` or have sequential identifiers (NOV-20, NOV-21, NOV-22...)
- The title prefix tells you the phase: ARCHITECT → IMPLEMENT → REVIEW → QA → SHIP → DEPLOY
- Find the backlog issue whose phase comes after yours

**If no next issue exists in backlog** (edge case — the chain wasn't pre-created):
Create it using the phase chain table:

| After your phase... | Next phase | Assign to |
|---|---|---|
| PLAN | ARCHITECT | CTO `1741ddd3-174c-42e0-85b4-fde5ba7fec48` |
| DESIGN | ARCHITECT | CTO `1741ddd3-174c-42e0-85b4-fde5ba7fec48` |
| ARCHITECT | IMPLEMENT | Backend `3f30b797-86b7-4ce9-8fec-6e42dbbca247` or Frontend `b8efd434-ef92-4f55-99e2-6d85e0a3df8e` |
| IMPLEMENT | REVIEW | CTO `1741ddd3-174c-42e0-85b4-fde5ba7fec48` |
| REVIEW | QA | QA `e73f2cd9-6717-4eff-8b05-b339e6223ac1` |
| QA | SHIP | Backend `3f30b797-86b7-4ce9-8fec-6e42dbbca247` |
| SHIP | DEPLOY | DevOps `35cc54f8-3ec8-4831-83ac-3b37445e38f4` |
| DEPLOY | — | Chain complete |

### 6. Pipeline-specific rules

- Read `agents/VIDEO_CONTEXT.md` before any pipeline work
- Do NOT use MoviePy / VideoFileClip — use subprocess FFmpeg
- QA Engineer: after `/qa`, also test with real videos from `~/Downloads` (find mp4/mov files, upload, create job, verify output with ffprobe)
- DevOps Lead: after deploy, run full stack verification (API health, Fly.io processes, DB, Redis, Celery, GCS, Gemini, templates, frontend)

### 7. Full playbook reference

For detailed playbooks per goal type, read:
`/Users/emirerben/.openclaw/workspace/startups/nova/PAPERCLIP.md`

## gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Install (per developer):
```
git clone --single-branch --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack && cd ~/.claude/skills/gstack && ./setup
```

Available gstack skills:
- `/office-hours`
- `/plan-ceo-review`
- `/plan-eng-review`
- `/plan-design-review`
- `/design-consultation`
- `/design-shotgun`
- `/design-html`
- `/review`
- `/ship`
- `/land-and-deploy`
- `/canary`
- `/benchmark`
- `/browse`
- `/connect-chrome`
- `/qa`
- `/qa-only`
- `/design-review`
- `/setup-browser-cookies`
- `/setup-deploy`
- `/setup-gbrain`
- `/retro`
- `/investigate`
- `/document-release`
- `/codex`
- `/cso`
- `/autoplan`
- `/plan-devex-review`
- `/devex-review`
- `/careful`
- `/freeze`
- `/guard`
- `/unfreeze`
- `/gstack-upgrade`
- `/learn`
