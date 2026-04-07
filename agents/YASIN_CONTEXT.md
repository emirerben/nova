# YASIN_CONTEXT — Template pipeline work

Mission brief for Yasin (ybyesilyurt), who owns template pipeline quality and
adding new templates. Read this first, then `agents/VIDEO_CONTEXT.md`.

## Mission

1. **Improve the existing default template** — raise output quality (motion, pacing, overlays, label timing, clip rotation).
2. **Add new templates** — end-to-end: ingest a reference video, run `analyze_template`, validate the recipe, register it in the admin UI, test against real source footage.

The default template ID is pinned in `NEXT_PUBLIC_DEFAULT_TEMPLATE_ID=6ea9bed6-daf9-4352-889a-6970ce638dc1` (see `src/apps/web/.env.local`). That's the current template output every user job renders against.

## How Claude Code should manage the dev environment (automation rules)

**Start it once, leave it running. Do NOT restart manually.**

- When the user asks to start working, run `./scripts/dev-auto.sh` in the background. It starts redis, postgres, API (with `uvicorn --reload`), Celery worker (with `watchfiles` auto-restart on `.py` edits), and Next.js (HMR).
- **Never tell the user to restart a server after editing code.** The API hot-reloads on `.py` edits. The Celery worker auto-restarts via `watchfiles` on any `.py` change under `src/apps/api/app/`. The frontend hot-reloads on `.ts`/`.tsx`/`.css` edits.
- When the user asks "does it work?" or "why isn't it working?" — read logs first, do not speculate:
  - `tail -50 .dev/api.log`
  - `tail -50 .dev/worker.log`
  - `tail -50 .dev/web.log`
- When the user edits Python code in `src/apps/api/app/` and asks to verify, tail `worker.log` to confirm the `watchfiles` restart happened (look for "Restarting" or the startup banner).
- When the user edits a Gemini prompt file in `src/apps/api/prompts/`, that's a plain text file — no restart is needed; the next `analyze_template` call picks it up automatically.
- If the dev env seems broken (stale state, port conflicts, migration errors), run `./scripts/dev-stop.sh` then `./scripts/dev-auto.sh`. Do not kill individual processes.
- If `.dev/migrate.log` shows an error, the migrations failed — fix the migration and re-run.

## Project layout (what Yasin actually edits)

### Template analysis (Gemini → recipe)

Gemini reads a reference video and emits a **template recipe** — a structured
JSON describing slot count, per-slot durations, transition types, overlay text,
clip-level preferences, and a creative direction string.

| File | What it does |
|------|--------------|
| `src/apps/api/prompts/analyze_template_single.txt` | One-pass Gemini prompt (default mode) |
| `src/apps/api/prompts/analyze_template_pass1.txt` | Two-pass mode: structural extraction |
| `src/apps/api/prompts/analyze_template_pass2.txt` | Two-pass mode: creative refinement |
| `src/apps/api/prompts/analyze_template_schema.txt` | JSON schema enforced on Gemini output |
| `src/apps/api/app/pipeline/agents/gemini_analyzer.py` | `analyze_template()` — calls Gemini File API, parses + validates |
| `src/apps/api/app/services/template_validation.py` | Post-Gemini recipe validation (slot durations, transition names, etc.) |

**To improve template analysis quality, edit the prompt files.** They are plain
text, no code change or restart needed — the next analyze call picks them up.

### Template orchestration (recipe → FFmpeg → output video)

Once a recipe exists and a user job comes in, the worker orchestrates FFmpeg
to render the final clip. This is where most quality improvements live.

| File | What it does |
|------|--------------|
| `src/apps/api/app/tasks/template_orchestrate.py` | Top-level Celery task. Matches user clips to slots, assembles timeline, calls rendering helpers. Contains `_LABEL_CONFIG`, `_MERGE_GAP_THRESHOLD_S`, `MIN_CURTAIN_ANIMATE_S`, `_CURTAIN_MAX_RATIO`. |
| `src/apps/api/app/pipeline/template_matcher.py` | Clip → slot assignment. Has `_minimum_coverage_pass()` for variety. |
| `src/apps/api/app/pipeline/interstitials.py` | Curtain-close + fade-black + flash-white rendering (via `geq` and `drawbox`) |
| `src/apps/api/app/pipeline/transitions.py` | Maps Gemini vocabulary (whip-pan, zoom-in, dissolve) → FFmpeg xfade types |
| `src/apps/api/app/pipeline/text_overlay.py` | Gaussian-shadow text, font-cycle, ASS animated overlays |
| `src/apps/api/app/pipeline/reframe.py` | 9:16 reframing (scale + pad or smart crop) |
| `src/apps/api/app/pipeline/score.py` | Segment scoring (energy, motion, speech density) |
| `assets/fonts/` | Bundled fonts (Playfair Display Bold + Regular). Referenced by ASS via `fontsdir=`. |

