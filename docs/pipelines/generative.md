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
- **Word-cluster intro layout** (v0.4.97.0): `overlay_format_matcher` may pick
  `layout: "cluster"` (calm/scenic content, 3-6 word hooks). `intro_writer` annotates
  `word_roles` (hero/connector/closer); the deterministic engine in
  `app/pipeline/intro_cluster.py` computes per-block geometry from Skia glyph
  measurement and `generative_overlays` emits one [fade-in reveal, static hold] pair
  per block — existing renderer fields only, no renderer change. Engine declines
  (unsuitable word count / unfittable words) → linear fallback, never a lost intro.
  Effective `intro_layout` + `intro_word_roles` persist on the variant; the instant
  text editor gates on `intro_layout == "cluster"` (server reburn instead of local
  preview). Kill switch: `GENERATIVE_CLUSTER_INTRO_ENABLED`.

### Intro placement snapshot (`intro_placement`)

`_resolve_intro_overlay_params` folds knobs > curated set > agent advisory into one
placement; `_intro_placement_from_params` snapshots the resolved
position/fracs/max_width/anchor/rotation onto the variant so the editor's read
adapter (`_base_text_elements_for_variant`) projects the element where the burn
actually put it — it used to re-guess and always land on "center", drawing a
`bottom` hook at mid-frame. `None` for the plain centered placement (the majority),
which keeps those variants on the legacy projection path byte-identically — EXCEPT
when the variant carries `text_placement_candidates`, where even a centered
resolution is persisted (`has_candidates=True`), deliberately moving it OFF the
legacy path so the adapter stops reading candidate fracs the resolver declined
(guard: `test_declined_candidates_do_not_leak_into_the_editor`). A no-LLM
re-render folds the persisted **non-center** position back in at the ADVISORY tier
(`_persisted_intro_position` → `_resolve_regen_text`), so a text edit can't silently
re-center it; folding "center" is deliberately suppressed because it would flip
`has_explicit_position` and drop masonry placement candidates. The key must stay in
`_finalize_job`'s allowlist and in sync with `_INTRO_PLACEMENT_ADAPTER_KEYS` —
both pinned by `tests/tasks/test_intro_placement_parity.py`.

Companion reader fix in the same release: `_burn_dict_position` now takes a
half-pinned overlay's missing y from the renderer's `_POSITION_Y` table instead
of a hardcoded `0.5` (`center` is 0.45, `bottom` is 0.85). It is shared with the
lyric-seed adapter, so every style set that pins x with a null y (`word_reveal`,
`typewriter`, `ai_answer`, `lyric_word_pop_punchy`) was saving the invented y
through `GET .../lyric-seeds`; guard
`test_seed_elements_half_pinned_style_set_uses_the_renderers_y` in
`tests/pipeline/test_lyric_seed_elements.py`. Zero-valued fracs are no longer
truthiness-coerced (`0.0` is reachable and meaningful).

See agents/DECISIONS.md (2026-07-30) for the reusable rules: the `_finalize_job`
allowlist trap, and why a burn-dict reader must mirror the renderer's fallbacks.

## Three variants

- `song_lyrics` — matched song + its lyrics
- `song_text` — matched song + AI hero-intro overlay
- `original_text` — clips' original audio + AI intro

## Landscape and Smart Captions rollout

Both features ship dark and reuse existing render paths; enabling them is an
operational rollout, not a code restoration.

**Landscape (dual flag):** set `LANDSCAPE_OUTPUT_ENABLED=true` on Fly and
restart the API/workers first. Verify the editor capability response and one
16:9 render while the UI remains hidden. Only then set
`NEXT_PUBLIC_LANDSCAPE_OUTPUT_ENABLED=true` in Vercel and deploy the web app.
Unsupported caption, talking-head, collage, and visual-block variants continue
to report a non-editable orientation capability. Roll back in reverse order:
Vercel off first, then Fly.

