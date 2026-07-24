# Smart Captions v2 — internals

Reference doc for deep pipeline internals. CLAUDE.md carries the env-flag contract;
`docs/pipelines/generative.md` carries the rollout runbook. This file carries the v2
mechanics (v0.11.0.0).

## Eligibility and capability

`app/services/smart_captions.py` is the single server-authoritative resolver: a
creator is eligible when `SMART_CAPTIONS_ENABLED=true`,
`SUBTITLED_ARCHETYPE_ENABLED=true`, the plan item uses `edit_format="subtitled"`,
and either an enabled `CreatorStyleAssignment` pins a `preset_id`/`preset_version`
or `SMART_CAPTIONS_DEFAULT_PRESET_ID`/`SMART_CAPTIONS_DEFAULT_PRESET_VERSION`
configure a fleet-wide default for users without an assignment row. The browser
persists intent but can never select a preset or bypass rollout gates. Assignment
rows win over the default: they may pin a different reviewed preset, explicitly
disable one account, or carry `shadow_preset_id`/`shadow_preset_version`
(migration 0066; check-constrained to be set or null as a pair) — see the shadow
canary section. Guard: `tests/smart_edit/test_capability.py`.

## Planner → compiler contract

- `app/smart_edit/schemas.py` — strict renderer-independent contracts
  (`SMART_EDIT_SCHEMA_VERSION_V2 = "2026-07-20"`, `extra="forbid"`). The planner
  may choose semantic roles, word-id spans, and closed tokens ONLY; milliseconds,
  coordinates, storage paths, fonts, and colors are resolved later by
  deterministic preset code. Caps: 120 events, 600 words, 300 baseline cues.
- `app/smart_edit/planner.py` (`PLANNER_VERSION_V2`; legacy v1 path keeps
  `PLANNER_VERSION`) — transcript-anchored semantic planner. Event lanes: text,
  visual, camera, sfx, caption-emphasis, boundary-effect, audio-treatment; every
  event anchors to word ids (`w000123`).
- `app/smart_edit/compiler.py` (`COMPILER_VERSION_V2`) — compiles a validated
  plan into existing renderer lanes: caption cues + policy, text elements
  (numbered chapter titles render via Skia independently of subtitles), media
  overlays, sfx intents, boundary effects; v2 adds `camera_intents` and
  `audio_treatment_intents` to the compiled patch. Compilation is deterministic:
  same document + preset ⇒ identical patch (fingerprinted for shadow comparison).
- Camera lane: semantic crop pulses compile into the EXISTING reframe filter
  chain (`reframe.py`) — no extra encode. Guard:
  `test_semantic_crop_is_inside_existing_reframe_filter_only`.
- Typewriter title reveals and their keyboard-tick SFX share one schedule
  (`text_overlay_skia.py` reveal + tick placements). Guard:
  `test_v2_typewriter_visual_and_keyboard_ticks_share_schedule`.

## Presets

`app/smart_edit/presets.py` loads `app/smart_edit/presets/<id>/<version>.json`
(e.g. `cigdem/v2.json`). The planner emits closed tokens; the preset is the ONLY
place tokens become typography, geometry, density, transition, and audio policy.
Preset JSON is strict pydantic, repo-reviewed, and covered by golden tests. Key
blocks: `caption` (word ranges, font, `y_frac`), `scene_layouts`
(`hook_accumulation`, `single_example`, `example_pair`, `fullscreen_cutaway`,
`persistent_badge`), `density` (hook window / max visuals /
`hook_caption_suppress_min_visuals`), `text_styles`, `visual_aliases`,
`sfx_roles`, `camera`, `audio_treatment`, `boundary_effects`.

## Hook accumulation and caption suppression (deferred)

Hook-window visuals accumulate on screen under `scene_layouts.hook_accumulation`.
When the layout says `suppress_if_resolved` and enough hook visuals resolved, the
compiler sets `hook_caption_suppression_eligible` — but `hook_caption_suppressed`
always stays false. Asset resolution at compile time is planning evidence only;
downloads, normalization, collision arbitration, and FFmpeg can still fail later,
so suppressing speech there could ship a hook with neither captions nor visuals.
Suppression waits for a transactional compositor keyed on the applied-media
manifest. See agents/DECISIONS.md (2026-07-20).

## Shared render geometry

