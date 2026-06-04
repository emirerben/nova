# Nova — Technical Decisions

> Key decisions logged here until ARCHITECTURE.md is written. Format: date, decision, why, revisit trigger.

---

## [2026-03-22] Monorepo over separate repos

**Decision:** Single repo `emirerben/nova` with `apps/web` + `apps/api`
**Why:** Frontend and backend API contract will change constantly early on. Monorepo eliminates cross-repo PRs for contract changes. Two-person team doesn't need the isolation overhead.
**Revisit if:** API needs to be licensed/distributed separately, or team grows >5 engineers.

---

## [2026-03-22] FastAPI over Flask

**Decision:** FastAPI for the Python backend
**Why:** Async-native (important for job status streaming), auto-generates OpenAPI docs (aids frontend/agent integration), faster than Flask for our use cases.
**Revisit if:** team has strong Flask expertise or a library we need is Flask-only.

---

## [2026-03-22] FFmpeg subprocess over MoviePy

**Decision:** FFmpeg via `subprocess.run()` directly, not MoviePy
**Why:** MoviePy's `VideoFileClip` buffers the entire video into RAM. A 2GB source video = OOM crash. FFmpeg streams. Existing `~/src/vid-to-audio/` project is the cautionary example.
**Revisit if:** never. This is a permanent constraint.

---

## [2026-03-22] GitHub under emirerben (personal)

**Decision:** Repos live at `github.com/emirerben/nova` and `github.com/emirerben/nova-workspace`
**Why:** Fastest setup, no new org to create. ybyesilyurt is collaborator.
**Revisit if:** Nova incorporates, or we add a third engineer.

---

## [2026-03-27] Interstitials as separate clips, not xfade parameters

**Decision:** Render interstitials (curtain-close, black hold, white flash) as standalone video clips inserted between template slots, rather than encoding them as xfade transition parameters.
**Why:** xfade can only blend two adjacent clips. Curtain-close is a three-phase effect (bars closing, hold, next clip) that needs its own timeline segment. Separate clips also make beat-snap accounting explicit (cumulative_s tracks total duration).
**Revisit if:** FFmpeg adds native curtain-close xfade type, or performance requires fewer concat segments.

---

## [2026-03-27] Playfair Display over Montserrat for editorial overlays

**Decision:** Bundle Playfair Display (Bold + Regular) as the primary editorial font. Montserrat retained for font-cycle contrast.
**Why:** Playfair's serif forms are more readable at mobile text sizes and signal editorial quality. Sans/serif contrast during font-cycle adds visual variety. ASS subtitle filter uses `fontsdir` to discover bundled .ttf files.
**Revisit if:** user testing shows readability issues on specific devices, or font-cycle contrast feels jarring.

---

## [2026-03-27] geq pixel expression over drawbox for curtain-close animation

**Decision:** Use FFmpeg `geq` filter to animate curtain-close bars, not `drawbox`.
**Why:** drawbox's `h`, `w`, `x`, `y` parameters are static expressions that do NOT have access to the `t` (timestamp) variable. Only drawbox's `enable` expression can reference `t`, but that only toggles visibility, it cannot animate bar height over time. `geq` evaluates per-pixel per-frame with full access to `T` (timestamp), `X`, `Y`, `H`, `W`. Performance cost is mitigated by splitting the clip: stream-copy the prefix, geq-process only the short tail segment.
**Revisit if:** FFmpeg adds timestamp support to drawbox geometry expressions, or a lighter filter gains per-frame expression evaluation.

---

## [2026-03-27] Font-cycle timing: separate multi-PNG timestamps from overlay timestamps

**Decision:** Font-cycle overlays generate their own `start_s`/`end_s` timestamps per PNG frame. `_burn_text_overlays()` must not overwrite these with the parent overlay's single timestamp.
**Why:** A font-cycle overlay produces N PNGs with precise per-frame timing (e.g., 0.15s intervals). The previous code had a 1:1 timing reassignment loop that overwrote all font-cycle timestamps with the single overlay's `start_s`/`end_s`, collapsing every frame to the same time window. This caused all font-cycle PNGs to display simultaneously instead of sequentially.
**Revisit if:** overlay rendering is refactored to a single-pass approach (e.g., ASS-only rendering for font-cycle).

---

## [2026-03-28] Fly.io over Cloud Run / Railway for API + Worker deploy

