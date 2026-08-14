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

### [2026-05] Dockerfile / .dockerignore coupling (PR #118/#119)
**Incident:** PR #118 (`f156c19`, v0.4.7.0) added `COPY src/apps/api/tests/evals/rubrics ./tests/evals/rubrics` for the Loop B online judge. Local pytest + the `test-api` CI job both passed (they run against the source tree, not the image). The deploy then failed on Fly with "not found in build context" because `.dockerignore` excluded the source path — the two files don't track each other and nothing local flags the mismatch. PR #119 (`121a6e9`) hotfixed `.dockerignore` with negation patterns to let the rubrics through.
**Rule:** any new `COPY <src> ...` in the prod `Dockerfile` must have its source path verified against `.dockerignore`. `.github/workflows/docker-build.yml` now runs `docker build` against the prod Dockerfile on every PR touching `Dockerfile`, `.dockerignore`, or `src/apps/api/**`, so this class of bug fails on the PR, not on merge-to-main.

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

---

## [2026-06-07] Style-chat 500 + false "Done": validator divergence incident

**Symptom (prod):** user typed "I like using small texts…" → 500 "Something went wrong".
Retry succeeded with "Done" but stored nothing size-related.

**Root cause 1 — 500:** Two Pydantic validators for `text_size_px` disagreed.
`StyleKnobs._clamp_px` silently clamps any value to `[40, 80]` (passes), but
`StyleKnobsEdit(ge=40)` was constructed from the **raw** agent value (e.g. 14) and
raised `ValidationError` outside any `try/except` → unhandled HTTP 500.
The model emits CSS-scale px (12/14) because the prompt never stated the legal range
or that these are 1080×1920 video-overlay px (not CSS px).

**Root cause 2 — false "Done":** Retry returned `style_edit` with only `free_text` (no
structured knob). Route called `_apply_style_edit` anyway, wrote only `status="edited"`,
set `applied=True` → "Done — your next render will use this style." with nothing stored.

**Root cause 3 — no read-back:** `_style_snapshot` exposed 3 of 10 knobs; the intent
taxonomy had no `describe` intent; the prompt steered "what is it set to?" to `unknown`.