`app/pipeline/render_geometry.py` is one deterministic geometry module shared by
the compiler, ASS caption writer, Skia title renderer, and media compositor:
normalized boxes, renderer-font text measurement, bounded face sampling, alpha-
aware footprints, and the conservative reposition → shrink → omit policy for
decorative media. Simultaneous pip cards get collision-free, aspect-correct
layouts from real asset footprints. Face sampling runs in
`face_sampler_worker.py` — a killable OpenCV subprocess under a hard timeout;
failure or timeout ⇒ empty regions, never a hung render. Guards: the
`test_shared_geometry_*` and `test_face_*` tests in
`tests/smart_edit/test_v2_render_contract.py`.

`measure_caption` also takes soft, fit-first line-layout preferences (plan 011,
Feature B): `keep_together` word-index pairs that must not split across the two
lines, and `penalize_widows` to discourage a lone short word (≤3 chars) on its
own line. Overflow dominates both penalties, so a split that fits the width
always wins over one that overflows — honoring a preference never forces an extra
shrink. A bare `measure_caption` with neither supplied is byte-identical to the
pre-feature scoring. Guard: `tests/pipeline/test_render_geometry.py`.

## Caption grammar

`app/smart_edit/captions.py` + `prepare_smart_caption_cues` in
`app/pipeline/captions.py`: readable word-timed cues under the preset `caption`
policy (measured two-line wrap, reburn-stable). Explicit user font/position edits
survive Smart reburns; without edits the pinned preset policy stays
authoritative. Caption-language changes on Smart variants are declined until a
safe re-plan exists.

### Contextual caption cues (plans 011 + 012)

`build_semantic_caption_cues` groups words by MEANING instead of a fixed
words-per-cue when the emphasis brain is on. The scene matcher emits
`emphasis_spans` (`standalone` | `keep_together`), validated + budget-capped +
gap-spaced in `planner._validate_emphasis_spans` (≤10 per video; standalone pops
kept ≥1.5s apart so a spoken list does not strobe). A `standalone` span
("number one … Messi") closes its own cue so the named entity shows alone —
marked `smart_emphasis` and held to the `_STANDALONE_MIN_HOLD_S` (0.5s) display
floor without overrunning the next cue or touching per-word `end_s`. A
`keep_together` span never splits across cues or lines; multi-word spans of
either kind surface to `measure_caption` as `smart_keep_together` line pairs. A
semantic close (role change or authored-title boundary) ALWAYS wins over both —
presentation may never own meaning.

Reliability floors (plan 012, P0): an isolated cue that is purely a
scene-matcher-confirmed named entity is promoted to `smart_emphasis` even when
the LLM missed the standalone span (`_is_lone_name_cue`, keyed on the matcher's
entity anchors); and a stranded lone list marker ("number", "and", "the") folds
back into a neighboring cue instead of blinking as a one-word caption — never
folding into or across a standalone/floor-name/boundary, and never stripping the
emphasis off the name it sits beside. The section-heading keyword picker
(`compiler._KEYWORD_STOP`) skips the same list markers so "number one Lionel
Messi" surfaces "Lionel", not "number". The list-marker vocabulary is
hand-mirrored across `captions._LONE_MARKER_TOKENS`, `captions._NAME_STOP`, and
`compiler._KEYWORD_STOP` — keep them in sync.

Line layout (`prepare_smart_caption_cues`): persisted emphasis pairs
(`smart_keep_together`) are always honored; the deterministic digit+word rule
(`digit_word_keep_together`, "1 Messi") and the widow penalty apply only under
`SMART_CAPTION_LAYOUT_BALANCE_ENABLED`. All of the above is inert when no
emphasis span / entity anchor is present, so a flag-off render is byte-identical
(`SMART_CAPTION_EMPHASIS_CUES_ENABLED` / `SMART_CAPTION_LAYOUT_BALANCE_ENABLED` /
`SMART_CAPTION_SECTION_HEADING_ENABLED`, all documented in `.env.example`).
Guards: `tests/smart_edit/test_captions.py`,
`tests/smart_edit/test_scene_matcher.py`,
`tests/pipeline/test_smart_caption_prepare.py`,
`tests/pipeline/test_render_geometry.py`.

### Face-aware caption placement (plan 011, Feature C)

`choose_caption_y_frac` (`app/pipeline/render_geometry.py`) picks **one static
caption `y_frac` per video** so the band never sits on the speaker's face, on the
first render only. Orchestrated by `_apply_face_aware_caption_placement`
(`app/tasks/generative_build.py`) POST-reframe — faces can only be located on
final geometry — and BEFORE the caption burn, which is the ordering surgery this
feature required.

