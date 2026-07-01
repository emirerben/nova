# Nova

Nova transforms raw real-life videos into viral short-form content (TikTok, Reels, Shorts).

## CLAUDE.md size budget

Hard budget: **38,000 chars**, enforced by CI (`scripts/check_claude_md_size.sh`).

**Pattern:** keep inline → invariants, guard-test names, run commands, file pointers.
Move out → incident narratives (prod job IDs, multi-paragraph "why") →
`agents/DECISIONS.md`; feature internals → `docs/pipelines/` or `docs/runbooks/`.

When the CI guard fails your PR: move narrative out, don't fight the check.

## Session workflow: isolate in a worktree

**Before starting non-trivial work, create a fresh worktree off `origin/main`.** The primary checkout at `/Users/emirerben/Projects/nova` is shared across sessions; uncommitted edits collide and the checkout stays pinned to whatever branch was last left there. Symptoms: stray `.venv` deletions, duplicate migration files, mixed `git status`, PRs with phantom conflicts because work landed against a stale base.

Fastest path — `nova-fresh <topic>` (zsh function in `~/.zshrc`) creates the worktree off `origin/main`, `cd`s into it, and launches Claude there in one step:

```bash
nova-fresh template-text-100             # = new-session.sh + cd + claude
```

Or run the underlying script yourself (fetch origin/main + worktree off the fresh tip + correct branch naming):

```bash
bash scripts/new-session.sh <topic>      # e.g. template-text-100
cd ../nova-<topic>                       # script prints this; copy it
```

