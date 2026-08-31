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

Both paths auto-run `scripts/worktree-setup.sh` (symlinks `.env`/`.venv`/`node_modules` from the primary checkout + migrations). Re-run it manually in any worktree that's missing those.

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
- `src/apps/web/src/app/generative/` — redirects to /plan (v0.45; siblings = shared editor modules); `admin/generative/` — admin dashboard
- `src/apps/web/src/app/plan/new/` — New-video chooser; /plan home = create block + past-edits grid (`WorkspaceHome.tsx`)
- `src/apps/web/src/app/create/` + `src/apps/api/app/routes/{me,manual_drafts}.py` — dark flagged footage-first creation + manual drafts (UI superseded by /plan home, backend live); `PlanItem.audio_mode` is `kria|original|voiceover` (`plans/017-qendresa-creation-flow.md`)
- `src/apps/api/app/pipeline/music_recipe.py` — beat-snap recipe generator (see `docs/pipelines/music.md`)
- `src/apps/api/app/tasks/music_orchestrate.py` — Celery tasks: beat analysis + music job orchestration
- `src/apps/api/app/services/audio_download.py` — yt-dlp audio download + beat detection via FFmpeg
- `src/apps/api/app/services/seed_provenance.py` — token-set matcher (`match_specs_to_seeds`) that links generated plan items back to the idea seed they honour; called at plan-generation time to set `PlanItem.source_idea_seed_id` and flip matched seeds to `in_plan`
- `.../plan/_components/ui/SeedProvenanceBadge.tsx` — "From your idea" lime badge on the item page
- `src/apps/api/prompts/` — LLM prompt templates (template analysis, transcription)
- `agents/` — project-level agent context (VIDEO_CONTEXT.md, STACK.md, DECISIONS.md)
- `plans/` — implementation plans (`plans/README.md` has status; 001–017)

## Local dev
```bash
cp .env.example .env            # fill in values
./scripts/dev-auto.sh            # single-command dev env with hot reload
```