- **Anchors:** the union of 8 evenly-spaced times on the RENDERED base duration
  (never the original clip — a silence-cut base is shorter and seeking past its
  EOF yields undecodable frames) ∪ camera-intent times ∪ media-overlay starts,
  deduped within ±0.25s. Each lane has its OWN budget
  (`_FACE_PLACEMENT_MAX_INTENT_ANCHORS = 12`, matching the sampler's historical
  `max_samples` default so card face-protection keeps parity, plus 8 evenly-spaced
  that always survive) so neither can starve the other and a saturated plan
  (`MAX_SMART_EDIT_EVENTS = 120`) cannot inflate the sampler's frame-seek count or
  timeout. Samples are taken ONCE and reused for card arbitration.
- **Dominant face band:** the union of padded face boxes, only when faces appear on
  ≥60% of anchors that produced a **decodable** frame (the sampler reports
  `decoded` under `count_decoded=True`; the denominator is decodable, not
  attempted, so silence-cut bases stay honest). <3 usable anchors ⇒ preset.
- **Overlap gate is COVERAGE-FRACTION, not IoU** (`NormalizedBox.coverage_by`):
  `intersection ÷ caption-box area ≤ 5%`. IoU's denominator grows with the face
  band, which would inflate tolerance and certify a caption "clear" while it sits
  on the face. Candidate #0 is always the preset, so a well-framed video changes
  nothing. If no candidate is both clear and chrome-safe, the least-covered
  **chrome-safe** candidate wins (`status: best_effort`) — chrome-safety ranks
  ahead of coverage, because a caption under the platform UI is worse than one
  overlapping a face.
- **Probe set = EVERY distinct cue box** (`_distinct_caption_probe_boxes`), not
  just the tallest. Because the gate divides by the probe's own area, no single
  cue is the universal worst case: against a band near the caption's bottom edge
  a short one-line cue reports far more coverage than a tall two-line one (same
  intersection, smaller denominator), and the true maximum can fall at an
  intermediate height. A candidate must clear the gate for ALL shapes. Cheap —
  `max_lines` is clamped to 1-2. Boxes are measured once and translated
  arithmetically per candidate (wrap/shrink is y-independent).
- **Persistence:** the chosen y is written to `smart_caption_policy["y_frac"]` and
  mirrored to `caption_margin_v` (so the position UI tracks it) but deliberately
  does NOT set `caption_position_user_edited` — a non-null `caption_margin_v` no
  longer implies the creator pinned it. Precedence:
  `caption_position_user_edited` > face-chosen > preset. Reburns and
  re-transcribes read the persisted policy and never recompute.
- **Fail-open, never raises.** The receipt (`smart_validation_receipts
  .caption_placement`) carries a `reason` enum — `no_face | sampler_timeout |
  sampler_error | insufficient_anchors` — plus the embedded raw sampler receipt,
  so a broken cv2 worker image is distinguishable from well-framed clips in
  `/admin/jobs`. `base` is mutated only after every fallible step succeeds, so a
  mid-flight error truly leaves the preset geometry intact.
- **Kill switch:** `SMART_CAPTION_FACE_PLACEMENT_ENABLED=false` (default) ⇒
  geometry, the sampler's anchor-list argument, and receipts byte-identical.
  Render-only; no `NEXT_PUBLIC` twin. Guards:
  `tests/pipeline/test_render_geometry.py` (chooser matrix) +
  `tests/smart_edit/test_v2_render_contract.py` (flag gate, anchor union, cap).

### Transcript determinism

`transcribe_whisper_cached` in `app/pipeline/transcribe.py` (plan 012 P1-4):
whisper-1 is non-deterministic, so re-rendering one clip could drop or split a
proper noun differently each run and change the captions. The transcript is now
content-addressed — cached in GCS under
`transcript-cache/<version>/<sha256>_<lang>_<prompt>.json` keyed by the clip's
content hash — so every re-render of the same clip reuses the identical word
list. Fully fail-open: any hashing / storage error falls straight through to a
live transcribe, and a cache-write failure never affects the returned result.
The sole caller is `_render_subtitled_variant`. Gated by
`SMART_CAPTION_TRANSCRIPT_CACHE_ENABLED` (default on). Guard:
`tests/pipeline/test_transcript_cache.py`.

## Grounded caption correction