**Smart Captions (server-only flag):** set `SMART_CAPTIONS_ENABLED=true` on Fly
and restart the API/workers. There is deliberately no `NEXT_PUBLIC` twin. A
creator is eligible only when `SUBTITLED_ARCHETYPE_ENABLED=true`, the plan item
uses `edit_format="subtitled"`, and either an enabled
`CreatorStyleAssignment` pins a preset id/version or
`SMART_CAPTIONS_DEFAULT_PRESET_ID`/`SMART_CAPTIONS_DEFAULT_PRESET_VERSION`
configure a fleet-wide default. Verify an eligible creator gets
`smart_captions_available=true`, the job trace records
`smart_captions.plan_compiled`, and the ready variant persists
`smart_captions_applied=true`. Planner/compiler failures fail open to ordinary
corrected captions. Roll back by setting the Fly flag false; unset the default
preset secrets to remove open-to-all eligibility without deleting assignments.

**Smart Captions v2 (v0.11.0.0):** the semantic pipeline in `app/smart_edit/`
(strict schemas → transcript-anchored planner → preset-driven compiler) builds
the full Çiğdem-style composition; internals live in
`docs/pipelines/smart-captions.md`. V2 adds a licensed music bed (kill switch
`SMART_MUSIC_BED_ENABLED`, independent of `SOUND_EFFECTS_ENABLED`; admin
licensing toggle on the music Config tab) and a server-pinned shadow-preset
canary (migration 0066) that compiles and fingerprints a shadow plan without
changing output. Rollout stays gated by `SMART_CAPTIONS_ENABLED` exactly as
above; v2 failures fail open to a standard subtitled render with receipts.

## Key files

- `src/apps/api/app/tasks/generative_build.py` — `orchestrate_generative_job` Celery
  task
- `src/apps/api/app/pipeline/generative_overlays.py` — intro overlay builder
- `src/apps/web/src/app/generative/` + `admin/generative/` — public result UI + admin
  dashboard
- `src/apps/api/app/routes/generative_jobs.py` — job submission + status: `swap-song` /
  `retext` per-variant re-renders; `_variants_for_response` re-signs ready variant URLs
  from the persisted `video_path` key (`PLAYBACK_URL_TTL_MIN`). Reuses
  `admin_music._validate_clip_path_prefixes` for the clip allowlist. The status
  response also carries `retrying` (computed at read time — see "Worker memory
  guardrails + render liveness" below).
- `src/apps/api/app/routes/admin_generative.py` — `GET /admin/generative` dashboard
  list.
- `src/apps/api/app/services/job_phases.py` — phase/heartbeat writers plus the
  render-attempt clock helpers `mark_reattempt` / `stamp_variant_attempt` (see
  "Render-attempt clock" below).

## Worker memory guardrails + render liveness (v0.12.2.0)

Three mechanics from the 2026-07-21 worker OOM incident. Full narrative and
rejected alternatives: `agents/DECISIONS.md` "[2026-07-21] Worker OOM
mid-reframe". Env vars documented in `.env.example` (deliberately not
CLAUDE.md — its size budget was full).

- **Heavy-source downscale guard** — `downscale_oversized_sources` in
  `app/pipeline/source_guard.py`, called from `_ingest_clips`,
  `_prepare_timeline_assembly` (timeline re-renders decode the durable
  originals), and the narrated bed-level reburn. SDR sources with short edge
  > `SOURCE_DOWNSCALE_SHORT_EDGE_MAX` (1920) are re-encoded once at ingest
  (decode threads capped at `SOURCE_DOWNSCALE_FFMPEG_THREADS`, never
  upscaled, audio stream-copied with an AAC-transcode retry) so per-slot
  reframes never decode native 4K HEVC. HDR clips are excluded
  (`_pretonemap_hdr_clips` owns those), still images too. Whole-pass budget
  `_GUARD_TOTAL_BUDGET_S = 900`; overflow and failed conversions keep the
  original clip. Trace events on the `reframe` stage: `source_guard_downscaled`,
  `source_guard_downscale_failed`, `source_guard_budget_exhausted`. Kill
  switch: `SOURCE_DOWNSCALE_GUARD_ENABLED=false` + worker restart. Guards:
  `tests/pipeline/test_source_guard.py`. Template/music ingest paths are NOT
  wired yet (TODOS.md).
- **Prefork child recycling + tmpfs sweep** — `worker_max_memory_per_child`
  in `app/worker.py` (`WORKER_MAX_MEMORY_PER_CHILD_KB`, default 3GB,
  0 disables; billiard compares lifetime PEAK RSS, recycles between tasks
  only). A `task_prerun` hook sweeps orphaned `nova*` tmpdirs from RAM-backed
  /tmp (`app/pipeline/tmp_sweep.py`; the 1850s age cutoff must stay above
  every render `time_limit` and at or below the 1900s `visibility_timeout` —
  pinned by `test_tmp_sweep_cutoff_stays_inside_redelivery_window`).