`dev-auto.sh` starts redis + postgres via `docker-compose up -d redis db` (infra only), runs migrations, then launches API (`uvicorn --reload`), Celery worker (`watchfiles`), and Next.js (HMR) natively. Logs go to `.dev/<service>.log`. Do NOT restart servers manually — hot reload handles all code edits. Stop with `./scripts/dev-stop.sh`. If a worker crash leaves a plan spinning in `generating`, run `python3 scripts/dev/reset-stuck-plans.py` (local-only reaper).

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
- Pre-PR gate: `bash scripts/preship-check.sh` — scoped ruff on changed files, tsc when web TS changed, drift vs origin/main, VERSION-slot check, CI `[skip-*]` marker list. Run before every PR.

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
- Per-job GCS objects (`dev-user/*`, `music-jobs/*`, `music-lyrics-previews/*`, `voiceover-uploads/*`, `transcript-cache/*`, `training-exports/*`) are deleted by a bucket lifecycle rule 24h after upload; `jobs/*` and the anonymous `00000000-…0001/*` upload prefix at 30d. Soft delete is OFF on the bucket. Config lives at `infra/gcs-lifecycle.json` (per-prefix table + rationale: `infra/README.md`); apply once with `gsutil lifecycle set infra/gcs-lifecycle.json gs://$STORAGE_BUCKET` (not part of CI deploy).
- Curated assets (`music/*`, `templates/*`) and extracted job posters (`job-posters/{job_id}/*`, v0.59.1.0) are NOT matched by the rule and persist forever — a thumbnail must outlive its source video's retention window.
- Signed-URL TTL in `storage.py` is 1 day to match the object lifetime.
- **`generative-jobs/*` exception:** blobs persist forever but `upload_public_read` signs `output_url` for only 1 day → expired URLs show blank video after 24h. Fix is read-time re-signing via `_variants_for_response` in `routes/generative_jobs.py` (`PLAYBACK_URL_TTL_MIN`). Pinned by `test_variants_for_response_resigns_ready_variant`. See agents/DECISIONS.md "Storage retention incidents" for the full narrative.
- Authenticated uploads live under `users/{user_id}/`, outside the 24h delete prefixes.

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
- No reference template, no pre-selected song: `orchestrate_generative_job` analyzes uploaded clips, auto-matches a track, writes its own intro overlay, renders 3 variants: `song_lyrics`, `song_text`, `original_text`.
- **Best-effort:** unmatched track -> song variants skip; `original_text` renders (never hard-fails on empty library).
- Per-variant state lives in `Job.assembly_plan["variants"]` (task-owned); jobs are `Job` rows with `mode == "generative"`.
- **Intro-text persistence:** text + highlight word persist on `variants[i]["intro_text"]`/`["intro_highlight_word"]`; re-render without override reuses them (no LLM); intro_writer runs only on first render or legacy variants.
- **Fast reburn invariant:** pure text edits need a non-stale, exact-canvas `base_video_path`; otherwise full-render. Outputs are generation-keyed and token-gated. Kill switch: `GENERATIVE_FAST_REBURN_ENABLED=false` + worker restart.
- **Text-behind-subject:** `TEXT_BEHIND_SUBJECT_ENABLED`/`MATTE_DEPTH_OCCLUDER_ENABLED` (both default `false`; latter = depth backbone for non-person subjects) — matte occludes text behind subject. See `docs/pipelines/text-behind-subject.md`.
- See `docs/pipelines/generative.md` (music reuse, variant mechanics).

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
- GEMINI_API_KEY — clip + template analysis
- `NEXT_PUBLIC_CREATION_HUB_ENABLED` / `GENERATIVE_DIRECT_VOICEOVER_STRICT_ENABLED` / `NEXT_PUBLIC_MANUAL_EDITOR_ENABLED` — default `false`; enable Fly strict validation before the hub, and keep manual drafts off pending plan 017 acceptance.
- `EDIT_WIDE_LOOKS_ENABLED` — off; rollout: `docs/pipelines/generative.md`.
- `ORIENTATION_NORMALIZE_ENABLED` — defaults to `true`. Set to `false` and restart workers to make `normalize_orientation` a no-op (safety valve for orientation regressions).
- `LYRIC_DYNAMIC_CROSSFADE_ENABLED` — defaults to `true`. Set to `false` to roll back to legacy `_inject_line` behavior byte-identically. **WARNING: disabling re-introduces the stacked-text bug — emergency rollback ONLY.** Kill-switch test: `tests/pipeline/test_lyric_injector_no_stacking.py::test_kill_switch_disabled_reproduces_pre_fix_output`. Apply: `fly secrets set LYRIC_DYNAMIC_CROSSFADE_ENABLED=false --app nova-video` + `fly machine restart <id>`. See agents/DECISIONS.md "Kill-switch incidents" for the full warning.
- `TIKTOK_DEEP_ANALYSIS_ENABLED` — defaults to `true`. When false, `scrape_tiktok_profile` skips chaining `analyze_tiktok_profile` and persona/plan/hook prompts receive no TikTok analysis block (byte-identical to pre-feature). Apply: `fly secrets set TIKTOK_DEEP_ANALYSIS_ENABLED=false --app nova-video` + `fly machine restart <id>`.
- `PLAN_SYNC_DISPATCH_ENABLED` — default `true`. Generate mints the Job in-request (`dispatch_item_render_for`: FOR-UPDATE lock; duplicate ⇒ 200; publish-fail ⇒ `processing_failed`). `false` ⇒ legacy `.delay()`+409 route contract. plans/014; apply: fly secret + restart.
- `NARRATIVE_CLIP_ORDER_ENABLED` — defaults to `true`. Plan-item edits follow the filming guide's shot order (narrative mode in `template_matcher.match`; dispatch contract in `content_plan_build._narrative_clip_order`). Read at render time → flipping affects queued jobs and re-renders. Set to `false` to fall back to pure greedy matching. Apply: `fly secrets set NARRATIVE_CLIP_ORDER_ENABLED=false --app nova-video` + `fly machine restart <id>`.
- `EDITORIAL_SEQUENCE_ENABLED` — defaults to `true`. Editorial (cluster) variants with audible, coherent original speech render the transcript-synced typographic sequence (`phrase_sequence.py` + `SequenceEmphasisAgent` + `EDITORIAL_STYLE`); without eligible speech (incl. song/voiceover variants) RHYTHM MODE paces an agent-authored quote instead (`SequenceQuoteWriterAgent` + `rhythm_scenes`, persisted as `sequence_quote`/`sequence_mode`; agent failure ⇒ static cluster, never a heuristic quote). `false` ⇒ legacy single static cluster, byte-identical. Guards: `tests/tasks/test_generative_build_sequence.py`. Apply: `fly secrets set EDITORIAL_SEQUENCE_ENABLED=false --app nova-video` + `fly machine restart <id>`.
- `SMART_MUSIC_BED_ENABLED` — default `true`. Kill switch for the v2 licensed music bed (independent of SFX lane); off = no new treatments, reburns skip the bed but preserve persisted state.
- `SOUND_EFFECTS_ENABLED` / `MEDIA_OVERLAYS_ENABLED` — both default **`false`**. Gate the SFX-lane and overlay-lane write/render routes in `routes/plan_items.py` (404 when off); public `GET /sound-effects` stays ungated (picker loads). Caption archetypes (subtitled/narrated) carry both lanes; every caption reburn re-applies persisted lanes (`docs/pipelines/generative.md`). **Dual-flag trap:** frontend lanes use the `NEXT_PUBLIC_` twins (Vercel); frontend on + backend off ⇒ saves 404. Keep Fly + Vercel in sync. Apply: `fly secrets set SOUND_EFFECTS_ENABLED=true --app nova-video` + `fly machine restart <id>` (api + worker).
- `MEDIA_OVERLAY_ALPHA_ENABLED` — defaults **`false`**. When true, transparent image pip cards keep alpha (PNG normalize + `format=rgba`, final composite pinned `yuv420p`); off is byte-identical JPEG flatten. Image pip cards only. No NEXT_PUBLIC twin by design (render-only). Internals: `docs/pipelines/generative.md`. Apply: `fly secrets set MEDIA_OVERLAY_ALPHA_ENABLED=true --app nova-video` + `fly machine restart <id>` (worker).
- `FULLSCREEN_CUTAWAYS_ENABLED` — defaults **`false`**. Gates the AI fullscreen branch (`build_suggestions` slot `"full"` → `display_mode="fullscreen"` cover-crop takeover). Dual-flag with `NEXT_PUBLIC_FULLSCREEN_CUTAWAYS_ENABLED` (Vercel; gates the MANUAL promote affordances): **Fly first, then Vercel** — new web + OLD api silently bakes manual fullscreen as pip. Render rollback = `MEDIA_OVERLAYS_ENABLED`. Guards: `tests/test_overlay_fullscreen_rules.py`, dual preset pins in `tests/test_media_overlay_command.py`. Narrative: agents/DECISIONS.md "Kill-switch incidents"; plans/009.
- `SUBTITLED_TEXT_LANE_ENABLED` — defaults **`false`**. Styled-text lane on subtitled variants: text burns (Skia) onto the caption-free base FIRST, captions LAST via `_compose_subtitled_final`; every fast-reburn mints a NEW GCS key + deletes the old (CDN staleness). Dual-flag with `NEXT_PUBLIC_SUBTITLED_TEXT_LANE_ENABLED` (Vercel). Fly first, then Vercel. Apply: `fly secrets set SUBTITLED_TEXT_LANE_ENABLED=true --app nova-video` + `fly machine restart <id>` (api + worker). Internals: `docs/pipelines/generative.md`.
- `SUBTITLED_ARCHETYPE_ENABLED` — **ON in prod** (code default `false`). Gates the subtitled single-clip edit style (talk-to-camera clip → auto-language captions, editable + reburnable, sentence-per-cue pop-in; TR/EN). Off ⇒ falls back to montage. Dual-flag with `NEXT_PUBLIC_SUBTITLED_ENABLED` (Vercel, also ON) — inlined at Next.js build time, so changing it needs a `vercel --prod` rebuild, not just an env flip. Companions: `SUBTITLED_CAPTION_CORRECTION_ENABLED` (default `true`) + `CAPTION_CORRECTION_MODEL` (default `gpt-4o`; mini missed TR case errors 4/4). Rollback: `fly secrets set SUBTITLED_ARCHETYPE_ENABLED=false --app nova-video` (api + worker) + `vercel env rm NEXT_PUBLIC_SUBTITLED_ENABLED production` + `vercel --prod`.
- `NARRATED_SELF_NARRATION_ENABLED` — defaults **`false`**. Narrated items generate WITHOUT a recorded voiceover when the footage's own audio carries the voice: 1 clip → `subtitled` (captions), 2+ → `talking_head` (speech spine); no speech → montage + reason persisted on `assembly_plan["archetype_fallback"]` (item-page banner). SOLE gate — deliberately bypasses the two archetype flags above. Dual-flag `NEXT_PUBLIC_NARRATED_SELF_NARRATION_ENABLED` (Vercel); flip Fly first. Voiceover, when recorded, still wins (narrated archetype unchanged). Guards: flag-off pins in `tests/tasks/test_generative_dispatch.py`.
- `CAPTION_PUNCTUATION_ENABLED` — defaults `true`. `_transcribe_openai` restores punctuation/case from full-text onto the timed word stream via `align_punctuated_text()` (`transcribe.py`); ANY residual mismatch bails the WHOLE transcript (fail-open). `false` ⇒ byte-identical; `_transcribe_local` unaffected. Internals: `docs/pipelines/smart-captions.md`. Apply: `fly secrets set CAPTION_PUNCTUATION_ENABLED=false` + restart.
- `POSTER_ONDEMAND_REPAIR_ENABLED` / `POSTER_REPAIR_QUEUE` — default **`false`** / `celery`. `POST /me/jobs/posters/refresh` stops being a pure re-signer and enqueues `tasks.repair_job_poster` (`app/tasks/poster_repair.py`) to mint a missing library poster; off is byte-identical. **Set the queue FIRST** — the task downloads a full MP4 + runs ffmpeg, so prod needs `autoplace-jobs` (2GB), never the 1GB `light`/Beat machine. Guards: `tests/tasks/test_poster_repair.py`, `tests/routes/test_me_jobs.py`. Narrative + apply order: agents/DECISIONS.md "Storage retention incidents".
- `SILENCE_CUT_ENABLED` — defaults **`false`**. Cuts silence/fillers only on speech paths; music/beat paths are excluded (`test_silence_cut_isolation.py`). Fail-open to the uncut render. `RETAKE_CUT_ENABLED` is an independent default-false switch. Candidates use revision-guarded apply/restore and a full speech rerender; no browser-side cut. Behavior pin: `test_silence_cut_golden.py`; plans/010. Enable both only after production approval: `fly secrets set SILENCE_CUT_ENABLED=true RETAKE_CUT_ENABLED=true --app nova-video` + worker restart.

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
- Env vars: set via `vercel env` CLI (NEXT_PUBLIC_API_URL, NEXT_PUBLIC_WS_URL, NEXT_PUBLIC_DEFAULT_TEMPLATE_ID, NEXTAUTH_SECRET, ADMIN_BASIC_AUTH_USER, ADMIN_BASIC_AUTH_PASSWORD). NEXT_PUBLIC_GOOGLE_CLIENT_ID + NEXT_PUBLIC_GOOGLE_PICKER_API_KEY were removed in v0.7.8.2 with the dead `/template/[id]` route (Drive picker gone; NextAuth uses server-side GOOGLE_CLIENT_ID).
- **`ADMIN_BASIC_AUTH_USER` + `ADMIN_BASIC_AUTH_PASSWORD` are MANDATORY.** They gate `/admin/*` and `/api/admin/*` via `src/apps/web/src/middleware.ts`. Without them, every admin page returns 503 — fail-closed by design.
- Preview deploys: full API access via regex CORS (`allow_origin_regex` in `main.py`)

