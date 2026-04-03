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
- src/apps/api/  — Python API (upload endpoint, job queue, FFmpeg pipeline)
- src/apps/api/prompts/ — LLM prompt templates (template analysis, transcription)
- agents/        — project-level agent context (VIDEO_CONTEXT.md, STACK.md, DECISIONS.md)

## Local dev
cp .env.example .env    # fill in values
docker-compose up        # starts web + api + worker + redis + db

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

## Template pipeline
- Interstitials: `app/pipeline/interstitials.py` detects curtain-close vs fade-to-black via FFmpeg luminance band analysis, renders color holds and `geq` pixel-expression curtain-close animations (drawbox cannot animate bar height over time)
- Transitions: `app/pipeline/transitions.py` translates Gemini vocabulary (whip-pan, zoom-in, dissolve) to internal FFmpeg xfade types
- Font bundle: Playfair Display (Bold + Regular) in `assets/fonts/`, referenced via `fontsdir` in ASS subtitle filters
- Text overlays: `app/pipeline/text_overlay.py` renders gaussian-shadow text (no hard outlines), supports font-cycle and ASS animated overlays
- Font-cycle: acceleration syncs with curtain-close (`font_cycle_accel_at_s`), `text_color` passthrough for colored text, per-size font caching in `_resolve_cycle_fonts()`, `MAX_FONT_CYCLE_FRAMES` (60) safety cap prevents PNG explosion, gap-fill PNG bridges frame cap to cycle_end
- Font-cycle settle: when `font_cycle_accel_at_s` is active, settle phase is skipped entirely (cycling runs to end_s)
- Cross-slot text merge: `_collect_absolute_overlays()` merges same-text+same-position overlays across adjacent slots when gap < `_MERGE_GAP_THRESHOLD_S` (2.0s), inheriting effect and accel from later slots
- Curtain-close minimum: `MIN_CURTAIN_ANIMATE_S=1.0` enforced at both `_assemble_clips` and `_collect_absolute_overlays` call sites (0.5s was too fast to perceive)
- Beat-snap: `cumulative_s` in `_assemble_clips()` must account for interstitial hold durations to keep beat-snap calculations accurate
- Timing: `_burn_text_overlays()` must not reassign font-cycle multi-PNG timestamps with single overlay timestamps (bug fixed in v0.1.1.0)

## Env vars needed (see .env.example for full list with descriptions)
- STORAGE_BUCKET, STORAGE_PROVIDER
- REDIS_URL
- DATABASE_URL
- OPENAI_API_KEY

## Deploy Configuration (configured by /setup-deploy)
- Platform: Fly.io
- App name: nova-video
- Region: iad
- Production URL: https://nova-video.fly.dev
- Deploy workflow: `fly deploy` from repo root (or GitHub Actions CD)
- Deploy status command: `fly status --app nova-video`
- Merge method: squash
- Project type: web app + API + background workers
- Process groups: api (FastAPI/uvicorn) + worker (Celery)
- Release command: `python -m alembic upgrade head` (runs migrations on every deploy)
- VM sizing: api = 1 shared CPU / 512MB, worker = 2 shared CPUs / 2048MB
- Dockerfile: repo-root `Dockerfile` (cached dependency layer from pyproject.toml)
- Docker image includes: `app/`, `assets/`, `prompts/`, `alembic.ini`

### Custom deploy hooks
- Pre-merge: none
- Deploy trigger: `fly deploy` (manual) or GitHub Actions (after CD workflow added)
- Deploy status: `fly status --app nova-video`
- Health check: https://nova-video.fly.dev/health

### Secrets (set via `fly secrets set`)
Required before first deploy:
```bash
fly secrets set -a nova-video \
  DATABASE_URL="..." \
  REDIS_URL="..." \
  STORAGE_BUCKET="..." \
  STORAGE_PROVIDER="..." \
  GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}' \
  OPENAI_API_KEY="..." \
  GEMINI_API_KEY="..."
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
