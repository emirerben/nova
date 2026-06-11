# Generative-edits pipeline — internals

Reference doc for deep pipeline internals. CLAUDE.md carries the design contract;
this file carries the mechanics.

See also: `agents/VIDEO_CONTEXT.md` for FFmpeg patterns, `docs/pipelines/music.md` for
the music engine that generative reuses.

## What it reuses from music

`orchestrate_generative_job` reuses: `generate_music_recipe`, `music_matcher`,
`inject_lyric_overlays`, `_assemble_clips`, `_mix_template_audio`, and the JobClip
variant pattern.

Net-new render behavior:
- **No-music branch** (`original_text`): skips `_mix_template_audio` to keep source
  audio.
- **Intro overlay injection**: `generative_overlays.py` builds the agent-authored "hero
  intro" overlay and injects it directly into the recipe (same pattern as lyric
  injection; bypasses `template_text`/`text_designer` schemas).

## Three variants

- `song_lyrics` — matched song + its lyrics
- `song_text` — matched song + AI hero-intro overlay
- `original_text` — clips' original audio + AI intro

## Key files

- `src/apps/api/app/tasks/generative_build.py` — `orchestrate_generative_job` Celery
  task
- `src/apps/api/app/pipeline/generative_overlays.py` — intro overlay builder
- `src/apps/web/src/app/generative/` + `admin/generative/` — public result UI + admin
  dashboard
- `src/apps/api/app/routes/generative_jobs.py` — job submission + status: `swap-song` /
  `retext` per-variant re-renders; `_variants_for_response` re-signs ready variant URLs
  from the persisted `video_path` key (`PLAYBACK_URL_TTL_MIN`). Reuses
  `admin_music._validate_clip_path_prefixes` for the clip allowlist.
- `src/apps/api/app/routes/admin_generative.py` — `GET /admin/generative` dashboard
  list.

## Post-generation timeline editing (clip editor)

After a render, `song_text` / `original_text` montage variants are editable: reorder,
beat-quantized duration, in-point scrub, clip swap/add/remove, reset. The editor edits the
AI's assembly decisions, not pixels.

- **Contract:** `variants[i]["ai_timeline"]` (written once per assembly — rewritten by any
  match-driven re-render like swap-song) + `variants[i]["user_timeline"]` (the user's
  override, persisted by the route pre-enqueue under the `_update_variant_entry` row-lock
  pattern). Slots key on `clip_index` into `all_candidates["clip_paths"]` — matcher
  clip_ids are Gemini-ref-derived and unstable. Windows are post-resolution values.
- **Override render:** `regenerate_generative_variant(..., timeline_override=...)` builds
  exact-window `AssemblyStep`s and skips `match()`, `consolidate_slots`, and the entire
  Gemini leg (download + probe only). `exact_window` slots in `_plan_slots` reuse the
  locked-branch window arithmetic WITHOUT the letterbox output fit.
- **Resolution order:** explicit `timeline_override` kwarg → persisted `user_timeline` →
  fresh match. Retext/restyle/mix re-renders therefore honor clip edits.
- **Durable sources:** at orchestrate start, uploads are copied to
  `generative-jobs/{job_id}/sources/` (order-preserving rewrite of
  `all_candidates["clip_paths"]` — narrative order slices the first N keys, so order is
  load-bearing). This also keeps swap-song alive past the 24h upload lifecycle.
- **Endpoints:** GET/POST/DELETE `/generative-jobs/{id}/variants/{vid}/timeline`
  (mirrored on plan-items). Beat math walks the real non-uniform grid server-side.
- **Kill switch:** `GENERATIVE_TIMELINE_EDITOR_ENABLED=false` (Fly secret + restart) —
  GET returns `editable:false reason:"disabled"`, POST 403.
- **Guards:** window-parity test (`tests/pipeline/test_exact_window_steps.py`) pins that
  an unmodified override render reproduces the original assembly windows AND framing.

## Local smoke test

```bash
make local-render MODE=generative CLIPS="a.mp4 b.mp4 c.mp4"
```