- **`retrying` status flag** — `orchestrate_generative_job` wraps its body in
  `job_phases.job_heartbeat`, ticking `jobs.worker_heartbeat_at` (migration
  0068) every `RENDER_HEARTBEAT_INTERVAL_S` with the DB clock. The status
  route's `_compute_retrying` reports `retrying: true` at read time while a
  `processing`/`rendering` job's beacon is older than
  `RENDER_HEARTBEAT_STALE_AFTER_S` but still inside the redelivery window
  (visibility_timeout + stale + 300s slack — past it no retry can come and
  the claim stops). NULL beacon never flags. ProgressTheater and EditPayoff
  swap in recovery copy and hide the ETA while retrying. Variant re-render /
  reburn tasks do NOT heartbeat (accepted gap, TODOS.md). Guards:
  `tests/routes/test_generative_retrying.py`,
  `src/apps/web/src/__tests__/progress/retrying.test.tsx`.

## Render-attempt clock (v0.18.1.1)

A re-render reuses the SAME `Job` row, so nothing about the first render's
timestamps expires on its own. Full narrative, rejected alternatives, and the
accepted costs: `agents/DECISIONS.md` "[2026-07-30] Render-attempt clock".
Deliberately not in CLAUDE.md — its size budget is full.

**Invariant: every re-render dispatch restarts the user's clock.** A dispatcher
that marks a variant `rendering` must call BOTH helpers from
`app/services/job_phases.py`:

- `stamp_variant_attempt(variant)` — sets `render_status="rendering"` plus a
  fresh `render_started_at` (naive-UTC + literal `"Z"`, the frozen wire format
  for that JSONB field). Takes the DICT the caller persists, because the
  copy-loop dispatchers (`updated = dict(v)` … `variants[i] = updated`) would
  otherwise overwrite a write made to the original. Does NOT write
  `render_enqueued_at` — that field's only writer is `_mark_variant_rendering`,
  which owns the caption-reburn supersession token.
- `mark_reattempt(job)` — moves `job.started_at` to now (returns whether it
  moved). Takes the ORM object the route already holds and row-locks. Anchored
  at DISPATCH, not worker pickup: the Save press is the user's mental model and
  queue wait is real waiting. SKIPS the move while an orchestrator run is in
  flight (`current_phase` non-None) so editing a ready variant can't yank the
  origin out from under siblings still on their first render. Never touches
  `finished_at` — `plan_items.py` exports it as the item's ready date and no
  re-render task calls `mark_finished`.

Wired into all **16** dispatch paths across `routes/generative_jobs.py` and
`tasks/autoplace.py` (the autoplan visual-blocks render included; its rollback
path restores the previous `render_started_at` with the status). A persist-only
save (`render=False`) must NOT call either helper. `mark_started` is unchanged
and still refuses to move `started_at` — it models worker pickup of one
orchestrator run, so a Celery redelivery can't restart a clock mid-render.

**Frontend contract** — a re-render does NOT move `job.status` off
`variants_ready`, so "terminal status wins" is the wrong poll predicate:

- `isGenerativeJobSettled(status, variants)` (`src/apps/web/src/lib/generative-api.ts`)
  is the single definition of settled for the item page, public `/generative`,
  and the onboarding EditPayoff panel. `GENERATIVE_TERMINAL_STATUSES` is now
  composed from `GENERATIVE_SUCCESS_STATUSES` + `GENERATIVE_FAILED_STATUSES` so
  the two halves partition it and a new failure status can't go missing.
  Non-terminal ⇒ not settled; a FAILED terminal ⇒ settled whatever the variants
  say (a variant frozen in `rendering` after a failed job is a backend
  data-integrity gap and must never block the UI); a SUCCESS terminal ⇒ not
  settled while a variant is rendering inside the 30-minute
  `STUCK_RENDER_CEILING_MS`. `admin/generative/[id]` still uses the raw status
  check (TODOS.md).
- `deriveReceiptText(startedAt, finishedAt)` (`components/progress/logic.ts`) is
  the one receipt formatter for all three surfaces; a non-positive span falls
  back to `RECEIPT_FALLBACK` instead of rendering "Ready in -36:-12".