`build_trusted_caption_hints` in `app/pipeline/caption_correct.py`: the model may
only propose (timed-word span → `target_alias`) pairs where the alias comes from
the preset `visual_aliases` table AND the alias group is grounded by a phonetic
filename match in the creator's uploaded pool (`_safe_display_name` blocks paths
and prompt payloads). A grounded group admits ALL of its curated
transcript_terms — the table exists to map phonetically dissimilar pairs
("Çelik" ↔ "robots_wedding"), so alias≈anchor similarity is deliberately NOT
required. Word IDs and timestamps never change; untrusted rewrites are rejected.
Non-Smart caption correction keeps its original behavior.

## Licensed music bed

- **Selection:** `_resolve_smart_music_treatment` in `tasks/generative_build.py`
  — deterministic, under a short Redis lock with a 24h cache key
  (`smart-captions:music-treatment:{job_id}:{variant_id}`). Eligibility is a
  closed allowlist (`_smart_music_track_eligible`): ready + published +
  non-archived + `track_config.smart_captions_licensed == true` + analysis
  complete (curated `music/` audio path, current-version `ai_labels` and
  `best_sections`, resolvable best section). The admin toggle lives on the
  `/admin/music/[id]` Config tab (`TrackConfig.smart_captions_licensed` in
  `src/lib/music-api.ts`).
- **Mix:** `MusicBedTreatment` in `app/pipeline/sound_effects.py` — looped bed,
  sidechain-ducked under speech (`speech_duck_db`, default −12 dB), loudnorm to
  `final_lufs` (default −14), mixed with reveal SFX in ONE voice-safe graph that
  stream-copies the finished video (no re-encode). Retry ladder: full mix →
  SFX-only → untouched speech.
- **Persistence:** `smart_music_treatment` + `smart_audio_receipt` on the
  variant; reburns reuse the persisted treatment instead of re-matching.
- **Kill switch:** `SMART_MUSIC_BED_ENABLED=false` — independent of
  `SOUND_EFFECTS_ENABLED` (the SFX lane is user-authored; the bed is
  agent-selected). Off: new renders resolve no treatment, reburns skip re-mixing
  the bed, persisted treatments are preserved for re-enable, never deleted.
- Guards: `test_music_eligibility_is_closed_and_requires_explicit_license`,
  `test_music_and_sfx_share_one_voice_safe_stream_copy_graph`,
  `test_audio_treatment_retries_full_then_sfx_only`.

## Shadow preset canary

`CreatorStyleAssignment.shadow_preset_id`/`shadow_preset_version` (migration
0066). When set and the primary compile produced canonical cues, the worker
compiles the SHADOW plan from the same immutable asset snapshot and persists
`smart_shadow_comparison` — document + patch fingerprints plus aggregate lane
counts (captions, titles, visuals, sfx, camera, audio intents) — WITHOUT
materializing any shadow visual, text, camera, or audio decision. Output stays
byte-identical to a no-shadow render; the receipts are the promotion evidence
for a new preset version. CI pins migration reversibility with an Alembic
upgrade → downgrade → upgrade roundtrip (`.github/workflows/ci.yml`).

## Fail-open receipts and media persistence

Stale preset assignments and creator-pool outages fall back to a standard
subtitled render with `smart_validation_receipts` explaining why — a Smart
failure never fails the job. Media-overlay persistence (review D4): survivors of
the apply pass carry arbitration-resolved geometry; download-failed cards keep
their ORIGINAL payload so the next reburn retries them; arbitration-omitted
cards are dropped (the reburn path has no arbitration — persisting them would
resurrect the occlusion the omission prevented). `media_overlays_applied_ids`
separately records the subset that actually reached the burned video. Guard:
`test_media_overlay_persistence_keeps_failed_cards_drops_omitted`.

## Guards (entry points)

- `tests/smart_edit/test_v2_render_contract.py` — caption policy, shared
  geometry, typewriter tick schedule, one-graph audio mix, licensing closure,
  face-sampler budget, semantic-crop-in-reframe.
- `tests/smart_edit/test_reference_parity.py` — parity vs the Çiğdem reference
  edit.
- `tests/smart_edit/test_planner_compiler.py` — planner/compiler contract incl.
  hook-suppression eligibility.
- `tests/smart_edit/test_capability.py` — availability resolver incl. the
  shadow pair.
- `tests/evals/test_caption_correction_evals.py` +
  `tests/evals/rubrics/caption_correction.md` — grounded-correction quality.