A `SessionStart` hook (`.claude/settings.json` → `scripts/session-check.sh`) fetches `origin/main`, then: on a **clean `main`** it auto fast-forwards (so the shared checkout never drifts); on a dirty `main` or any feature branch it only prints a warning (it can't relocate the session or discard work). If you see the stale-worktree warning, run `nova-fresh`/`new-session.sh` for new work, or `git merge --ff-only origin/main` to update an in-flight branch.

Rules:
- Run all edits, tests, and commits from the worktree path — never from `/Users/emirerben/Projects/nova` directly.
- One worktree per logical change. Don't reuse a worktree for an unrelated feature.
- When done, `git worktree remove ../nova-<topic>` after the PR merges. List active worktrees with `git worktree list`.
- `.claude/worktrees/agent-*` are auto-managed by the Agent tool (`isolation: "worktree"`) — leave those alone.
- Skip the worktree only for read-only investigation or single-line config tweaks confined to one file. The session-check warning still applies.

## Stack
- Frontend: Next.js (src/apps/web/) — TypeScript, React
- Backend: Python FastAPI + Celery (src/apps/api/) — video processing pipeline
- Queue: Redis (job queue for async processing)
- Storage: GCS or S3 — raw uploads + processed outputs NEVER in git
- DB: PostgreSQL (job metadata, user state)

## Key paths
- `DESIGN.md` — design-system source of truth (tokens, loading rules, anti-slop, a11y); design reviews calibrate against it.
- `src/apps/web/` — Next.js frontend (upload UI, progress tracker, result viewer)
- `src/apps/web/src/app/admin/templates/[id]/components/` — visual overlay editor (OverlayPreview, OverlayTimeline, PropertyPanel, overlay-constants.ts)
- `src/apps/web/src/app/music/` — music gallery page
- `src/apps/web/src/app/admin/music/` — admin music management; `/admin/music/[id]` Config + Test tabs
- `src/apps/web/src/lib/music-api.ts` — typed API client for music routes
- `src/apps/api/` — Python API (upload endpoint, job queue, FFmpeg pipeline)
- `src/apps/api/app/routes/admin_music.py` — music CRUD + publish/reanalyze + Test tab endpoints (test-job, rerender-job, status, test-jobs list); `clip_gcs_paths` allowlisted to `music-uploads/` and `slot-uploads/` prefixes
- `src/apps/api/app/routes/music.py` — public music-track gallery endpoint
- `src/apps/api/app/routes/music_jobs.py` — beat-sync job submission + status; `_validate_clip_count` is public so `admin_music.py` can reuse it
- `src/apps/api/app/routes/generative_jobs.py` — generative-edit submission + status + swap-song/retext; re-signs ready variant URLs on read (`_variants_for_response`)
- `src/apps/api/app/routes/admin_generative.py` — `/admin/generative` dashboard list
- `src/apps/api/app/tasks/generative_build.py` — `orchestrate_generative_job` Celery task (see `docs/pipelines/generative.md`)
- `src/apps/api/app/pipeline/generative_overlays.py` — agent-authored intro overlay injector
- `src/apps/web/src/app/generative/` + `admin/generative/` — public generative UI + admin dashboard
- `src/apps/api/app/pipeline/music_recipe.py` — beat-snap recipe generator (see `docs/pipelines/music.md`)
- `src/apps/api/app/tasks/music_orchestrate.py` — Celery tasks: beat analysis + music job orchestration
- `src/apps/api/app/services/audio_download.py` — yt-dlp audio download + beat detection via FFmpeg
- `src/apps/api/app/services/seed_provenance.py` — token-set matcher (`match_specs_to_seeds`) that links generated plan items back to the idea seed they honour; called at plan-generation time to set `PlanItem.source_idea_seed_id` and flip matched seeds to `in_plan`
- `src/apps/web/src/app/plan/_components/ui/SeedProvenanceBadge.tsx` — "From your idea" lime badge rendered on TodayCard, PlanItemCard, and item detail page; driven by `source_idea_seed_text` from the API
- `src/apps/api/prompts/` — LLM prompt templates (template analysis, transcription)
- `agents/` — project-level agent context (VIDEO_CONTEXT.md, STACK.md, DECISIONS.md)
- `plans/` — DONE implementation plans from the June 12 audit (`plans/README.md` has execution order + status; 001–004 all shipped)

## Local dev
```bash
cp .env.example .env            # fill in values
./scripts/dev-auto.sh            # single-command dev env with hot reload
```

`dev-auto.sh` starts redis + postgres via `docker-compose up -d redis db` (infra only), runs migrations, then launches API (`uvicorn --reload`), Celery worker (`watchfiles`), and Next.js (HMR) natively. Logs go to `.dev/<service>.log`. Do NOT restart servers manually — hot reload handles all code edits. Stop with `./scripts/dev-stop.sh`.

Alternatives:
- `./scripts/dev-no-docker.sh` — same flow but assumes postgres@16 + redis already running
- `docker-compose up` — full stack in containers; rarely needed since native is faster

Production uses repo-root `Dockerfile` (Python 3.11 + FFmpeg/libheif/libmagic) deployed to Fly.io. Note: `src/apps/api/Dockerfile` and `src/apps/web/Dockerfile` are only referenced by docker-compose.

### Local sign-in (dev-login)
The content-plan / generative flows are Google-gated. To sign in on localhost without Google consent, the repo ships a `dev-login` NextAuth provider (`src/apps/web/src/lib/auth.ts`) gated behind `ALLOW_DEV_LOGIN=true`. Because `dev-auto.sh` sources the repo-root `.env`, setting `ALLOW_DEV_LOGIN=true` + `INTERNAL_API_KEY=<any-string>` there (see `.env.example`) makes dev-login work in **every worktree** automatically — no per-worktree setup. Sign in at `http://localhost:3000/api/auth/signin` → "Dev login (local only)" → any email. Both the API and web must read the same `INTERNAL_API_KEY` (sourcing root `.env` guarantees this); the API fail-closes (401 on plan routes) if it's unset. NEVER set `ALLOW_DEV_LOGIN` in Vercel/Fly. If you run web/API by hand (not via `dev-auto.sh`), remember Next.js loads env from `src/apps/web/`, not repo root — so `source ../../../.env` into the launch shell.

### Local-render parity (high-fidelity, slow iteration)

Use `make local-render` when you need byte-equivalent output before merging a render-affecting change — it runs the prod Docker image with matching FFmpeg and fonts. See `docs/runbooks/local-render.md` for full usage, divergence sources, and cache-busting instructions.

```bash
cp .env.local-render.example .env.local-render
make local-render CLIP=/path/to/clip.mp4 TEMPLATE=<uuid> [MODE=template|music]
make local-render MODE=generative CLIPS="a.mp4 b.mp4 c.mp4"
```

## Quality checks
- Backend tests: `cd src/apps/api && pytest` (asyncio mode auto; tests live in `src/apps/api/tests/`)
- Backend lint: `cd src/apps/api && ruff check . && ruff format --check .`
- Frontend lint: `cd src/apps/web && npm run lint`
- Frontend typecheck: `cd src/apps/web && npx tsc --noEmit`
- Frontend tests: `cd src/apps/web && npm test` (Jest)

## Admin API access (for automation / Claude Code)
Use `scripts/admin.py` instead of curling `/admin/*` with a raw token — the token stays in `.env`, never in commands or transcripts.

```bash
python scripts/admin.py GET  /admin/templates                                 # local
python scripts/admin.py GET  templates/abc123/debug                           # /admin/ auto-prefixed
python scripts/admin.py POST templates/abc/reanalyze-agentic --json '{"use_layer2": true}'
python scripts/admin.py --prod GET /admin/templates                           # Fly prod
python scripts/admin.py --prod POST templates/abc/publish                     # prompts y/N
```

- Local uses `ADMIN_API_KEY`, `--prod` uses `ADMIN_PROD_API_KEY` (both in repo-root `.env`; example slots in `.env.example`).
- Any `POST/PUT/PATCH/DELETE` against `--prod` prompts `Proceed? [y/N]` — pass `--yes` to skip in scripts you've reviewed.
- `--dry-run` prints the resolved request without sending. `-v` shows headers (token always redacted).
- Exits 0 on 2xx, 1 otherwise — safe in shell pipelines.
- Stdlib-only (no `requests`/`dotenv`) so it runs with any Python 3 outside the API venv.

## Domain context
- Target output: 9:16 aspect ratio, sub-60s, H.264/AAC, 1080x1920
- Hook window: first 2-3 seconds must create a question in the viewer's mind
- Processing is ASYNC — a 5-min source video takes 2-5 min to process
- Jobs polled via GET /jobs/:id/status or websocket
- Raw uploads and processed outputs are NEVER committed to git
- Read agents/VIDEO_CONTEXT.md for full video domain context and FFmpeg patterns
- Read agents/DECISIONS.md for why key choices were made

## Storage retention
- Per-job GCS objects (`dev-user/*`, `music-jobs/*`, `music-lyrics-previews/*`) are deleted by a bucket lifecycle rule 24h after upload. Config lives at `infra/gcs-lifecycle.json`; apply once with `gsutil lifecycle set infra/gcs-lifecycle.json gs://$STORAGE_BUCKET` (not part of CI deploy).
- Curated assets (`music/*`, `templates/*`) are NOT matched by the rule and persist forever.
- Signed-URL TTL in `storage.py` is 1 day to match the object lifetime.
- **`generative-jobs/*` exception:** blobs persist forever but `upload_public_read` signs `output_url` for only 1 day → expired URLs show blank video after 24h. Fix is read-time re-signing via `_variants_for_response` in `routes/generative_jobs.py` (`PLAYBACK_URL_TTL_MIN`). Pinned by `test_variants_for_response_resigns_ready_variant`. See agents/DECISIONS.md "Storage retention incidents" for the full narrative.
- `JobClip.storage_expires_at` is informational only — no sweeper reads it.
- **When auth/login lands:** revisit `infra/gcs-lifecycle.json`. Put authenticated-user content under a new prefix (e.g. `users/{user_id}/`) that the delete rule does NOT match.

## ⚠️ Anti-pattern: do NOT use MoviePy / VideoFileClip
VideoFileClip(path) buffers the entire video into RAM. On a 2GB source file this crashes.
Use subprocess FFmpeg directly. See agents/VIDEO_CONTEXT.md for patterns.

## Music beat-sync pipeline
- Track lifecycle: `pending` → `analyzing` → `ready` | `failed`; only `ready`+`published` tracks appear in the public gallery
- Beat detection + best-section auto-select: `_detect_audio_beats()` + `_auto_best_section()` in `audio_download.py`. Tracks with 0 detected beats are marked `failed`.
- Recipe generation: `generate_music_recipe()` in `music_recipe.py` (slot layout from beats)
- Job orchestration: `orchestrate_music_job` — parallel Gemini clip analysis → `template_matcher.match` → `_assemble_clips` with beat-snap → `_mix_template_audio`
- Output URL contract: `_run_music_job` / `_run_templated_music_job` / `orchestrate_auto_music_job` persist the **signed URL** from `upload_public_read` into `assembly_plan.output_url` — NOT the relative GCS path.
- Clip count: `slot_count` returned by `/music-tracks` tells the frontend how many clips to collect; `POST /music-jobs` validates the count matches.
- Auto-classification: `song_classifier` runs after beat analysis and persists `MusicLabels` on `MusicTrack.ai_labels`. Classifier failure is non-fatal.
- Auto-matching: `music_matcher` (Gemini Flash) ranks the full library against a clip set using `MusicLabels`.
- Song-sections visualizer: `MusicTrack.best_sections` + `section_version` rendered as a ranked band SVG at `/admin/music/[id]`. `src/lib/music-api.ts` carries a hand-mirrored `SongSection` interface — keep its literal unions in sync when the Pydantic schema changes.
- See `docs/pipelines/music.md` for internals (beat detection algo, recipe slice math, admin proxy, clip validation detail).

## Generative-edits pipeline
- NO reference template and NO pre-selected song: the user uploads clips; `orchestrate_generative_job` analyzes them, auto-matches a track, writes its own intro overlay, and renders THREE variants: `song_lyrics`, `song_text`, `original_text`.
- **Best-effort by design:** if no labeled track matches, song variants are skipped and `original_text` still renders — a generative job never hard-fails on an empty library.
- Per-variant state is authoritative in `Job.assembly_plan["variants"]` (the task owns it) — no new DB column. Jobs are plain `Job` rows with `mode == "generative"`.
- **Intro-text persistence:** rendered intro text + highlight word are stored on `variants[i]["intro_text"]` / `["intro_highlight_word"]`. On re-render without override, persisted text is reused (no LLM). intro_writer only runs on first render or legacy variants.
- **Fast reburn base:** for `agent_text` montage variants, the audio-mixed text-free base is stored at `variants[i]["base_video_path"]` (GCS key). Font/text/size edits download the base and reburn only the overlay (seconds). Kill switch: `GENERATIVE_FAST_REBURN_ENABLED=false` + worker restart.
- See `docs/pipelines/generative.md` for how the music engine is reused and per-variant mechanics.

## Template pipeline
- **Single-pass CFR-before-xfade invariant:** every per-clip chain in `app/pipeline/single_pass.py` (`_per_clip_filter_chain`) must end with `fps={output_fps}, setpts=PTS-STARTPTS, settb=AVTB` before its labelled output. The trailing `fps=` filter is PTS-independent and handles `avg_frame_rate=1/0` inputs (some phone HEVC, HEIF-derived video, screen recordings) where `framerate=fps=N` aborts. Locked by `test_per_clip_chain_forces_cfr_before_xfade` in `tests/pipeline/test_single_pass.py`. See agents/DECISIONS.md (2026-05-18).
- **Renderer split:** agentic templates + music-job lyrics → `text_overlay_skia.py` (skia-python, HarfBuzz shaping, per-frame PNG sequences); classic non-music + non-agentic → Pillow + libass. Dispatch: `_burn_text_overlays(use_skia=...)` + `_pre_burn_curtain_slot_text(is_agentic=...)` in `template_orchestrate.py`. Kill switch: `TEXT_RENDERER_SKIA_ENABLED=false`.
- **Renderer-parity invariant:** any overlay field plumbed through the burn dict MUST be honored by BOTH renderers. Guard: `test_both_renderers_honor_text_anchor_left` in `tests/pipeline/test_text_overlay_skia.py` — extend for any new anchor/position field. An agentic/music overlay change is NOT verified by the admin preview. See agents/DECISIONS.md (2026-05 #296/#297) for the incident.
- **Pre-PR text-overlay verify (`make verify-overlays`):** before any PR touching the Skia renderer, overlay layout, or the burn dict. Renders through the REAL Skia path in the prod Docker image, asserts each overlay is un-clipped (opaque-pixel bbox vs frame edges), writes `montage.png` + `report.json` to `.overlay-verify/`. Library: `app/pipeline/overlay_verify.py`; CLI: `app.cli.verify_overlays`; guard: `tests/pipeline/test_overlay_verify.py`. **Agent pre-PR gate:** run it, read `report.json` + `montage.png`, fix any FAIL, then `/ship`.
- **Gemini metadata never becomes on-screen overlay text** (architectural invariant). Overlay substitution input is exclusively user-provided (`inputs.location`). `TestNoGeminiTextLeaks` in `tests/tasks/test_template_orchestrate.py` is the sentinel. See agents/DECISIONS.md (2026-05-13). Does NOT cover `copy_writer` captions.
- Placeholder substitution: `_resolve_overlay_text()` + `_is_subject_placeholder()` in `template_orchestrate.py`. Literal overlays SHOULD set `"subject_substitute": False`; `tests/scripts/test_seed_overlays_literal.py` enforces this.
- Font bundle: Playfair Display (Bold + Regular) in `assets/fonts/`, referenced via `fontsdir` in ASS subtitle filters.
- See `docs/pipelines/template.md` for internals: font-cycle mechanics, cross-slot text merge, curtain-close minimum, label config, clip rotation, timing details, agentic pct-timing.

## Encoder policy (libx264 preset)
- **Intermediate encodes** → `preset="ultrafast"` is fine. Call sites: `reframe_and_export`, `_build_overlay_cmd` in `reframe.py`; `render_color_hold` in `interstitials.py`; `image_clip` rendering; `drive_import` thumbnailing.
- **Final-output encodes** → `preset="fast"` or stricter REQUIRED. `ultrafast` causes visible 16×16 macroblocking on smooth gradients (sky, dark canopy); CRF does NOT compensate. Call sites: `_concat_demuxer` fallback, `_pre_burn_curtain_slot_text`, `_burn_text_overlays` in `template_orchestrate.py`; `apply_curtain_close_tail` in `interstitials.py`; `join_with_transitions` in `transitions.py`.
- Locked by `tests/test_encoder_policy.py` — adding a new `_encoding_args(...)` call site forces a conscious quality-budget decision. See agents/DECISIONS.md for the history (PR #102/#105 + Brazil pixelation fix).

## Env vars needed (see .env.example for full list with descriptions)
- STORAGE_BUCKET, STORAGE_PROVIDER
- REDIS_URL
- DATABASE_URL
- OPENAI_API_KEY
- GEMINI_API_KEY — required for clip analysis (music jobs) and template analysis
- `ORIENTATION_NORMALIZE_ENABLED` — defaults to `true`. Set to `false` and restart workers to make `normalize_orientation` a no-op (safety valve for orientation regressions). Apply: `fly secrets set ORIENTATION_NORMALIZE_ENABLED=false --app nova-video` + `fly machine restart <id>`.
- `LYRIC_DYNAMIC_CROSSFADE_ENABLED` — defaults to `true`. Set to `false` to roll back to legacy `_inject_line` behavior byte-identically. **WARNING: disabling re-introduces the stacked-text bug from prod jobs `5a71226e`/`e72d52e9` — use ONLY for emergency rollback.** Kill-switch test: `tests/pipeline/test_lyric_injector_no_stacking.py::test_kill_switch_disabled_reproduces_pre_fix_output`. Apply: `fly secrets set LYRIC_DYNAMIC_CROSSFADE_ENABLED=false --app nova-video` + `fly machine restart <id>`. See agents/DECISIONS.md "Kill-switch incidents" for the full warning.
- `TIKTOK_DEEP_ANALYSIS_ENABLED` — defaults to `true`. When false, `scrape_tiktok_profile` skips chaining `analyze_tiktok_profile` and persona/plan/hook prompts receive no TikTok analysis block (byte-identical to pre-feature). Apply: `fly secrets set TIKTOK_DEEP_ANALYSIS_ENABLED=false --app nova-video` + `fly machine restart <id>` (no deploy needed).
- `NARRATIVE_CLIP_ORDER_ENABLED` — defaults to `true`. Plan-item edits follow the filming guide's shot order (narrative mode in `template_matcher.match`; dispatch contract in `content_plan_build._narrative_clip_order`). Read at render time → flipping affects queued jobs and re-renders. Set to `false` to fall back to pure greedy matching. Apply: `fly secrets set NARRATIVE_CLIP_ORDER_ENABLED=false --app nova-video` + `fly machine restart <id>`.
- `EDITORIAL_SEQUENCE_ENABLED` — defaults to `true`. Editorial (cluster) variants with audible, coherent original speech render the transcript-synced typographic sequence (`docs` in plan; pipeline: `phrase_sequence.py` + `SequenceEmphasisAgent` + `EDITORIAL_STYLE`); without eligible speech (incl. song/voiceover variants) RHYTHM MODE paces an agent-authored quote instead (`SequenceQuoteWriterAgent` + `rhythm_scenes`; persisted as `sequence_quote`/`sequence_mode`; agent failure ⇒ static cluster, never a heuristic quote). `false` ⇒ legacy single static cluster, byte-identical, no quote agent. Guards: `tests/tasks/test_generative_build_sequence.py` kill-switch tests. Apply: `fly secrets set EDITORIAL_SEQUENCE_ENABLED=false --app nova-video` + `fly machine restart <id>`.
- `SOUND_EFFECTS_ENABLED` / `MEDIA_OVERLAYS_ENABLED` — both default **`false`**. Gate the SFX-lane and overlay-lane write/render routes in `routes/plan_items.py` (404 when off); the public `GET /sound-effects` list is NOT gated, so the picker still loads. **Dual-flag trap:** the frontend lanes have SEPARATE flags (`NEXT_PUBLIC_SOUND_EFFECTS_ENABLED` / `NEXT_PUBLIC_MEDIA_OVERLAYS_ENABLED` in Vercel) — if the frontend flag is on but the backend one is off, saves 404 and the UI now surfaces an error (was silently swallowed pre-fix). Keep Fly + Vercel in sync. Apply: `fly secrets set SOUND_EFFECTS_ENABLED=true --app nova-video` + `fly machine restart <id>` (api + worker).
- `SUBTITLED_ARCHETYPE_ENABLED` — defaults to **`false`**. Gates the subtitled single-clip edit style (talk-to-camera clip → auto-detected-language captions, editable + reburnable, sentence-per-cue with pop-in; TR/EN). When off, a `subtitled` job falls back to montage. Dual-flag with `NEXT_PUBLIC_SUBTITLED_ENABLED` (Vercel picker card) — keep Fly + Vercel in sync. Companions: `SUBTITLED_CAPTION_CORRECTION_ENABLED` (default `true` — LLM pass fixing whisper mishearings per cue, timing preserved) + `CAPTION_CORRECTION_MODEL` (default `gpt-4o`; mini missed Turkish case errors 4/4 runs). Apply: `fly secrets set SUBTITLED_ARCHETYPE_ENABLED=true --app nova-video` + `fly machine restart <id>` (api + worker).

## Agent evals
- Per-agent quality eval harness lives at `src/apps/api/tests/evals/`. Covers the Big 5 (`template_recipe`, `clip_metadata`, `creative_direction`, `song_classifier`, `music_matcher`) plus the in-pipeline `transcript`, `platform_copy`, `audio_template`, and `template_text` agents.
- Default: `cd src/apps/api && pytest tests/evals/ -v` — structural-only, replay mode, no network. Runs in CI.
- With judge: `... --with-judge` (needs `ANTHROPIC_API_KEY`). Live Gemini: `NOVA_EVAL_MODE=live ... --eval-mode=live --with-judge` (~$2-5/run).
- **Prompt-change rule:** when editing any file under `src/apps/api/prompts/` or any `render_prompt()`, bump the agent's `prompt_version` in its `AgentSpec` AND run live evals against current fixtures before merge.
- **template_text live-eval wrapper:** `bash src/apps/api/scripts/run_template_text_eval.sh`. See `tests/evals/README.md` for the full prompt-iteration loop.
- **Layer-2 cache-bump rule:** any PR touching `text_overlay_v2/`, the Stage E/F agents/schemas, or their prompts must bump `TEXT_OVERLAY_VERSION_V2` in `template_cache.py`. Guard: `.github/workflows/layer2-cache-guard.yml`. Escape hatch: `[skip-layer2-cache-bump]` in a commit message.
- See `docs/pipelines/layer2-text-overlay.md` for Layer-2 stage details, OCR backend divergence (local Apple Vision ≠ prod Cloud Vision), and the template_text agent rubric.

## Admin job-debug view
- Surfaces every agent's full I/O + every non-LLM pipeline decision per job. Lives at `/admin/jobs` (list) and `/admin/jobs/{id}` (detail).
- **Mandatory orchestrator contract:** every Celery task that drives agents must wrap its body in `with pipeline_trace_for(job_id):`. Currently applied in `orchestrate_music_job`, `orchestrate_template_job`, `orchestrate_auto_music_job`, `orchestrate_generative_job`. New orchestrators MUST do the same or `record_pipeline_event` calls silently drop.
- **Success-outcome set:** `app/agents/_runtime.SUCCESS_OUTCOMES` is the single source of truth for which agent outcomes count as success. Any SQL filter or UI label that distinguishes pass/fail MUST import this constant.
- See `docs/runbooks/admin-job-debug.md` for storage layer details, event names, template-scoped sibling, and the eval harness opt-out flag.

## Deploy Configuration

### Architecture: Split Deploy
- **Frontend (Next.js)** → Vercel (CDN edge, preview deploys, auto-deploy on push)
- **API + Workers (FastAPI + Celery)** → Fly.io (long-running FFmpeg jobs, up to 18-20 min)

### Vercel — Frontend (`src/apps/web/`)
- Project: `emirerbens-projects/nova`
- Production URL: https://nova-video.vercel.app
- Framework: Next.js (auto-detected)
- Root directory: `src/apps/web/`
- Deploy: auto-deploys on push to `main` via GitHub integration. **Do NOT run `vercel --prod` from a feature branch** — it pushes your local working tree to production. For an emergency manual deploy: `git checkout main && git pull`, then `cd src/apps/web && vercel --prod`.
- Env vars: set via `vercel env` CLI (NEXT_PUBLIC_API_URL, NEXT_PUBLIC_WS_URL, NEXT_PUBLIC_DEFAULT_TEMPLATE_ID, NEXT_PUBLIC_GOOGLE_CLIENT_ID, NEXT_PUBLIC_GOOGLE_PICKER_API_KEY, NEXTAUTH_SECRET, ADMIN_BASIC_AUTH_USER, ADMIN_BASIC_AUTH_PASSWORD)
- **`ADMIN_BASIC_AUTH_USER` + `ADMIN_BASIC_AUTH_PASSWORD` are MANDATORY.** They gate `/admin/*` and `/api/admin/*` via `src/apps/web/src/middleware.ts`. Without them, every admin page returns 503 — fail-closed by design.
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
- VM sizing: api = 1 shared CPU / 512MB, worker = 4 shared CPUs / 6144MB
- **Celery time-limit invariant:** every long-running task's `time_limit` MUST stay strictly under the worker's broker `visibility_timeout` (`app/worker.py`, currently 1900s). Render orchestrators use `soft_time_limit=1740, time_limit=1800`. Locked by `tests/tasks/test_task_time_limits.py`. See agents/DECISIONS.md "Celery time-limit invariant" for the prod incident.
- Dockerfile: repo-root `Dockerfile` (cached dependency layer from pyproject.toml)
- Docker image includes: `app/`, `assets/`, `prompts/`, `alembic.ini`
- CORS: `ALLOWED_ORIGINS` env var — JSON array format

### Custom deploy hooks
- Pre-merge: none
- Deploy trigger (API): `fly deploy` (manual) or GitHub Actions
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

## Agentic workflow (how to work fast here)

- **Default to subagents, not new sessions.** Spawn a subagent (Agent tool) per heavy subtask from ONE orchestrating session. Each subagent burns its own context window and returns only a summary. Do NOT open a new session per subtask.
- **Parallelize independent subtasks** in one message (multiple Agent calls). Use `isolation: "worktree"` on subagents that edit files in parallel.
- **For batchable work** across N items, run the decompose workflow: `Workflow({ scriptPath: ".claude/workflows/decompose.js", args: { subtasks: [{title, prompt}, ...] } })`. Running ANY workflow needs explicit opt-in — include the word "workflow" in the request.
- **Prefer gbrain over grep for semantic lookups.** `gbrain search "<intent>"`, `gbrain code-def <symbol>`, `gbrain code-callers <symbol>`. Grep is still right for exact strings and regex.
- **Only start a new session when** the work is genuinely unrelated, or after a deliberate `/context-save` → `/context-restore` handoff.
- **Project agent skills** live in `.agents/skills/` and are symlinked into `.claude/skills/` so they travel with all worktrees. Active skills: `/improve` (full-repo audit + improvement plans) and `/transitions-dev` (21 CSS transition patterns from transitions.dev). Versions pinned in `skills-lock.json`.

## GBrain Search Guidance (configured by /sync-gbrain)
<!-- gstack-gbrain-search-guidance:start -->

GBrain is set up and synced on this machine. The agent should prefer gbrain
over Grep when the question is semantic or when you don't know the exact
identifier yet.

**This worktree is pinned to a worktree-scoped code source** via the
`.gbrain-source` file in the repo root (kubectl-style context).
`gbrain code-def`, `code-refs`, `code-callers`, `code-callees`, `search`, and
`query` from anywhere under this worktree route to that source by default —
no `--source` flag needed (gbrain >= 0.41.38.0; on older gbrain the call-graph
commands need `--source "$(cat .gbrain-source)"`). Conductor sibling worktrees
of the same repo each have their own pin and their own indexed pages, so
semantic results match the code on disk here.

Call-graph queries (`code-callers`/`code-callees`) also need the graph to be
built first — run `/sync-gbrain --dream` (or `--full`) if they return
`count: 0`. This only works if this source's gbrain schema pack extracts code
symbols; on a non-code-aware pack `--dream` completes but the graph stays empty
and reports a WARN. `code-def`/`code-refs` need the same extraction.

Two indexed corpora available via the `gbrain` CLI:
- This worktree's code (auto-pinned via `.gbrain-source`).
- `~/.gstack/` curated memory (registered as `gstack-brain-<user>` source via
  the existing federation pipeline).

Prefer gbrain when:
- "Where is X handled?" / semantic intent, no exact string yet:
    `gbrain search "<terms>"` or `gbrain query "<question>"`
- "Where is symbol Y defined?" / symbol-based code questions:
    `gbrain code-def <symbol>` or `gbrain code-refs <symbol>`
- "What calls Y?" / "What does Y depend on?":
    `gbrain code-callers <symbol>` / `gbrain code-callees <symbol>`
- "What did we decide last time?" / past plans, retros, learnings:
    `gbrain search "<terms>" --source gstack-brain-<user>`

Grep is still right for known exact strings, regex, multiline patterns, and
file globs. Run `/sync-gbrain` after meaningful code changes; for ongoing
auto-sync across all worktrees, run `gbrain autopilot --install` once per
machine — gbrain's daemon handles incremental refresh on a schedule.

Safety: don't run `/sync-gbrain` while `gbrain autopilot` is active — the
orchestrator refuses destructive source ops when it detects a running autopilot
to avoid racing it (#1734). Prefer registering user repos with `gbrain sources
add --path <dir>` (no `--url`): URL-managed sources can auto-reclone, and the
sync code walk for them requires an explicit `--allow-reclone` opt-in.

<!-- gstack-gbrain-search-guidance:end -->