**Fixes (PR #484):**
- Route: build `StyleKnobsEdit` from the clamped `StyleKnobs` output, never from raw
  agent dict. Detect clamping → append honest note to reply.
- Route: materiality check before `_apply_style_edit` — free_text-only returns
  `applied=False` without writing.
- Prompt: document px range 40–80, semantic size map ("small"→40–48), and the rule
  that `style_edit` MUST carry a concrete field.
- Agent + route: add `describe` intent; expose all 10 knobs in `_style_snapshot`.
- Pinned by `test_agent_turn_style_edit_small_px_clamped_to_40`,
  `test_agent_turn_style_edit_free_text_only_no_write`,
  `test_agent_turn_describe_intent_no_write`,
  `test_validator_parity_text_size_px_boundary_values`.

**Design rule going forward:** whenever two Pydantic validators cover the same field with
different strictness (one clamps, one raises), always use the lenient validator's OUTPUT
to construct the strict model — never the raw input.

## Editorial sequence: never-overlap + composite stream (2026-06-13, PR pending)

The editorial "sequence" feature (transcript-synced + rhythm-mode kinetic typography) carries
three invariants worth knowing before touching `phrase_sequence.py` / `text_overlay_skia.py`:

- **Scenes never overlap.** The original design crossfaded scenes (0.25s overlap); frame-by-frame
  analysis of the user's reference video proved phrases must exit cleanly before the next enters
  (verified empty frames between phrases). `split_phrases` ends every scene 0.1s before the next
  (`SCENE_CLEAR_GAP_S`); a 7-case property test pins no-overlap. Do not reintroduce crossfade.
- **Demo==production golden.** `test_golden_demo_quote_reproduces_approved_scene_windows` pins the
  user-approved render's exact 9 scene windows (3-decimal equality) through
  `synthesize_phrase_timings`. If rhythm pacing math changes, that approval is void — re-render and
  re-approve before merging.
- **Sequence overlays burn as ONE composite PNG stream** (`_render_sequence_composite`): FFmpeg burn
  cost scales with INPUT COUNT (~6.5s/input on a 60s canvas), not frames or bytes — 80 per-block
  inputs took 525s and flirted with the 600s subprocess timeout; the composite is 11.3s. Unique
  frames render only at pops/fade ramps; holds are hard-linked. Never emit sequence blocks as
  separate FFmpeg inputs.

## [2026-07-11] Celery task failures are ACKED — no redelivery self-heal (plan 010, PR #627)

**Fact:** a Celery task that RAISES is acked even with `task_acks_late=True` —
`task_acks_on_failure_or_timeout` defaults True. Late-ack redelivery only covers worker
DEATH (`task_reject_on_worker_lost=True` in `app/worker.py`), not task failures.

**Where it bit (accepted, R3-B / OV-9 in plans/010-subtitled-sfx-overlay-lanes.md):**
during PR #627's rolling deploy, a caption save from an upgraded API can land on an old
worker that TypeErrors on the new task kwarg. The failed message is acked — no
redelivery. The variant sits "rendering" until the 60-min reaper (`app/tasks/reaper.py`)
converts it to a failed badge; the user recovers by re-tapping Apply. Judged a
one-deploy, minutes-wide window; a two-phase kwarg rollout was rejected as
over-engineering.

**Reusable rule:** adding a required kwarg to an existing Celery task always opens this
window. Either accept it consciously (document it + confirm the reaper backstop covers
the queue) or ship the kwarg with a server-side default the old worker tolerates.

## [2026-07-20] Smart Captions v2 review decisions (v0.11.0.0)

Internals: `docs/pipelines/smart-captions.md`. Four calls from the pre-merge review
worth keeping:

**Hook-caption suppression is deferred, not implemented.** The compiler sets
`hook_caption_suppression_eligible` when the preset's `hook_accumulation` layout says
`suppress_if_resolved` and enough hook visuals resolved, but `hook_caption_suppressed`
always stays false. Compile-time asset resolution is planning evidence only —
downloads, normalization, collision arbitration, and FFmpeg can all still fail later,
so suppressing speech captions there could ship a hook with neither captions nor
visuals. Suppression waits until both lanes share a transactional compositor that reads
the applied-media manifest; the persisted eligibility receipts show how often the
deferral actually matters. Follow-up tracked as TODOS.md T-SMART-COMP-1.

**Survivors vs manifest (review D4).** Persisted `media_overlays` after a Smart apply
pass = survivors with arbitration-resolved geometry PLUS download-failed cards with
their ORIGINAL payload (the next reburn retries them — a transient storage failure must
never permanently delete a creator's card). Arbitration-OMITTED cards are dropped: the
reburn path has no arbitration, so persisting them would resurrect the exact occlusion
the omission prevented. `media_overlays_applied_ids` is the separate record of what
actually reached the burned video. Guard:
`tests/smart_edit/test_v2_render_contract.py::test_media_overlay_persistence_keeps_failed_cards_drops_omitted`.

**Music-bed eligibility is a closed allowlist, not a filter.** A track reaches the v2
music bed only when an admin explicitly set `track_config.smart_captions_licensed =
true` (music Config tab toggle). Default is ineligible — licensing exposure from an
uncleared track landing in a creator's export outweighs bed coverage. Guard:
`test_music_eligibility_is_closed_and_requires_explicit_license`.

**`SMART_MUSIC_BED_ENABLED` is deliberately independent of `SOUND_EFFECTS_ENABLED`.**
The SFX lane is user-authored; the bed is agent-selected — an incident in one must be
killable without silencing the other. Off: new renders resolve no treatment and reburns
skip re-mixing the bed, but persisted `smart_music_treatment` state is never deleted,
so re-enabling restores creators' saved mixes (same preserve-on-rollback rule the SFX
lane follows).

## [2026-07-21] Worker OOM mid-reframe + 30-min silent redelivery gap (prod job e8173a25)

**Incident:** a 170MB / 134s high-bitrate clip OOM-killed worker 6e826515c714e8 during
`reframe_and_export` (last log `reframe_filter_chain` 18:26:17Z, silent death, fresh
Celery boot 18:31:41Z). Compounding: 7 `analyze_pool_asset` tasks had just run on the
same worker, leaving CLIP/torch/Whisper residency in the single long-lived prefork
child (concurrency=1) for the ffmpeg peak to stack on. acks_late +
visibility_timeout=1900s redelivered at 18:56:57Z and attempt 2 finished cleanly — so
recovery worked, but the user stared at healthy-looking "rendering" for 30+ minutes.

**Three-part fix (this entry is the narrative; invariants live in the guard tests):**

1. **Heavy-source downscale guard** — `app/pipeline/source_guard.py`, wired into
   `_ingest_clips` (generative_build.py). SDR sources with short edge > 1920px are
   re-encoded ONCE at ingest (2-thread decode cap, h264 crf16/fast, cover-scale of
   1080x1920, never upscaled, audio stream-copied, original deleted from tmpfs), so
   every downstream per-slot reframe decodes a bounded intermediate instead of native
   4K HEVC — and Gemini uploads shrink too. HDR is excluded: `_pretonemap_hdr_clips`
   already downscales HDR inside its zscale chain, and an 8-bit re-encode here would
   destroy its input. Still images excluded (image_clip owns those). Kill switch:
   `SOURCE_DOWNSCALE_GUARD_ENABLED=false` + worker restart (byte-identical off).
   Guards: `tests/pipeline/test_source_guard.py`. Template/music ingest paths NOT
   wired yet — follow-up if the class recurs there.

2. **Prefork child recycling** — `worker_max_memory_per_child` in `app/worker.py`
   (`WORKER_MAX_MEMORY_PER_CHILD_KB`, default 3GB, 0 disables). Recycles the child
   BETWEEN tasks once RSS exceeds the threshold; the replacement forks from the parent
   and keeps the prewarmed CLIP singleton via copy-on-write. A dedicated queue/machine
   for analysis tasks was rejected: concurrency=1 already serializes execution — the
   problem was residual memory, not co-execution. Guards:
   `tests/tasks/test_worker_memory_recycle.py` (conf carries the value; threshold
   stays under the fly.toml worker VM size).

3. **User-visible retrying state** — `jobs.worker_heartbeat_at` (migration 0068) is
   ticked ~30s by a daemon thread (`job_phases.job_heartbeat`, column-only UPDATE by
   design — it must never read-modify-write assembly_plan) wrapped around
   `orchestrate_generative_job`. The status route computes `retrying: true` at READ
   time when a processing/rendering job's beacon is older than
   `RENDER_HEARTBEAT_STALE_AFTER_S` (150s); the redelivered attempt's synchronous
   entry beat clears it immediately. ProgressTheater swaps the leave-note for honest
   recovery copy. NULL beacon never flags (legacy rows / non-heartbeating
   orchestrators). Guards: `tests/routes/test_generative_retrying.py`,
   `src/apps/web/src/__tests__/progress/retrying.test.tsx`.

**Env vars (not in CLAUDE.md — its 38k budget was full at the time):**
`SOURCE_DOWNSCALE_GUARD_ENABLED` / `SOURCE_DOWNSCALE_SHORT_EDGE_MAX` /
`SOURCE_DOWNSCALE_FFMPEG_THREADS`, `WORKER_MAX_MEMORY_PER_CHILD_KB`,
`RENDER_HEARTBEAT_INTERVAL_S` / `RENDER_HEARTBEAT_STALE_AFTER_S`. Apply on Fly:
`fly secrets set <VAR>=<val> --app nova-video` + `fly machine restart <id>` (worker).

### [2026-07-22] Pre-merge review hardening of the OOM fixes (same branch)

/review (7 specialists + red team + adversarial) confirmed the design and
forced these changes before merge — all shipped in the same PR:

- **Guard aggregate budget:** per-clip timeouts alone let 20 heavy clips ×
  serial re-encodes eat the orchestrator's soft_time_limit (the d30c61fe
  serial-preprocessing class). `_GUARD_TOTAL_BUDGET_S=900` now bounds the
  whole pass; overflow clips keep originals + trace event. Serial stays
  deliberate — parallel conversions would double the peak memory the guard
  exists to bound.
- **Guard coverage widened:** `_prepare_timeline_assembly` (timeline re-render
  decodes durable ORIGINALS — would have reproduced the incident verbatim) and
  the bed-level reburn now run the guard too. AAC-transcode retry when `-c:a
  copy` can't mux into .mp4 (PCM/.mov, Opus) — the silent-skip class. Failure
  branch now emits a pipeline-trace event + deletes the partial tmpfs file.
- **tmpfs orphan sweep:** a SIGKILL'd child's TemporaryDirectory survives on
  RAM-backed /tmp into the redelivered attempt (invisible to
  worker_max_memory_per_child — not process RSS). `task_prerun` →
  `app/pipeline/tmp_sweep.py` sweeps nova* dirs older than 1850s; the cutoff
  invariant (> every render time_limit 1800, ≤ visibility_timeout 1900) is
  pinned by `test_tmp_sweep_cutoff_stays_inside_redelivery_window`.
