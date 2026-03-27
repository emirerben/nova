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
- Font-cycle: acceleration syncs with curtain-close (`font_cycle_accel_at_s`), `text_color` passthrough for colored text, per-size font caching in `_resolve_cycle_fonts()`, `MAX_FONT_CYCLE_FRAMES` (60) safety cap prevents PNG explosion
- Beat-snap: `cumulative_s` in `_assemble_clips()` must account for interstitial hold durations to keep beat-snap calculations accurate
- Timing: `_burn_text_overlays()` must not reassign font-cycle multi-PNG timestamps with single overlay timestamps (bug fixed in v0.1.1.0)

## Env vars needed (see .env.example for full list with descriptions)
- STORAGE_BUCKET, STORAGE_PROVIDER
- REDIS_URL
- DATABASE_URL
- OPENAI_API_KEY

## Deploy Configuration (configured by /setup-deploy)
- Platform: Fly.io (planned, not yet configured)
- Production URL: TBD (update after `fly launch`)
- Deploy workflow: auto-deploy on push to main (via Fly.io GitHub integration)
- Deploy status command: `fly status --app <app-name>`
- Merge method: squash
- Project type: web app + API + background workers

### Custom deploy hooks
- Pre-merge: none
- Deploy trigger: automatic on push to main (Fly.io GitHub integration)
- Deploy status: `fly status --app <app-name>` (after Fly CLI installed)
- Health check: `https://<app-name>.fly.dev` (after deploy configured)

### Setup remaining
1. Install Fly CLI: `curl -L https://fly.io/install.sh | sh`
2. Create Fly app: `fly launch` (generates fly.toml + Dockerfile)
3. Connect GitHub: `fly deploy` or Fly.io dashboard → GitHub integration
4. Update this section with real app name and production URL