### Fly.io — API + Workers (configured by /setup-deploy)
- App name: nova-video
- Region: iad
- Production URL: https://nova-video.fly.dev
- Deploy workflow: **Fly Deploy** only; no bare `fly deploy`. Runbook: `docs/runbooks/video-poster-backfill.md`.
- Deploy status command: `fly status --app nova-video`
- Merge method: squash
- Process groups: api (FastAPI/uvicorn) + worker (Celery)
- Release command: `python -m alembic upgrade head` (runs migrations on every deploy)
- VM sizing: fly.toml `[[vm]]` is the source of truth per process group
- **Celery time-limit invariant:** every long-running task's `time_limit` MUST stay strictly under the worker's broker `visibility_timeout` (`app/worker.py`, currently 1900s). Render orchestrators use `soft_time_limit=1740, time_limit=1800`. Locked by `tests/tasks/test_task_time_limits.py`. See agents/DECISIONS.md "Celery time-limit invariant" for the prod incident.
- Dockerfile: repo-root `Dockerfile` (cached dependency layer from pyproject.toml)
- Docker image includes: `app/`, `assets/`, `prompts/`, `alembic.ini`
- CORS: `ALLOWED_ORIGINS` env var — JSON array format

### Custom deploy hooks
- Pre-merge: none
- Deploy trigger (API): **Fly Deploy** (push or manual rerun)
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
Every new `COPY <src> ...` in the prod `Dockerfile` must be verified against
`.dockerignore` — the two files do not track each other, and a mismatch fails at Fly's
builder AFTER the PR merged. `.github/workflows/docker-build.yml` catches it on the PR
for anything touching `Dockerfile`, `.dockerignore`, or `src/apps/api/**`. Narrative:
agents/DECISIONS.md "Dockerfile / .dockerignore coupling" (PR #118/#119).

## Agentic workflow (how to work fast here)

- **Default to subagents, not new sessions.** Spawn a subagent (Agent tool) per heavy subtask from ONE orchestrating session. Each subagent burns its own context window and returns only a summary. Do NOT open a new session per subtask.
- **Parallelize independent subtasks** in one message (multiple Agent calls). Use `isolation: "worktree"` on subagents that edit files in parallel.
- **For batchable work** across N items, run the decompose workflow: `Workflow({ scriptPath: ".claude/workflows/decompose.js", args: { subtasks: [{title, prompt}, ...] } })`. Running ANY workflow needs explicit opt-in — include the word "workflow" in the request.
- **Prefer gbrain over grep for semantic lookups.** `gbrain search "<intent>"`, `gbrain code-def <symbol>`, `gbrain code-callers <symbol>`. Grep is still right for exact strings and regex.
- **Only start a new session when** the work is genuinely unrelated, or after a deliberate `/context-save` → `/context-restore` handoff.
- **Project skills** live in `.agents/skills/`: `/improve`, `/motion-dev`, `/transitions-dev`, `/verify-editor-timeline`. Read each `SKILL.md` for its trigger and gate; external versions are pinned in `skills-lock.json`.

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
- `~/.gstack/` curated memory + Claude transcripts, in the `default` source.

Prefer gbrain when:
- "Where is X handled?" / semantic intent, no exact string yet:
    `gbrain search "<terms>"` or `gbrain query "<question>"`
- "Where is symbol Y defined?" / symbol-based code questions:
    `gbrain code-def <symbol>` or `gbrain code-refs <symbol>`
- "What calls Y?" / "What does Y depend on?":
    `gbrain code-callers <symbol>` / `gbrain code-callees <symbol>`
- "What did we decide last time?" / past plans, retros, learnings:
    `gbrain search "<terms>" --source default`

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

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