- **Heartbeat honesty:** beacon written with `func.now()` (DB clock — worker/API
  VM skew shifted the 150s window); `retrying` now has an UPPER bound
  (visibility_timeout + stale + 300s slack) because a hard-time_limit SIGKILL
  ACKS the message and no redelivery ever comes — past the window the reaper
  owns the row. Threshold floors at 2× beat interval (misconfig guard).
  Beats also refresh `updated_at` via the model's onupdate — documented as
  deliberate, not a leak.
- **worker_max_memory_per_child semantics:** billiard compares lifetime PEAK
  RSS (ru_maxrss), not current residency — one >3GB spike recycles the child
  even if freed. Deliberate, but validate in prod via billiard's "child
  process exceeding memory limit" log line; every-task recycling ⇒ raise it.
- **Frontend:** EditPayoff (onboarding) was a missed ProgressTheater call site
  — a dead first-render attempt showed "About 90 seconds" indefinitely; ETA
  label suppressed while retrying; recovery note is an aria-live status
  region; contradictory static reassurance lines gated off while retrying.

**Known accepted gaps (documented, not fixed):** variant re-render/reburn tasks
(`regenerate_generative_variant`, caption/bed reburns) do NOT heartbeat and run
while job.status is terminal, so a dead re-render attempt still shows
render_status="rendering" until the boot-time variant reconciler — a
variant-level beacon is future work. Template/music ingest paths still lack the
downscale guard (separate task chip). Guard conversions re-run on every
swap-song/retext regen (clip_metas re-analysis already does; cacheable later).

---

## [2026-07-26] Vendor the face-detection Haar cascade instead of trusting cv2's wheel data dir

**Decision:** Ship `haarcascade_frontalface_default.xml` in the repo/image at `src/apps/api/assets/cv/` and resolve it via `resolve_face_cascade_path()` (`app/pipeline/face_sampler_worker.py`), preferring the bundled asset over `cv2.data.haarcascades`.
**Why:** Prod job `1e768d5b-3c82-499e-9063-c25449562844` showed `worker_error: rc_1 ... Can't open file '/usr/local/lib/python3.11/site-packages/cv2/data/haarc...'` — face-aware caption/thumbnail placement silently fell back to preset placement (`reason=sampler_error`) on every prod job. Root cause: `pyproject.toml`'s documented collision (mediapipe pulls `opencv-contrib-python` alongside `opencv-python-headless`) means whichever `cv2` package wins dependency resolution in the built image, its `cv2.data.haarcascades` directory does not reliably ship the cascade XML — a wheel-packaging detail outside our control that only reproduces in the built image, not local dev. `_load_cascade()` also now checks `cascade.empty()` and raises `FaceCascadeLoadError` naming the resolved path instead of silently constructing an always-empty classifier (the class of failure that produced the opaque `rc_1` in the first place).
**Revisit if:** the opencv/mediapipe dependency collision gets a real fix upstream (e.g. mediapipe drops its own opencv pin) — at that point the vendored asset becomes a belt-and-braces fallback rather than the primary path, but keeping it costs ~1MB and removes a wheel-packaging dependency either way.

## [2026-07-27] Behind-subject glitch: RVM backbone + boundary resets + oscillation gate (prod job add80a9c)

Plan item 9f51eee2 (4-clip landscape beach montage, one behind_subject
element spanning all clips) rendered with visibly strobing occlusion. The
matte showed the selfie segmenter's confidence oscillating en masse every
~5–9 frames (mask area 7%↔63% — sand/rock read like skin), which the 3-frame
median can't suppress and BOTH sanity gates missed: presence never flipped
(mask never vanished; 4 flips @ 0.365/s under the AND-gate) and the median
IoU stayed 0.927 (~15 jump pairs hidden among 308 stable ones). Two more
mechanisms compounded it: the temporal median carried ~2 frames of the
previous clip's silhouette across every slot cut, and the 0.25s window pad
(7.5 frames) put mask_at on a half-frame offset where banker's rounding
repeats/skips mask indices every ~3 frames.

Decisions:
- **Swap the segmentation backbone to RobustVideoMatting** (onnxruntime CPU,
  recurrent) — measured on the exact failing footage: median adjacent-frame
  IoU 0.980 vs the flapping selfie-segmenter mask, 30fps on M-series CPU at
  downsample 0.25. Mediapipe stays as the automatic fallback +
  `MATTE_RVM_ENABLED=false` kill switch. RVM weights are **GPL-3.0**: used
  server-side only, never distributed to users, and our inference code is
  written fresh against onnxruntime — acceptable; revisit if we ever ship
  the model client-side.
- **Reset temporal state at known cuts** (`cut_boundaries_s` threaded from
  variant timelines / assembly plans into `compute_subject_matte`) instead of
  detecting scene cuts in the matte engine — the orchestrator already knows
  the exact slot joins.
- **Add the large-jump oscillation gate** (adjacent-pair IoU < 0.5 count +
  rate AND-gate, cut pairs excluded) so any backbone that oscillates falls
  back to text-in-front rather than shipping a strobing occlusion.
- **Version the matte cache key** (`.matte.v2.mp4`): old cached mattes may be
  glitching mattes the old gate accepted; suffix mismatch = cache miss +
  recompute + best-effort v1 delete. Chosen over sidecar re-validation (old
  sidecars lack the new stats — defaults would always pass).

Trade-off accepted: RVM mattes people, not arbitrary objects — the "behind
the rock" look on clip 1 of the beach montage was the broken model
hallucinating and is gone. Behind-arbitrary-objects would need a
salient-object/video-segmentation model (follow-up if ever wanted).

Ship-review addenda (same day): pre-downscale frames to 0.25 natural aspect +
`downsample_ratio=1.0` (kills the discarded full-res guided-filter/fgr work —
review measured the full-res path infeasible for the 90s budget on Fly
vCPUs); ORT threads pinned (2 intra-op, spinning off) for the shared-vCPU
worker; windows totalling >1800 ticks fall back to mediapipe up front; a
definitive sanity-gate rejection persists a `.matte.v2.unstable` sentinel so
reburns of the same base stop re-paying the recompute (transient failures
still retry); matte-migration deletes are prefix-guarded to
`generative-jobs/*.matte.*`.

## [2026-07-30] Render-attempt clock: anchor at Save, not at worker pickup (v0.18.1.1)

A re-render of a 5-minute edit came back reading "40m 32s". Every clock in the
progress band was pinned to the FIRST render, because a re-render reuses the
SAME `Job` row and nothing moved the anchors:

- `job.started_at` — written only by `job_phases.mark_started`, which refuses to
  move it once set (correct for its own job: it models worker pickup of ONE
  orchestrator run, and a Celery redelivery must not restart a clock mid-render).