### Template model + admin

| File | What it does |
|------|--------------|
| `src/apps/api/app/models.py` | `VideoTemplate` SQLAlchemy model — has `recipe_cached` (JSONB), `analysis_status`, `analysis_mode` |
| `src/apps/api/app/routes/admin.py` | Admin endpoints: `POST /admin/templates`, `POST /admin/templates/:id/reanalyze`, etc. Requires `X-Admin-Token` header. |
| `src/apps/api/app/routes/templates.py` | Public template list + get endpoints |
| `src/apps/web/src/app/admin/templates/new/page.tsx` | Admin UI for creating a new template from a reference video |
| `src/apps/web/src/app/admin/templates/[id]/page.tsx` | Admin UI for inspecting + re-analyzing a template |

### Read these before touching pipeline code

- `agents/VIDEO_CONTEXT.md` — FFmpeg patterns, anti-patterns (never use MoviePy)
- `agents/DECISIONS.md` — why key choices were made (interstitials as clips, Playfair, etc.)
- `CLAUDE.md` "Template pipeline" section — live gotchas (font-cycle timing, beat-snap accounting, curtain-close minimums)

## Workflows

### Workflow A — improve an existing template

1. Start the dev env (Claude Code handles this automatically).
2. Submit a test job through the UI at http://localhost:3000/template, or hit the API directly:
   ```
   curl -X POST http://localhost:8000/template-jobs \
     -H "Content-Type: application/json" \
     -d '{"template_id":"6ea9bed6-daf9-4352-889a-6970ce638dc1","source_gcs_path":"gs://nova-videos-dev/path/to/source.mp4"}'
   ```
3. Watch the worker log: `tail -f .dev/worker.log`
4. When the job finishes, the processed output is at `Job.result_gcs_path`. Download with `gsutil cp` and review.
5. Make your pipeline code change (e.g. edit `template_orchestrate.py` label timing, or `interstitials.py` curtain math).
6. **The worker auto-restarts.** Do not restart manually.
7. Re-run the same job to compare. The eval harness (`EVAL_HARNESS_ENABLED=true`) uploads per-slot intermediate clips to GCS for A/B visual comparison.

### Workflow B — add a new template

1. Upload a reference video to GCS (manually, via `gsutil cp`, or through the upload endpoint).
2. In the admin UI at http://localhost:3000/admin/templates/new, create the template pointing at the GCS path. This enqueues an `analyze_template` Celery task.
3. Poll or wait. When `analysis_status="ready"`, the template has a `recipe_cached` — inspect it at `/admin/templates/<id>`.
4. If the recipe looks wrong (bad slot count, missing overlays, wrong transitions), either:
   - Edit the Gemini prompt (`src/apps/api/prompts/analyze_template_single.txt`) and hit "Re-analyze" in the admin UI.
   - Or edit `template_validation.py` to enforce missing constraints.
5. Run a test job against the new template to verify end-to-end quality.

## Environment + secrets

- `.env` must exist at repo root. Copy from `.env.example` and fill in: `GEMINI_API_KEY`, `OPENAI_API_KEY`, `STORAGE_BUCKET=nova-videos-dev`, `GOOGLE_APPLICATION_CREDENTIALS=/path/to/nova-sa.json` (or `GOOGLE_SERVICE_ACCOUNT_JSON` with inline JSON).
- The GCS service account must have read/write on `nova-videos-dev`.

## Never do

- **Never** use `MoviePy` / `VideoFileClip`. Use subprocess `ffmpeg`. See `agents/VIDEO_CONTEXT.md`.
- **Never** commit `.env` or anything under `.dev/`.
- **Never** bump the version or touch `CHANGELOG.md` — Emil handles releases.
- **Never** merge to `main`. Push to your feature branch only. Emil reviews.
- **Never** restart the API or worker manually unless the dev script is broken. Hot reload handles code changes.

## Known gotchas (from CLAUDE.md)

- **Font-cycle frame cap:** `MAX_FONT_CYCLE_FRAMES=20` prevents PNG explosion. If a font-cycle animation needs more frames, the cap kicks in and a gap-fill PNG bridges to `cycle_end`.
- **Curtain-close minimum:** `MIN_CURTAIN_ANIMATE_S=4.0` enforced at both `_assemble_clips` and `_collect_absolute_overlays` call sites, clamped to 60% of slot duration (`_CURTAIN_MAX_RATIO=0.6`).
- **Beat-snap + interstitials:** `cumulative_s` in `_assemble_clips()` must account for interstitial hold durations or beat-snap drifts.
- **Label detection is content-based**, not role-based, because Gemini sometimes returns `role=hook` instead of `role=label`. See `_is_subject_placeholder()`.
