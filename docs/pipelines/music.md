# Music beat-sync pipeline — internals

Reference doc for deep pipeline internals. CLAUDE.md carries the lifecycle contract and
output-URL rule; this file carries the mechanics.

## Beat detection and best-section selection

`_detect_audio_beats()` in `audio_download.py` uses FFmpeg `silencedetect`/`astats`
energy-peak analysis on the downloaded audio file.

`_auto_best_section()` scores 30s windows by beat density; result stored in
`MusicTrack.track_config` as `best_start_s`/`best_end_s`/`slot_every_n_beats`.

## Recipe generation

`generate_music_recipe()` in `music_recipe.py` slices the best section into N slots
where N = `beat_count / slot_every_n_beats`; each slot target duration = beats-per-slot
× beat interval.

## Job orchestration

`orchestrate_music_job` Celery task runs parallel Gemini clip analysis →
`template_matcher.match` → `_assemble_clips` with beat-snap → `_mix_template_audio`.

Output URL contract: `_run_music_job`, `_run_templated_music_job`, and
`orchestrate_auto_music_job` persist the **signed URL** returned by `upload_public_read`
into `assembly_plan.output_url` — NOT the relative GCS path. Legacy rows that still
hold the relative path are stripped to `null` by the "Previous renders" list
(`GET /admin/music-tracks/{id}/test-jobs`); the UI shows a "legacy format, re-render to
view" notice.

## Audio download

`audio_download.py` uses yt-dlp subprocess (not yt-dlp Python API) to avoid RAM
buffering; downloads to a temp path in GCS-mounted storage.

## Auto-music classification

After `_run_gemini_audio_analysis` succeeds, `analyze_music_track_task` reuses the same
Gemini `file_ref` to run `song_classifier` (`nova.audio.song_classifier`) and persists
a locked-schema `MusicLabels` blob on `MusicTrack.ai_labels` + `label_version` (schema:
`app/agents/_schemas/music_labels.py`, `CURRENT_LABEL_VERSION = 2026-05-15`). Classifier
failure is non-fatal — the recipe still saves; the track just won't be visible to
`music_matcher` until backfill (`scripts/backfill_song_classifier.py`).

## Auto-music matching

`music_matcher` (`nova.audio.music_matcher`, Gemini Flash, text-only) ranks the full
published-track library against a clip set using each track's Phase-1 `MusicLabels`.

## Song-sections visualizer

`song_sections` agent output (`MusicTrack.best_sections` + `section_version`, schema:
`app/agents/_schemas/song_sections.py`) is exposed on `GET /admin/music-tracks` and
rendered as a ranked band SVG over the beat strip at `/admin/music/[id]` (with hover
rationale + click-to-seek). `_to_response` in `admin_music.py` coerces sections
row-by-row and drops drifted enums so a single bad row can't 500 the list endpoint.
`src/lib/music-api.ts` carries a hand-mirrored `SongSection` interface — keep its
literal unions in sync when the Pydantic schema changes.

## Admin proxy

Next.js `/api/admin/[...path]` route proxies to Fly.io API, keeping the admin token
server-side only (never exposed to browser).

## Clip-count validation

`slot_count` returned by `/music-tracks` tells the frontend exactly how many clips to
collect; `POST /music-jobs` validates clip count matches before enqueuing.
`_validate_clip_count` is public (no underscore) so `admin_music.py`'s test-job/rerender
endpoints can reuse it across modules.

## Beat-sync guards

- Tracks with 0 detected beats are marked `failed` at analysis time.
- `POST /music-jobs` rejects non-ready or non-published tracks.