- `variants[i]["render_started_at"]` — written in exactly ONE place repo-wide,
  the initial render loop in `tasks/generative_build.py`.

Both are read as live wall clocks: the elapsed counter, the ETA, the "taking
longer than usual" stall copy, and the per-variant tile all count from them. So
after the first render finished, every subsequent Save produced a counter
counting from the original render, an ETA floored at "less than a minute", the
stall hint firing instantly, and a receipt reading "Ready in -36:-12" (because
`finished_at` was still ahead of `started_at`).

Decisions:

- **Anchor at DISPATCH, not at worker pickup.** The user's mental model is the
  Save press, and queue wait is time they are genuinely waiting. Two helpers in
  `app/services/job_phases.py`: `mark_reattempt(job)` (moves `started_at` to
  now, returns whether it moved) and `stamp_variant_attempt(variant)` (sets
  `render_status="rendering"` + a fresh `render_started_at`). Wired into all
  **16** re-render dispatch paths in `routes/generative_jobs.py` +
  `tasks/autoplace.py`.
- **`mark_reattempt` takes the ORM object, `stamp_variant_attempt` takes the
  variant dict** — deliberately unlike the `job_id`-keyed helpers around them.
  Route dispatchers already hold a loaded (often row-locked) `Job` and commit
  themselves; and several dispatchers build `updated = dict(v)` then assign
  `variants[i] = updated`, so a helper that re-walked the job's variant list
  would mutate the original and have its write silently overwritten by that
  assignment. Passing the dict the caller actually persists makes that class of
  bug unrepresentable.
- **Skip the reset while an orchestrator run is in flight** (`current_phase` is
  non-None until `mark_finished` clears it). Editing an already-ready variant
  while its siblings are still on their FIRST render would otherwise move the
  anchor out from under that run: every later `record_phase` would compute
  `t_offset_ms` against the new origin (non-monotonic `phase_log`) and the
  whole-job clock the user is watching would visibly jump back to zero.
- **Never touch `finished_at`.** `plan_items.py` exports it as the plan item's
  ready date and no re-render task calls `mark_finished`, so nulling it would
  erase that date permanently. Readers guard on `started_at > finished_at`
  instead — `deriveReceiptText` (`components/progress/logic.ts`) returns the
  generic "Your edits are ready" whenever the pair can't yield an honest
  duration. Rejected: nulling `finished_at` at dispatch (data loss), and adding
  a parallel `attempt_started_at` column (a migration plus a second anchor every
  reader would have to learn, for a value `started_at` already means).
- **Gen-id minting stays in `_mark_variant_rendering`** and was NOT folded into
  the stamp helper: `_update_variant_entry` discards any worker write whose
  token differs from the stored one, so minting a token on a dispatch path whose
  task doesn't carry it would strand the variant in "rendering" forever.
- **Structural (AST) guard, not a grep.** The bug existed because 14 hand-rolled
  `render_status = "rendering"` blocks had no choke point, and NONE of them wrote
  the timestamp — at the base commit `render_started_at` appears in
  `routes/generative_jobs.py` only as a Pydantic field default, and not at all in
  `tasks/autoplace.py`. A source-grep guard matches one exact spelling of one
  line, so the same write in ANOTHER module slips through — which is exactly how
  `tasks/autoplace.py` diverged. `tests/routes/test_render_attempt_clock.py`
  walks the AST of both dispatch modules instead. Known residual: the raw-write
  guard matches `variant["render_status"] = "rendering"` assignments, so a
  `.update({...})` call or a whole-dict literal would still evade it; the pairing
  guard (stamp ⇒ reset) catches those in any function that also stamps.

Frontend half of the same bug: a re-render does NOT move `job.status` off
`variants_ready`, so a "terminal status wins" poll predicate stopped polling the
instant the user pressed Save (new video only appeared after a manual reload),
and `ProgressTheater`'s collapsed band never reopened on the same mount.
`isGenerativeJobSettled` (`lib/generative-api.ts`) is now the single predicate
for all three surfaces (item page, public `/generative`, onboarding EditPayoff):
non-terminal ⇒ not settled; a FAILED terminal ⇒ settled regardless of variant
state (a variant frozen in "rendering" after a failed job is a backend
data-integrity gap and must never block the UI); a SUCCESS terminal ⇒ not
settled while a variant is genuinely rendering, where "genuinely" means inside
the 30-minute `STUCK_RENDER_CEILING_MS`. The ceiling exists because
`reconcile_stuck_variants` only heals a stranded variant after ~60 min — without
it the UI would poll a dead render forever with a live-ticking timer.

Accepted costs (all tracked in TODOS.md, none silently degraded):

- **Admin render-timing breakdown is last-attempt only.** `admin_jobs.py`
  computes `queue_wait_ms = created_at → started_at` and
  `processing_ms = started_at → finished_at`. Once `started_at` moves at every
  dispatch, the first measures "job creation → last Save" and the second covers
  only the last attempt. `started_at` is the user-facing wall clock first and an
  admin metric second; per-attempt truth lives on the variants.
- **A dead re-render now climbs confidently.** `_compute_retrying` gates on
  `job.status in {processing, rendering}` plus an orchestrator-ticked heartbeat,
  and a re-render leaves `job.status` terminal. Before this change the lie was
  at least static (frozen counter, stopped poller); now it updates smoothly
  until the 30-minute ceiling. The fix makes a known gap more visible rather
  than creating one.
- **Bar and ETA still read the first render's phases.** No re-render task calls
  `record_phase`, so a null `currentPhase` pins the bar under 5% and
  `get_baselines("generative")` advertises the full-pipeline estimate for a
  20-second text reburn.
- **Client-vs-API clock skew got sharper, not worse.** The anchor used to be
  minutes in the past (skew invisible); it is now "a moment ago", so a browser
  clock trailing the API clock parks the counter at "0s" for the skew window.

**Revisit if:** the re-render tasks start driving `record_phase` /
`mark_finished` and heartbeating. At that point `job.status` and the phase log
cover re-renders the way they cover first renders, the 30-minute frontend
ceiling becomes a backstop rather than the only bound, and `phase_log` needs an
origin-aware reader (it will carry entries written against two `started_at`
origins).

## [2026-07-30] Intro placement: persist what the render resolved; readers mirror the renderer (v0.18.1.3)

Two bugs with one shape — something downstream of the burn re-derived a value
the render had already resolved, and guessed wrong. Mechanics live in
`docs/pipelines/generative.md` ("Intro placement snapshot"); the reusable rules
are here.