- `ProgressTheater` resets `bandCollapsed`/`showReceipt` when the job leaves the
  success-terminal state — an in-place edit never remounts it, and a collapsed
  band rendered the restarted clock invisibly.
- `usePolledJobStatus` re-arms its max-poll ceiling alongside the interval, and
  releases the ceiling's own ref when it fires. Both halves are load-bearing: a
  re-armed poll with no fresh ceiling, or a stale ceiling ref blocking every
  later re-arm, each leave a permanently non-terminal payload polling forever.
  The ceiling is per-MOUNT, not per-attempt: a re-arm only fires while the ref is
  null, so a Save made before the mount's ceiling expires inherits the remaining
  budget rather than getting a fresh 30 minutes.

**Guards:** `tests/routes/test_render_attempt_clock.py` — per-dispatcher clock
tests plus two structural (AST) guards:
`test_every_stamping_function_also_restarts_the_job_clock` (a function that
stamps must also reset) and
`test_no_module_marks_a_variant_rendering_outside_the_helper` (no raw
`render_status = "rendering"` in a dispatch module). AST rather than grep because
a grep guard matches one spelling of one line and missed `tasks/autoplace.py`
entirely. The autoplan path's clock + rollback behavior is pinned separately in
`tests/tasks/test_visual_blocks_autoplan.py`
(`test_autoplan_render_restarts_the_attempt_clock`,
`test_render_dispatch_failure_rolls_the_tile_clock_back`,
`test_render_dispatch_failure_drops_a_first_ever_tile_clock`). Frontend:
`src/apps/web/src/__tests__/progress/attempt-clock.test.tsx`,
`src/apps/web/src/__tests__/hooks/usePolledJobStatus.test.tsx`.

## Post-generation timeline editing (clip editor)

After a render, `song_text` / `original_text` montage variants are editable: reorder,
beat-quantized duration, in-point scrub, clip swap/add/remove, reset. The editor edits the
AI's assembly decisions, not pixels.

- **Fast text reburn safety:** only pure text edits may reuse
  `base_video_path`. Base-affecting commits (orientation, timeline, music,
  mix, lyrics, or camera changes) set the sticky `base_video_stale` marker;
  only the token-winning full render clears it. Before burning, the worker
  probes the cached base and requires an exact match with the requested
  portrait or landscape canvas. A stale marker, explicit orientation request,
  probe failure, or canvas mismatch falls back to full assembly instead of
  letting FFmpeg resize the old base. Reburn outputs and derived visual/motion
  caches use immutable generation-scoped keys; the `render_generation_id`
  guard publishes only the newest render and cleans rejected objects.
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
  **Swap-song exception:** a `new_track_id` clears the persisted `user_timeline` and
  forces a fresh match (the override is ignored) — a new track means a new beat grid, so
  the old cut can't line up. Matches the frontend ConfirmDialog copy ("your clip edits
  will be reset").
- **ai_timeline carry-forward:** an override render persists NO `ai_timeline` (the key is
  popped from the success patch) — the steps are the USER's cut, and "Reset to AI cut"
  must keep pointing at the real AI plan. Only fresh-match assemblies rewrite it.
- **Durable sources:** at orchestrate start, uploads are copied to
  `generative-jobs/{job_id}/sources/` (order-preserving rewrite of
  `all_candidates["clip_paths"]` — narrative order slices the first N keys, so order is
  load-bearing). This also keeps swap-song alive past the 24h upload lifecycle.
- **Endpoints:** GET/POST/DELETE `/generative-jobs/{id}/variants/{vid}/timeline`
  (mirrored on plan-items). Beat math walks the real non-uniform grid server-side.
  Internal cut boundaries remain beat-quantized. When the final active clip extends
  beyond the grid's last natural beat, its terminal endpoint stays at the exact
  user-requested second; the re-render sizes the song window to that timeline total.
- **Kill switch:** `GENERATIVE_TIMELINE_EDITOR_ENABLED=false` (Fly secret + restart) —
  GET returns `editable:false reason:"disabled"`, POST 403.
- **Guards:** window-parity test (`tests/pipeline/test_exact_window_steps.py`) pins that
  an unmodified override render reproduces the original assembly windows AND framing.
