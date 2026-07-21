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