**1. The resolved placement was never written down.**
`_resolve_intro_overlay_params` folds knobs > curated set > agent advisory into
one placement, but nothing persisted the result. The editor's read adapter
(`_base_text_elements_for_variant`) had to reconstruct it and always landed on
`generative_overlays._DEFAULT_POSITION` — so a curated set or an
`overlay_format_matcher` run that chose `bottom` burned at the bottom while the
editor drew the hook mid-frame, and saving baked the wrong spot in.
`_intro_placement_from_params` now snapshots position / fracs / max_width /
anchor / rotation onto `variants[i]["intro_placement"]`.

- **`None` for the plain centered placement.** Most variants are centered;
  persisting a dict for all of them would change the stored shape of every
  variant to buy nothing, since the adapter's legacy path is already right
  there. The one exception is a variant carrying `text_placement_candidates` —
  the legacy fallback reads those candidate fracs, so even a centered
  resolution has something to disagree with and must be recorded.
- **Fold the persisted position back at the ADVISORY tier, and only when it is
  not "center".** A no-LLM re-render rebuilds `agent_form` without the agent's
  original advisory, so without the fold the first text edit silently
  re-centered a `bottom` intro. Folding it as an advisory (not an override)
  keeps knobs and curated sets winning exactly as they did on the first render.
  Folding "center" is deliberately suppressed rather than treated as a harmless
  no-op: it would resolve `style["position"] == "center"`, flip
  `has_explicit_position` True, and make the resolver skip the
  placement-candidate branch — silently dropping a masonry intro's
  whitespace-pocket fracs on re-render.

**2. `_finalize_job`'s allowlist is a trap worth stating once.** Finalization
rebuilds each variant from an explicit key list, so a new per-variant key is
DELETED on the first render unless it is added there. This is the third time
the repo has paid for rediscovering it (plans/007 autoplace, plans/010 silence
cut, now `intro_placement`) and it fails in the worst way: the feature works
end-to-end in the render, then the value vanishes at the finish line. Any PR
adding a variant key adds it to the allowlist AND pins it with a
`test_finalize_job_preserves_*` guard — here,
`test_finalize_job_preserves_intro_placement`.