**Decision:** Deploy API and Celery worker as separate Fly.io process groups in a single app, with Alembic migrations as a release_command.
**Why:** Fly.io natively supports multi-process apps (api + worker) with per-group VM sizing. Worker needs 2048MB for FFmpeg; API only needs 512MB. `release_command` runs migrations before any new code starts serving traffic. Cloud Run would require two separate services with separate deploy pipelines. Railway lacks per-process memory controls.
**Revisit if:** worker FFmpeg memory usage exceeds 2GB (bump vm sizing), or we need GPU transcoding (move worker to a GPU-capable platform).

---

## [2026-04-11] overlay-constants.ts: pure logic extracted from editor components

**Decision:** All canvas math, position maps, font maps, and helper functions (`getEffectiveTiming`, `isOverlayVisible`, `snapToNearestZone`, `computeBarPosition`) live in `overlay-constants.ts`, separate from React components.
**Why:** Makes the mapping layer independently testable and keeps components free of coordinate arithmetic. Constants must stay in sync with `app/pipeline/text_overlay.py` (same pixel values: CANVAS_W=1080, POSITION_Y_MAP, FONT_SIZE_MAP).
**Revisit if:** backend constants change — frontend map must be updated to match.

---

## [2026-03-27] Gemini vocabulary translation layer

**Decision:** Map Gemini's human-friendly transition names (whip-pan, zoom-in, dissolve) to internal FFmpeg xfade types via `translate_transition()`, rather than constraining Gemini's output vocabulary.
**Why:** Gemini produces better creative direction when using natural film terminology. The translation layer is 10 lines and easy to extend. Unknown types default to "none" (hard-cut) for safety.
**Revisit if:** the vocabulary mapping grows beyond 20 entries, or Gemini starts generating types that don't map cleanly.

---

## [2026-04-17] yt-dlp subprocess over yt-dlp Python API for audio download

**Decision:** Invoke yt-dlp as a subprocess (`subprocess.run(['yt-dlp', ...])`) rather than importing the yt-dlp Python API directly.
**Why:** Consistent with the FFmpeg subprocess pattern — keeps RAM usage flat regardless of source file size. The Python API has different release cadence from the CLI binary, which can cause breakage when YouTube/SoundCloud change their protocols. Subprocess always uses the installed binary, which is simpler to update.
**Revisit if:** yt-dlp Python API stabilizes and we need programmatic progress reporting.

---

## [2026-04-17] FFmpeg energy-peak beat detection over librosa

**Decision:** Use FFmpeg `silencedetect`/`astats` for beat detection rather than librosa.
**Why:** No additional dependency (~50MB numpy/scipy stack). FFmpeg is already required; adding a pure-subprocess beat detector keeps the Docker image lean. Quality is sufficient for cut-on-beat videos (energy transients = drum hits, bass drops). librosa is logged in TODOS.md as a P3 upgrade path if this proves insufficient.
**Revisit if:** users report poor beat alignment on melodic-only tracks (no percussion). Librosa onset detection handles these cases better.

---

## Pipeline incident archaeology (extracted from CLAUDE.md for size)

These are the "why" behind invariants stated tersely in CLAUDE.md's pipeline sections. CLAUDE.md keeps the rule + guard test; the narrative lives here.

### [2026-05-18] Single-pass CFR-before-xfade invariant
**Incident:** prod job `856daa32-…` on the BAD BUNNY music template aborted with `current rate of 1/0 is invalid`. The per-clip chain head `framerate=fps=N` interpolates against source PTS and silently fails on inputs reporting `avg_frame_rate=1/0` ("unknown rate" — some phone HEVC, HEIF-derived video, screen recordings); xfade then rejects the stream.
**Fix:** every per-clip chain in `single_pass.py` (`_per_clip_filter_chain`) must end with `fps={output_fps}, setpts=PTS-STARTPTS, settb=AVTB` — `fps=` drops/duplicates frames independent of PTS coherence, so it works where `framerate=` can't. Locked by `test_per_clip_chain_forces_cfr_before_xfade`.

### [2026-05] Renderer-parity invariant (#296/#297 class — "looks right locally, clips in prod")
**Incident:** the admin overlay-preview + classic templates render via Pillow, while agentic + music jobs render via Skia. #296 carried `text_anchor` through the burn dict and fixed Pillow + preview, but Skia's `_draw_centered_text`/`_draw_karaoke_line` kept centering every line on `position_x_frac`; prod job `ff0d2e1c` still rendered `text_anchor="left"` overlays clipped ("It's not just luck" → "s not just luck"). #297 fixed `_draw_centered_text`/`_draw_pop_in_with_suffix`; the karaoke path was fixed later.
**Rule:** any overlay field plumbed through the burn dict MUST be honored by BOTH renderers. Guard: `test_both_renderers_honor_text_anchor_left`. Agentic/music overlay changes are NOT verified by the admin preview — verify the burned Skia video (`make verify-overlays`).