- **Editorial text projection:** `text_elements_for_variant()` projects every
  independently timed `generative_sequence` burn block into its own editor element
  with a stable scene/block id. Text, font face/style, placement, size, glow, shadow,
  effect, and fade-out timing survive editor load and save instead of being collapsed
  to one scene-level approximation.
- **Split & Place:** pasted composition copy is split line-first (with a linear
  fallback for prose), then timed sequentially across the remaining edit. The editor
  enforces the API's 50-element / 500-character limits before mutating the timeline,
  so rejected drafts never create empty or unsavable bars.

### Edit-copilot beat marks (creative direction)

The chat edit copilot sees and honors the music's beat grid (v0.11.4.0):

- **Snapshot contract:** `buildCopilotSnapshot` ships `beat_marks` — the beat grid
  projected into assembled-output seconds by `beatMarks()` in
  `src/apps/web/src/app/generative/timeline-math.ts` (grid variants only; removed
  and footage-trimmed slots contribute no marks). Capped by endpoint-preserving
  even sampling, never truncation: `COPILOT_BEAT_MARKS_MAX = 60`, re-sampled to 30
  when the snapshot exceeds its byte budget — first/last marks always survive so
  late-video beats stay addressable. Renderer-side mirror: `_BEAT_MARKS_SHOWN_MAX`
  in `app/agents/edit_copilot.py` (MUSIC BEAT MARKS prompt section; non-finite /
  overflow values filtered before rendering).
- **Client-side snapping:** beat fidelity is not prompt-only. `applyCopilotOps`
  snaps model-proposed text/SFX/overlay timings within `BEAT_SNAP_EPSILON_S =
  0.12` s onto the nearest mark (`src/apps/web/src/lib/edit-copilot/apply-ops.ts`);
  farther timings pass through as deliberate non-beat placements. A span whose
  edges would collapse onto one mark keeps its raw end, and snapped SFX re-clamp
  below `total_duration_s - 0.1` so the terminal beat mark can't place an
  inaudible accent at the video's last instant.
- **Stale-marks rule:** any clip-timeline mutation in a bundle disables snapping
  for the bundle's later ops — the timeline shift stales every snapshot mark.
  Prompt v6 (`EDIT_COPILOT_PROMPT_VERSION = 2026-07-21-v6`) forbids mixing
  beat-sync and clip re-cuts in one reply; the client enforces it. Creative
  bundles carry up to `_MAX_OPS = 12` ops.

### Video-length song windows

Beat-synced `song_text` and `song_lyrics` variants can move an exact-video-duration
window across their assigned track. The server-owned
`editor_capabilities.music_window` contract supplies the authoritative video and
track durations, recommended start, beat timestamps, editability reason, and whether
the stored timeline is linear enough to preserve. A missing capability hides the
control during frontend-first deploy skew.

The atomic editor commit accepts `music_window.start_s` plus one alignment choice:

- `preserve_cuts` freezes the current effective timeline (including clip changes in
  the same commit) to second-based durations, replaces its relative beat grid, and
  skips matching.
- `resync_beats` clears `user_timeline`; the render matches against the selected
  window and writes a fresh AI timeline and beat grid.

`_effective_music_window` in `generative_build.py` is the single render-time source
for recipe generation, lyric projection, preview offset, and final mixing. It snaps
the start to the nearest usable beat, keeps the end at exactly
`start + video_duration`, and marks validated windows so the legacy near-EOF audio
clamp cannot silently move them. Synthetic endpoints cover partial first/final beat
fragments; a sub-minimum final fragment merges into the preceding slot instead of
shortening output. The effective start persists only on the variant as
`music_start_s`; track swaps through legacy routes reset it to the new track's
recommended section.

Lyric windows are rematerialized after a move. Same-track user overrides survive
only when both their stable line key and original-text fingerprint still match;
out-of-window lines are removed and newly visible lines are added. A track change
always clears prior lyric overrides.

Failure handling keeps the editor recoverable: a removed or unavailable track
rejects the commit without discarding the local draft, an expired preview URL is
retried once without blocking Save, and a downstream render failure leaves the
committed song window in place for the existing retry flow.

## SFX + media-overlay lanes on caption archetypes (plan 010, v0.7.25.0)

