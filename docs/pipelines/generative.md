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

## Local smoke test

```bash
make local-render MODE=generative CLIPS="a.mp4 b.mp4 c.mp4"
```