### [2026-05-13] Gemini metadata never becomes on-screen overlay text
**Incident:** job `a1091488` (Rule of Thirds) rendered "pilot in cockpit" — Gemini's `detected_subject` from a cockpit clip — in place of "The"/"Thirds".
**Fix:** removed the `_consensus_subject(clip_metas)` fallback and the empty-hook `clip_meta.hook_text` fallback. Overlay substitution input is now exclusively user-provided (`inputs.location`). Sentinel: `TestNoGeminiTextLeaks`. Does NOT cover `copy_writer` (captions are a separate trust surface).

### Encoder-policy preset history
- PR #102: curtain-close → `medium`.
- PR #105: curtain-close → `fast` + `--concurrency=1` + PNG-overlay.
- Brazil pixelation fix: propagated `fast` to the three `template_orchestrate.py` final-output sites the prior PRs missed (`ultrafast` disables `mb-tree`/`psy-rd`/B-frames/trellis → visible 16×16 macroblocking on smooth gradients; CRF does not compensate). Locked by `tests/test_encoder_policy.py`.

---

## Storage retention incidents (extracted from CLAUDE.md for size)

### Generative re-signing invariant (added 2026-06)
**Rule:** `generative-jobs/*` blobs persist forever (NOT in the 24h delete rule) but
`upload_public_read` still signs `output_url` for only 1 day. A content-plan /
generative item viewed >24h after render reads "ready" but its stored signature is
expired → GCS 400 `ExpiredToken` → empty `<video>`.

**Fix:** read-time re-signing, NOT a TTL bump (a longer global TTL would make
`dev-user/`/`music-jobs/` URLs outlive their 1-day-deleted blobs):
`GET /generative-jobs/{id}/status` re-signs each ready variant's `output_url` from
the persisted `video_path` key via `_variants_for_response` (`routes/generative_jobs.py`,
`PLAYBACK_URL_TTL_MIN`). The raw `_variants_of` stays unsigned for the mutate paths so
a short-lived URL never lands back in the DB.

Pinned by `test_variants_for_response_resigns_ready_variant` in
`tests/routes/test_generative_jobs.py`. Admin debug/list views still show stored (stale)
URLs — follow-up.

---

## Celery time-limit invariant (extracted from CLAUDE.md for size)

### [2026-06-01] Duplicate-worker space exhaustion (prod job 08532ba3)
**Incident:** generative voiceover job `08532ba3` at `time_limit=2000` vs broker
`visibility_timeout=1900s`. With `task_acks_late=True`, a task still in-flight past
visibility_timeout is redelivered to a SECOND worker while the first runs — duplicate
concurrent execution. Two workers both writing to the RAM-backed `/tmp` (tmpfs) →
`No space left on device` mid HDR pre-tonemap.

**Rule:** every long-running task's `time_limit` MUST stay strictly under the worker's
broker `visibility_timeout` (`app/worker.py`, currently 1900s). Render orchestrators
use `soft_time_limit=1740, time_limit=1800`. `batch_import_from_drive` (2400) is the
deliberate exception (download-bound, separate handling).

Locked by `tests/tasks/test_task_time_limits.py`.

---

## Kill-switch incidents (extracted from CLAUDE.md for size)

### LYRIC_DYNAMIC_CROSSFADE_ENABLED — WARNING on disabling
**Background:** Defaults to `true`. Set to `false` to fall back to legacy
`_inject_line` scheduler behavior byte-identically.

**WARNING:** disabling this flag re-introduces the stacked-text bug observed in prod
jobs `5a71226e` and `e72d52e9` (Mirea track) — the legacy timing math is precisely
what produced the bug. Use ONLY for emergency rollback (e.g., the new path itself ships
a regression), and re-enable as soon as the regression is patched. Do NOT leave the
kill switch off as a long-term mode.

Kill-switch byte-identical test:
`tests/pipeline/test_lyric_injector_no_stacking.py::test_kill_switch_disabled_reproduces_pre_fix_output`

Apply: `fly secrets set LYRIC_DYNAMIC_CROSSFADE_ENABLED=false --app nova-video` then
`fly machine restart <id>` on the worker process group.