Caption archetypes (`CAPTION_EDIT_ARCHETYPES = {"narrated", "subtitled"}`,
public in `routes/generative_jobs.py`) carry the Sounds and Overlays editor
lanes, same as montage variants — still behind `SOUND_EFFECTS_ENABLED` /
`MEDIA_OVERLAYS_ENABLED`. Two contracts make that safe:

- **Reapply-after-reburn:** every caption re-render path (caption Apply, caption
  position, narrated background-sound slider, subtitled re-transcribe) rebuilds
  `video_path` from the caption-free `base_video_path`, then
  `_reapply_user_media_layers` (`tasks/generative_build.py`) composites the
  persisted SFX/overlay lanes onto the fresh burn. Before plan 010 the lanes
  were disabled here precisely because these paths silently wiped composited
  effects. A no-op reapply still finalizes the terminal status, so a variant
  can never strand in "rendering".
- **Lane saves render through the caption reburn:** an SFX/overlay-only commit
  on a caption variant with a cached base enqueues `reburn_narrated_captions`
  on the `overlay-jobs` queue (solo worker — serializes the CLIP fork hazard)
  instead of the fast composite pass. The fast pass composites onto the
  CURRENT video, so a save racing an in-flight caption reburn could silently
  drop the caption edit. Legacy variants without a cached base fall through to
  the fast pass.

Supersession discipline: every caption dispatch mints a `render_generation_id`
and commits BEFORE enqueue (R1-1) — the reburn's start write is token-checked,
so an enqueue that outran the commit would read the old generation and strand
the variant. Superseded runs discard their terminal write and skip old-blob
deletes. Retired pre-effect snapshot blobs are freed by
`_free_media_snapshot_keys`, prefix-confined to `generative-jobs/*` (curated
`music/*` / `templates/*` are never deleted), and only after the accepted
terminal write. Caption tasks ride the standard render ceilings
(`soft_time_limit=1740`, `time_limit=1800`, under the 1900s broker
visibility_timeout).

Editor gating (`_editor_capabilities` in `routes/generative_jobs.py`, mirrored
by `src/apps/web/src/app/plan/items/[id]/_editor/editor-capabilities.ts`):

- AI overlay suggestions stay OFF on caption archetypes
  (`suggestions_reason = "caption_archetype"`) pending a speech-content
  quality eval (TODOS.md T-CAPFX-2).
- Text and mix are dual-gated (capability `false` + 422 on commit). Text goes
  through the shared `_text_elements_allowed` predicate, which folds in #625's
  `SUBTITLED_TEXT_LANE_ENABLED`. `CAPTION_TAB_COPY` is byte-stable —
  EditorShell string-compares it (`CAPTIONS_TAB_REASON`) to deep-link the
  Captions tab from disabled tools.

**Deploy skew (one-deploy window, accepted R3-B):** during the rolling
restart, a caption save from an upgraded API can hit an old worker →
TypeError on the new kwarg → the failure is ACKED
(`task_acks_on_failure_or_timeout` defaults True), NO redelivery self-heal.
The variant sits "rendering" until the 60-min reaper (`tasks/reaper.py`)
converts it to a failed badge; the user recovers by re-tapping Apply. See
agents/DECISIONS.md (2026-07-11) for the reusable rule.

## Speech map + SFX auto-suggestions (word-level sound design)

**Speech map.** `_variants_for_response` (routes/generative_jobs.py) attaches
`variant["speech_map"]` — `{source, words: [{w,s,e}], pauses: [{s,e,after}]}` in
final assembled-timeline seconds — derived on read from the variant's persisted
word source via `services/transcript_source.speech_words_for_variant`
(precedence: `transcript` → `overlay_transcript` → `caption_cues[].words`;
never Whisper on a read path) and shaped by
`services/speech_map.build_speech_map` (pause = inter-word
gap ≥ 0.28s, leading silence ≥ 0.5s; head-biased caps 150 words / 40 pauses;
out-of-range words dropped — coordinate invariant). Only rendered,
not-mid-re-render variants get the key. The editor forwards it into the copilot
snapshot as SPEECH WORDS / PAUSE MARKS (omitted while the local clip timeline
is dirty — the marks describe the persisted render), which is what makes
"add a click at the pauses in the first 4 seconds" and "place a sound effect on
the funny moment" answerable with verbatim word/pause times (prompt v6
speech-sync + moment-intelligence rules).