**3. A burn-dict reader must mirror the renderer's fallback table, not invent
0.5.** The renderer-parity invariant (2026-05 #296/#297) says every burn-dict
field must be honored by both renderers. This is its mirror image: anything
that reads a burn dict back — the editor adapters, not just the renderers — has
to fall back the way `_resolve_anchor` does. `_burn_dict_position` hardcoded
`y = 0.5` for a HALF-pinned overlay (x set, y null), but `_POSITION_Y["center"]`
is 0.45 and `_POSITION_Y["bottom"]` is 0.85. `_burn_dict_position` is shared, so
the silent second victim was lyric seeds: `word_reveal`, `typewriter`,
`ai_answer`, and `lyric_word_pop_punchy` all pin x with a null y, and
`GET .../lyric-seeds` is a PERSIST path, so the invented y got saved. Guard:
`test_seed_elements_half_pinned_style_set_uses_the_renderers_y`.

**4. Do not truthiness-coerce a placement frac.** `0.0` is meaningful and
reachable — `position_x_frac` is `ge=0.0` on the knob route and
`_rotation_for_empty_pocket` returns `rotation_deg=0.0` for every non-portrait
masonry pocket. Coercing a falsy frac to a default re-centers the block. The
`value or fallback` pattern in the adapter applies to the two STRING keys
(`position`, `text_anchor`) only.

## [2026-08-02] Worker VM reverted to shared-cpu-4x — the performance upgrade paid for an unused flag

The 2026-06-13 bump of the `worker` process from `shared-cpu-4x/6144MB` to
`performance-4x/8192MB` (~$33/mo → ~$155/mo) was justified by two things: faster
single renders, and making `GENERATIVE_PARALLEL_VARIANTS_ENABLED` safe (concurrent
FFmpeg encodes need real cores, not oversubscribed shared vCPUs — see the
`d018d1c3` incident referenced in `fly.toml`). Seven weeks later, that flag was
still `default=False` in `config.py` and absent from `fly secrets list` — it was
never turned on. With `--concurrency=1` hard-pinned regardless, 3 of the 4
dedicated vCPUs were paid-for idle capacity the entire time.

A Fly-cost audit (Aug 2026) found the bill was ~$172/mo with this one machine at
90% of it, running at a 0.63% duty cycle (4.58 render-hours billed as 730 in July).
Reverted `fly.toml` to the pre-6/13 sizing — zero behavior change, since the
capability the bigger VM existed for was never in use — recovering ~$122/mo (81%
of a ~$150/mo total cost-cut plan) with a one-line change and no new failure modes.

**Sequencing note, not a reversal of the original upgrade's logic:** the plan to
scale the worker to zero (stop it when idle, start it on demand) is a separate,
larger follow-up. Once that ships, the worker's *active* hours drop to roughly the
same ~5/month regardless of VM class, so the cost difference between
shared-cpu-4x and performance-4x collapses to well under $1/mo — at that point
performance-4x should come back, and `GENERATIVE_PARALLEL_VARIANTS_ENABLED` should
actually be flipped on, since there's no longer a real cost tradeoff against doing
so. This entry documents the interim state: performance capability off, flag off,
cost down. Revisit trigger: when the scale-to-zero follow-up lands.

## [2026-08-02] Render-worker autostop — app-controlled Fly Machines start/stop

Follow-up to the VM revert above. Same cost audit, same plan doc
(`~/.claude/plans/this-month-our-fly-io-twinkling-peach.md`), the harder ~$28/mo
of the ~$150/mo total: the `worker` machine now stops itself when idle and
starts back up on demand, instead of running 24/7 regardless of the ~0.63%
duty cycle the audit measured.

**Fly has no built-in for this — verified, not assumed.** The obvious
built-in, `fly-autoscaler`, was evaluated first and rejected: its own sample
config has no scale-down key, and a Fly staff reply on the community forum to
this exact question ("how do I auto stop/start a Celery worker") states
plainly that fly.toml has no config for it. The stop half has to be
hand-rolled; there is no shortcut.

**Design, in the order the pieces depend on each other:**

1. **Split the lanes first (Phase 1).** A `light` process (fly.toml,
   shared-cpu-1x/512MB) now runs everything Celery Beat schedules —
   `task_routes` in `worker.py` (`MAINTENANCE_TASK_NAMES`) sends every
   Beat-triggered task there instead of the render worker's queues. Before
   this, the 5-min `sweep_stale_jobs` entry alone guaranteed the worker was
   never idle for more than a few minutes — autostop is structurally
   impossible without this split. Deliberately narrow: analysis tasks that
   download media or load torch (`match_pool_clips`, `agentic_template_build_task`,
   etc.) stay on the render worker; they'd OOM a 512MB box.

2. **One idle-check, shared by two different questions.** `render_worker_idle()`
   (`queue_state.py`) answers "is there ANY render-queue work anywhere" —
   an aggregate check. The existing reaper's `_live_job_ids` answers "is
   THIS specific job_id live" — a per-job check. These do NOT share a
   function despite both meaning "check if Celery is doing something" —
   forcing the reaper to call an aggregate idle-check would answer the
   wrong question for its purpose. What they DO share: `RENDER_WORKER_QUEUES`,
   one constant, so "what counts as a render-worker queue" can't drift
   between the two call sites (plus a third: the wake-hook's routing_key
   filter, below).

3. **The wake hook filters by queue, not task name — on purpose.** The
   obvious design (hook the wake call into `job_dispatch.enqueue_orchestrator`,
   the existing first-render dispatch chokepoint) was checked against the
   actual call sites and rejected: 17 of ~24 render-task dispatches
   (`regenerate_generative_variant`, `reburn_narrated_captions`, etc. — swap-song,
   retext, bed-level, caption-language actions in `generative_jobs.py`) call
   `.delay()`/`.apply_async()` directly and bypass that helper by design
   (they act on an *existing* job, not a first render). A hand-curated
   task-name list for the wake hook kept missing tasks too — even the
   *analysis* tasks that run on this same worker (clip/track analysis)
   aren't render tasks by name but still need the wake. The fix: one
   `before_task_publish` signal in `worker.py`, filtered on `routing_key`
   (empirically verified to equal the target queue name in this app's
   config — no custom exchange topology declared). This mirrors Celery's
   actual dispatch mechanism exactly, closing the whole bypass class
   structurally rather than patching each site.

4. **The wake hook must never block the request path.** Most dispatch sites
   run inside FastAPI async handlers on the public `api` process. The
   Fly API call fires from a background thread with a tight timeout,
   debounced via a short-TTL (60s) Redis key — deliberately much shorter
   than the 10-min idle grace period, so a "we already woke it" skip can
   never coincide with a legitimate stop-then-restart within its own
   window. This is why no live Fly-state check is needed on the hot path:
   the debounce alone is correct, and the periodic lifecycle backstop
   (below) is what actually guarantees a missed/failed wake gets recovered.
   Fails OPEN (calls Fly anyway) on any Redis error — an extra harmless API
   call beats a silently-skipped wake.

5. **The stop/backstop lifecycle task (`tasks.manage_render_worker_lifecycle`,
   every 2 min on `light`) is the actual correctness guarantee**, not the
   wake hook. It stops the machine after `RENDER_IDLE_GRACE_MIN` (10 min)
   of continuous idleness, and — this is the part that bounds every other
   failure mode in this design — starts it back up if there's real work
   and it isn't already running, regardless of whether the wake hook fired,
   fired but failed, or was never reached at all. Idle-duration is tracked
   across ticks via a Redis timestamp (`_decide_lifecycle_action`, a pure
   function with no I/O, tested exhaustively) since Beat itself carries no
   state between firings.

6. **A new failure mode this design creates: Beat's death now blocks all
   renders.** Before autostop, if Beat died, the render worker kept running
   and kept consuming jobs — Beat's death only affected maintenance/cleanup.
   After autostop, the worker spends most of its life stopped, and the
   *only* thing that restarts it on a missed wake is Beat's own lifecycle
   task. If Beat dies while the worker happens to be stopped, there is no
   path back — every new render sits `queued` forever, silently (the API
   still returns 200 on job creation). The first fix tried — fold a
   heartbeat into the existing daily-digest task — doesn't actually work:
   that task is *itself* Beat-scheduled, so it shares Beat's exact blind
   spot and can't detect Beat's own total absence. Confirmed no
   external-to-Beat monitoring exists anywhere in this codebase (no
   scheduled GitHub Actions workflow; the existing "dead-man's-switch" in
   `send_daily_digest.py` has the identical blind spot). The actual fix:
   `GET /health/beat` (`main.py`) reads a Redis timestamp any Beat-scheduled
   task writes on success via a `task_success` signal
   (`BEAT_SCHEDULED_TASK_NAMES`, derived from `beat_schedule` itself so a
   future entry is covered automatically) — meant to be pinged by a service
   OUTSIDE this app entirely (UptimeRobot, cron-job.org, a scheduled GH
   Actions workflow), since that's the only kind of check immune to Beat's
   own death. [2026-08-04: that external pinger now exists —
   `.github/workflows/beat-health.yml`, every 15 min; see the 2026-08-04
   entry below for the incident its two-day absence allowed.]

7. **The Fly API token now lives on the public `api` process, not just
   `worker`/`beat`** — the wake hook fires from FastAPI request handlers.
   Scope `FLY_API_TOKEN` narrowly (app-scoped to nova-video, never a full
   personal/org token): if the internet-facing process is ever compromised
   through some unrelated vulnerability, the blast radius should be
   "control of this one app's machines," not "full Fly org access."

**Deliberately deferred, not forgotten:** generative clip-ingest cache reuse
and baking CLIP weights into the image (both TODOS.md, 2026-08-02 entry) —
real, but neither required for the cost win, both needing their own pass.
Merging `beat` into `light` to save an additional ~$2/mo was considered and
explicitly rejected — it would concentrate the exact Beat-liveness SPOF from
point 6 onto a single machine for a saving too small to justify it.

Kill switch: `RENDER_AUTOSTOP_ENABLED`, default `false` — byte-identical to
pre-autostop behavior when off. Restoring `performance-4x` and flipping
`GENERATIVE_PARALLEL_VARIANTS_ENABLED` (the VM-revert entry's Phase 3) is a
deliberate later step, gated on this autostop design being verified in prod
first — not bundled into this same change.

## [2026-08-02] Worker VM re-reverted to performance-4x — the downsize missed the other justification, and prod proved it within an hour

The shared-cpu-4x downsize above was live for less than an hour before it
caused a real render failure: job `6bdb58dc-5933-4e82-b55c-ba2de00da19c` (the
first render dispatched after the downsize deployed) failed when its Skia
PNG-sequence text-overlay burn (`text_overlay_skia.py:2025`,
`subprocess.run(..., timeout=600)`) exceeded its 600s ceiling and was killed.
Two of its stages timed out this way (916s and 616s elapsed before failing).

**The downsize's reasoning was incomplete, not wrong about the flag.** The
2026-06-13 performance-4x upgrade had two stated justifications:
`GENERATIVE_PARALLEL_VARIANTS_ENABLED` safety (genuinely unused, correctly
identified) and single-render speed — *"shared cores are
oversubscribed/throttled, so every FFmpeg encode + the Skia per-frame PNG
render runs slower than the vCPU count suggests. Dedicated cores make each
render materially faster."* The cost-cut plan treated "the flag was never
used" as license to downsize, without separately verifying the second
justification didn't matter on its own. It did — this incident is a direct,
fast confirmation of the original upgrade's own documented rationale, on
exactly the workload type (Skia PNG-sequence overlay burns) it named.

**Confidence level, stated plainly:** high but not proven by a controlled
comparison. The mechanism (oversubscribed shared vCPUs run CPU-bound Skia/
ffmpeg work slower), the timing (first render after the downgrade), and the
pre-existing first-party claim about this exact workload all point the same
direction. What's missing: a byte-identical rerun of the same input on both
VM classes — Fly's shared-vCPU throttling happens at the hypervisor level
and isn't visible from inside the guest (`/sys/fs/cgroup/cpu.stat` and
similar aren't present in a Fly Firecracker guest), so this couldn't be
confirmed directly from the machine itself.

**Why revert immediately instead of gathering more evidence first:** a
failed render is a real user-visible cost (a lost video, and the current
recovery path requires the user to notice and retry). Continuing to run
degraded while collecting more failures to be sure would trade a small,
already-large-enough amount of evidence for actual harm. Reverting costs
$122/mo for a matter of days, until `RENDER_AUTOSTOP_ENABLED` is verified —
at which point the worker's active hours drop enough that this VM-class
choice barely matters on cost either way (see the scale-to-zero entry
above), so this reversal has no long-run cost, only a short deliberate delay
on banking part of the savings.

**Lesson for future cost-cut work on this codebase:** when a resource
upgrade has multiple stated justifications, downsizing requires checking
that NONE of them still apply — not just the one that's easiest to prove
unused. "Unused capability" and "unused VM class" are different claims;
this incident is what happens when the first gets treated as proof of the
second.

## [2026-08-04] Frozen "Starting…" on Generate — mint the Job in-request, don't shorten the queue (plans/014, v0.23.2.0)

A mobile user tapped Generate on a plan item and sat on a disabled
"Starting…" button for ~8 minutes (plan item `f2b9201d`, job `4fb8fa0f`).
Root cause, prod-verified from the admin API: `POST /plan-items/{id}/generate`
didn't create the Job row — it only enqueued the `generate_plan_item_videos`
Celery task on the default `celery` queue, and the Job (whose existence is
what flips `derive_item_status()` to `"generating"`) was minted by that task.
The single `worker` machine (`--concurrency=1`, draining `celery` +
`plan-jobs` + `overlay-jobs`) had an in-flight render (`61c4d859`) holding
the slot, so the tiny Job-minting task waited behind it head-of-line. FIFO
drain signature: `61c4d859` finished at 11:25:35.99Z and the user's Job was
created 244 ms later. Corroboration: content_plan jobs routinely showed
multi-minute `created→started` gaps, while public generative jobs'
`created_at` is the click time — because `POST /generative-jobs` builds the
Job synchronously in the route. Ruled out: render-worker autostop (not
enabled in Fly secrets), mobile-specific frontend paths, poll caching.

**Decision — make the wait visible, not shorter.** The fix ports the public
generative-jobs pattern: Generate mints the Job in-request
(`dispatch_item_render_for` in `content_plan_build.py`, run via
`anyio.to_thread.run_sync`), so the page's immediate post-tap refetch flips
to the progress view and shows an honest "queued" phase. The single-slot head-of-line
blocking itself is deliberately NOT fixed here — worker topology is a cost
decision (second machine / analysis lane split) deferred to TODOS.md
("Render-worker queue latency" section). Kill switch:
`PLAN_SYNC_DISPATCH_ENABLED=false` restores the legacy `.delay()`+409
contract byte-identically.

**Sub-decisions worth keeping:**

- **One dispatch entry point, route AND task.** `dispatch_item_render_for`
  (SELECT … FOR UPDATE on the item + active-render re-check) is shared by
  the route thread and the Celery task body, so the two paths can't drift.
  The activation loop applies the same lock+re-check inline — every minting
  path locks, which is the duplicate-mint guard since
  `jobs.content_plan_item_id` deliberately has no uniqueness constraint
  (retries mint new rows).
- **Duplicate Generate ⇒ idempotent 200, not 409.** A lost-response mobile
  retry self-heals into the render that's already running instead of
  stranding the user on an error banner.
- **Publish-failure containment is CONDITIONAL on `status == "queued"`.**
  `apply_async` raising does not prove Redis rejected the message; if the
  worker already flipped the job to `processing`, an unconditional
  `processing_failed` write would mark a RUNNING render failed and invite a
  duplicate. rowcount 0 ⇒ the worker owns it ⇒ report dispatched. A genuine
  publish failure flips the Job terminal (`dispatch_publish_failed`) because
  the reaper deliberately never reaps `queued` — an uncontained failure
  would strand a forever-"generating" ghost.
- **Status buckets single-sourced** in `app/services/job_status.py`:
  `plan_items.py`'s hand-copied bucket had drifted from `me.py`'s (missing
  `template_ready`/`music_ready`), so a template/music job pinned to a plan
  day read as "generating" forever AND blocked new generates. The dispatch
  re-check and the read path must share one terminal set or a failed item
  refuses to re-generate / a live render double-dispatches.

Full plan + prod evidence table + review deltas: `plans/014-sync-plan-generate-registration.md`.

## [2026-08-04] Light machine OOM'd into a permanent `stopped` — gate worker_ready hooks by consumed queue

Two days after the autostop lane split (#764), the `light` machine
(`48e2547a50d138`) was found `stopped` with the state surviving every deploy —
meaning the 5-min stale-job sweeper, the daily digest, and all TikTok
periodics had silently been dead since Aug 2 ~12:33Z. `/health/beat` was
returning 503 with age ≈ 55h; nothing alerted.

**Root cause (Fly Prometheus, org `emir-erben`):** `worker_ready` signal
handlers in `app/worker.py` fire in EVERY celery worker process, so
`_prewarm_clip_font_matcher` loaded torch + open-clip ViT-B/32 (~450-600MB
peak per `clip_font_matcher.py`'s own docstring) on the 512MB light VM. The
machine ran with ~7MB available from its first minute, thrashed through ~13
OOM kill/reload cycles between 11:49Z and 12:33Z, exhausted flyd's
`on-failure, max_retries: 10` restart budget, and parked `stopped`. Two
compounding Fly behaviors made that permanent and invisible: `fly deploy`
updates a stopped machine but leaves it stopped (observed directly in the
machine's event log: `launch` → `stopped` on every subsequent deploy), and a
machine's event history is truncated on update, so the OOM evidence was gone
from `fly machine status` — the memory timeline had to come from the
Prometheus API.

**Decisions:**

- **Gate the prewarm by consumed queues, fail-closed** —
  `_worker_consumes_render_queues` (worker.py) checks
  `sender.app.amqp.queues.consume_from` against `RENDER_WORKER_QUEUES` and
  skips the prewarm for maintenance-only workers OR when introspection
  fails. The asymmetry that justifies fail-closed: a wrongly-skipped prewarm
  costs one ~3-5s lazy load on the render worker; a wrongly-run prewarm
  kills the light machine. Pinned by `tests/test_worker_prewarm_gate.py`.
  The fly.toml "light never loads torch" rule only ever covered task
  routing; boot-time signal hooks were the uncovered class, and any future
  `worker_ready` hook that loads a model must use the same gate.
- **light 512 → 1024MB** (fly.toml) — even mid-thrash the baseline (celery
  parent + 2 prefork children of the full app import) left single-digit MB
  available; 512 was sized against the tasks, not the worker shape.
- **Wire the external dead-man's switch that was designed but never
  deployed** — `.github/workflows/beat-health.yml` pings `/health/beat`
  every 15 min. `beat_heartbeat.py` predicted this exact blind spot ("a
  check that is itself Beat-scheduled shares Beat's exact blind spot") and
  the endpoint existed; the outside-the-app pinger was the missing half, and
  its absence is why the outage ran 2+ days undetected.
- **Recovery is NOT just `fly machine start`** — the stopped machine still
  runs the old image; starting it without this fix re-enters the same OOM
  loop (it survived ~66 min on Aug 2). Deploying this change recreates/
  updates the machine config; if it stays `stopped` after the deploy, one
  `fly machine start 48e2547a50d138 -a nova-video` brings the lane back on
  the fixed image (the ID is valid as of 2026-08-04 — a recreating deploy
  mints a new one, so resolve the current `light` machine via
  `fly machine list -a nova-video` first). Expect a brief drain burst: ~2 days of queued
  maintenance messages (~5-6k, mostly no-op polls) are sitting in Redis.

## [2026-08-05] Intro-hook transformation slop — ban the pattern class, lint the exemplars, scan don't warn (plans/015)

A montage plan-item render burned "the monkey changed my whole marketing
perspective" as its opening hook: intro_writer glued unrelated footage (a
monkey) to a persona pillar (marketing) with a fabricated transformation
claim. Root cause was three-way: the prompt's DON'T list banned slop as
LITERAL strings ("changed everything") that paraphrase trivially; the pillar
instruction outweighed the existing drop-the-theme escape clause (a subjective
"changed my perspective" isn't an invented fact/place/event, so the clause
never fired); and the exemplar library itself shipped the banned frame —
`transformation-before-after-karaoke-01` read "this is what changed
everything" (added PR #338, phrase banned later by PR #507; prompt/exemplar
drift with no guard).

**Decisions:**

- **Ban the pattern CLASS in the prompt, not more strings** — retrospective
  transformation / lesson-learned framing is named as a class with examples,
  plus a translate-don't-echo rule for plan ideas that arrive already
  slop-framed ("how X changed my life"). The existing pillar escape clause at
  write_intro_text.txt was deliberately left untouched: rewriting it was
  wording churn carrying over-correction risk (model drops persona when
  aligned); escalate only if live shadow A/B says the ban alone is
  insufficient.
- **One deterministic pattern source, three free consumers** —
  `slop_structural_failures()` in `app/agents/intro_writer.py` (same shape as
  sequence_quote's `quote_structural_failures`) feeds the eval structural
  floor, `tests/agents/test_overlay_examples_slop_guard.py` (every exemplar
  AND every recorded fixture output must pass — kills the #338/#507 drift
  class), and `scripts/dev/scan_intro_slop.py`. Turkish trap worth knowing:
  `str.casefold()` maps İ to `i` + U+0307 combining dot BETWEEN letters, so
  naive lowercase regexes silently miss uppercase Turkish — the normalizer
  strips U+0307 and a test pins it.
- **No runtime enforcement, no runtime warn** — a parse()-level rejection
  would downgrade good hooks to the generic fallback on any false positive
  (rejected at eng review), and a runtime warn was cut too: persisted
  `intro_text` is REUSED on re-render with no LLM call, so a runtime hook
  structurally cannot see legacy slop, ships with the fix (no baseline), and
  conflates deliberate false positives into a blind rate. The offline scanner
  (read-only) gives the pre-deploy baseline, the legacy remediation list, and
  the post-deploy delta instead.
- **Rubric had the incentive backwards** — persona_coherence scored 1 for
  "ignores the persona/theme", punishing the correct drop-the-theme behavior
  on conflict footage. Now: dropping an unhonorable theme scores 5 (conflict
  rule); lesson-glue lines score an automatic 1 on persona_coherence AND
  voice_match, with the monkey line as the calibrated example. Four
  adversarial fixtures (marketing/monkey, fitness/cooking, TR, slop-framed
  idea) pin the desired outputs.
- **Merge gate:** live judge shadow A/B on a keyed machine (repo
  prompt-change rule) — replay CI guards artifacts, not model behavior.

## [2026-08-14] Optional lyrics must not erase the content-plan intro

A Corfu content-plan item rendered a technically valid 3.2-second sailboat
video with no visible text. The intro writer had produced “pov: sailboat
mornings in korfu,” and the selected song had usable lyrics, but two individually
tested decisions interacted badly: content-plan primary montages chose the
first renderable music variant (`song_lyrics`), while optional-lyrics mode made
that variant start with lyrics off. Because a lyrics-mode variant does not burn
the agent intro, both possible text layers disappeared. The renderer correctly
produced an MP4; the product contract was wrong.

**Decisions:**

- **A generated intro is the default visible text.** With
  `LYRICS_OPTIONAL_ENABLED=true`, a track-backed content-plan montage selects
  `song_text` / `agent_text`, so the authored intro is persisted and burned.
- **Lyrics are a capability, not a competing variant.** A track-backed
  `song_text` variant with renderable lyrics is marked `lyrics_baked=false`; the existing
  lyric-seed endpoint and text-element editor can add lyrics later without
  replacing the intro. Its API capability follows `LYRICS_OPTIONAL_ENABLED`,
  not the separate legacy baked-lyrics editor flag.
- **The kill switch preserves the old path.** With optional lyrics disabled,
  the selector still takes the first renderable variant, so a lyric-capable
  track produces legacy `song_lyrics` with baked lyrics and the persisted dict
  shape remains unchanged.
- **Test interactions, not only branches.** The critical guard renders a
  lyric-capable `song_text` under optional-lyrics mode and requires both the
  Corfu-style intro persistence and the lyric-seed capability. Separate tests
  continue to pin the legacy flag-off behavior.
