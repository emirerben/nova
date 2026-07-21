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

## Caption grammar

`app/smart_edit/captions.py` + `prepare_smart_caption_cues` in
`app/pipeline/captions.py`: readable word-timed cues under the preset `caption`
policy (measured two-line wrap, reburn-stable). Explicit user font/position edits
survive Smart reburns; without edits the pinned preset policy stays
authoritative. Caption-language changes on Smart variants are declined until a
safe re-plan exists.

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