**SFX auto-suggestions** (`SFX_AUTOPLACE_ENABLED`, default false; dual-flag —
dispatch also requires `SOUND_EFFECTS_ENABLED`, since suggestions are realized
through the SFX write routes).
`_maybe_sfx_autoplace_after_finalize` (tasks/generative_build.py) dispatches
`autoplace_sfx_suggestions` (tasks/autoplace.py) per eligible variant (rendered,
once per generation via `sfx_autoplace_attempted`, speech-plausible). The task
feeds words/pauses/clip-moments + the published SFX glossary (role_tags;
`contains_voice=True` effects banned — legacy `NULL` passes, see TODOS) to the
`sfx_placement` agent (prompts/sfx_placement.txt, ONE
Gemini call); `services/sfx_autoplace.resolve_sfx_suggestions` validates (known
id, no voice, in-range, 1.5s spacing, cap 6) and persists ADVISORY
`pending_sfx_suggestions` stamped with `transcript_hash`. The read path
stale-filters against the current hash (clip re-cuts silently retire them), and
the copilot lists survivors as PENDING SFX SUGGESTIONS the model realizes as
ordinary `add_sfx` ops. Deliberately no autoapply twin — suggestions never
render without a human ask.

## Visual blocks

`visual_blocks` are first-class, per-variant base-layer replacements for rapid
montages and interstitial text cards. They are not media overlays: blocks are
composited onto the clean assembled base before authored text and captions.
The complete render order is clean base → visual blocks → authored text →
captions → media overlays → sound effects.

- Schemas and structural validation live in
  `app/agents/_schemas/visual_block.py`; blocks never overlap, montage shots
  persist concrete contiguous offsets, and card text links through
  `TextElement.visual_block_id`.
- `app/pipeline/visual_blocks.py` renders image/video shots, crop and Ken Burns
  motion, solid/gradient/blur/asset card backgrounds, transitions, and base
  audio mute windows. The text-free result is cached as
  `visual_blocks_base_path`, while `base_video_path` remains the durable clean
  source for block edits and removal.
- Editor saves include blocks and linked text in one `editor-commit` baseline.
  Auto pacing uses the non-persisting `retime-visual-block` endpoint and returns
  normalized shot boundaries; any direct timing edit switches to manual.
- `visual_treatment_planner` classifies transcript purpose and proposes
  transcript-backed cards or asset-backed montages under density guardrails.
  Extracted source frames become ordinary persistent `PlanItemAsset` rows with
  source clip/timestamp provenance before planning.
- Announced sections, rankings, steps, and numbered lists use the internal
  `section_item` purpose. The planner emits only the spoken ordinal and item
  title, then returns to the talking head for its definition or explanation.
  If the model misses the structure, a deterministic transcript fallback
  recovers only a complete, explicitly announced sequence; complete grounded
  model titles remain authoritative. Bare numbered hooks are never promoted.
  Card timing is deterministically aligned to the local contiguous transcript
  occurrence (including Turkish/English cardinal and ordinal forms), lasts at
  most four seconds, and requires at least 0.75 seconds of uncovered speaker
  footage before the next structured card. Long lists keep the first eight
  valid items. Generic card limits remain independent, and the 35% global
  treatment ceiling still applies.
- On subtitled variants, autoplanned card text always uses the text-then-caption
  compositor when visual blocks are active, even if the public subtitled text
  lane is disabled. The full editor previews persisted cues only over the
  caption-free base; an already-burned output is never captioned a second time.
- Planner input supports Nova's five-minute source ceiling. Whisper-derived
  `overlay_transcript` words persist even when the correct answer is zero
  cards. Source-revision checks prevent stale planning from overwriting newer
  renders or transcript corrections; feature-flag, preparation, planner, and
  queue failures release the run-once claim when retry is safe.

Rollout is triple-gated. `VISUAL_BLOCKS_ENABLED` gates API/render behavior,
`NEXT_PUBLIC_VISUAL_BLOCKS_ENABLED` gates the editor surface, and
`VISUAL_BLOCK_AUTOPLAN_ENABLED` separately gates first-edit AI planning. All
default false. Lyrics variants remain excluded until they have the same durable
clean-base contract.

## Local smoke test

```bash
make local-render MODE=generative CLIPS="a.mp4 b.mp4 c.mp4"
```
