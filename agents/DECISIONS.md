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
