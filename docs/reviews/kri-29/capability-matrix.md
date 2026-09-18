# KRI-29 capability matrix

The complete `MediaCapability` vocabulary (Swift: `KriaMediaEngine.MediaCapability`,
`src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/Models.swift`; Python mirror:
`app.kria.recipes.MediaCapability`, `src/apps/api/app/kria/recipes.py`), classified against the
source catalogs in [coverage.md](coverage.md). Kept in sync by
`src/apps/api/tests/kria/test_capability_matrix.py`, which fails if either enum gains or loses a
case without a matching row here.

A case existing in the enum is **vocabulary**, never a claim of support. Three states:

- **local-v1** — a native primitive exists in `KriaMediaEngine`, the capability is already derived
  from recipe content (`EditRecipe.effectiveCapabilities` / the matching Python
  `required_capabilities` setter), and gating is purely `phone_render_verified_features` plus, for
  some lanes, an additional explicit device-parity block in `app/services/phone_rollout.py` (font
  instance qualification, giant-title handwriting performance). "local-v1" therefore does **not**
  mean enabled in production — see [coverage.md](coverage.md) for what's actually been measured.
- **local-later** — the capability is named, but the V2 recipe schema has no fields to carry the
  content that would require it. The underlying plan lane is rejected before a recipe can exist
  (`app/pipeline/phone_guided_plan.py`'s per-lane reject list, `_UNSUPPORTED_PHONE_LANE_CAPABILITY`)
  or before text compiles (`app/pipeline/portable_text_layout.py`). Landing this needs schema work
  (new recipe fields) plus a native painter/compositor before it can ever become local-v1.
- **cloud-fallback** — architecturally cloud-only for the foreseeable future: it depends on
  server-side LLM/agent output at render time (song selection, custom-effect chain authoring,
  slide-post layout) rather than on data the recipe could carry verbatim. Not currently named as a
  `MediaCapability` at all; listed here for completeness against coverage.md's catalog.

## local-v1 — native primitive exists, capability is derived from recipe content

| Capability | Source lane | Notes |
| --- | --- | --- |
| `basicComposition` | any clip | Always required once a timeline has clips. |
| `local1080Export` | any clip | Always required once a timeline has clips. |
| `positionedText` | `text_layers` | Any text layer. |
| `animatedText` | `text_layers[].effect`, `clips[].text` | Non-static/none text effect, or legacy `TextTreatment`. |
| `authoredText` | `text_layers[].runs[].font_variations`, `.animation_phases`, `.background`, `.effect == caption-pop`, `.karaoke.active_only` | Additionally gated by `_has_unqualified_font_instance` and the authored-phases/caption-pop/karaoke blocks in `phone_rollout.py` — these are deliberately **stricter** than the capability bit (see [Font/authored-text qualification is per-instance, not per-capability](#font-authored-text-qualification-is-per-instance-not-per-capability) below); do not remove them. |
| `crossfade` | `clips[].transition.kind == crossfade` | |
| `clipTransitions` | `clips[].transition.kind != crossfade` | 5 native transition paths per coverage.md. |
| `editorMedia` | `clips[].overlay_*`, `.hold_duration` | Additionally blocked in `phone_rollout.py` pending device parity — same stricter-than-capability pattern. |
| `goldenHourLook` | `clips[].look` | Native support requires an exact-canvas, unrotated source (`phone_guided_plan.py`); other looks/rotations still reject at plan-compile time regardless of this bit. |
| `alphaOverlay` | overlay track with clips | |
| `audioMix` | non-default `AudioMixRecipe`, audio track, or non-1 clip volume | Does not cover `duck_original_during_music = true`, which `CapabilityNegotiator.decide` rejects unconditionally before the capability check runs (ducking has no dedicated bit yet — see `audioDucking` below). |
| `variableSpeed` | `clips[].rate != 1` | |
| `visualBlocks` | `visual_fills`, `audio.mute_windows`, `clips[].visual_placement` | Additionally blocked in `phone_rollout.py` pending device parity. Narrower than the plan-level `editor_visual_blocks` lane (montage/text_card/media blocks with backgrounds) — see `visualBlocks` under local-later's plan-lane mapping. |
| `motionScenes` | `motion_scenes` | Additionally blocked in `phone_rollout.py` pending device parity. This is `KriaMediaEngine.MotionSceneProgram`, a *different* runtime from the cloud "motion presets" catalog (`src/packages/motion-runtime/creator-blocks.catalog.json`) — see `motionPresets` below. |
| `cameraEffects` | `camera_pulses` | `KriaMediaEngine.CameraPulse`, additionally blocked in `phone_rollout.py` pending device parity. Distinct from the cloud's semantic camera tokens (`semantic_crop_pulse`, `sine_pulse`) — see `semanticCamera` below. |
| `stillImages` | `asset_manifest.assets[].kind == visual` | Creator Visuals-pool photos drawn as fullscreen main-track stills (KRI-121). `compile_phone_guided_plan` only emits them for `kind == image`, `lane == asset`, `layout == fullscreen`, no `image_motion`, look `none` moments, and only while this bit is in `phone_render_verified_features`; the device downloads each photo through the per-asset grant for its pinned generation and verifies its SHA-256. Pool videos, supporting cards and image motion still reject. |
| `hevcDecode` | — | Declared since the V1 recipe shipped; nothing in either recipe schema derives it yet. Pre-existing gap, not introduced by this PR. |
| `hdr` | — | Same as `hevcDecode`. |

## local-later — vocabulary exists, recipe schema has no fields yet

| Capability | Plan lane / source catalog | What's missing |
| --- | --- | --- |
| `musicBed` | `GuidedStoryExecutionPlan.music`; `GenerativeVariantDecision.music_track_id` (montage/day_vlog/single_hero) | The licensed music bed. V6 guided plans dodge this by sending a timing-only `song_reference` (no audio) instead — see `docs/runbooks/phone-rendering.md` §"Matched songs as posting references". KRI-114 P1-2/P1-3's `app.pipeline.phone_montage_plan.compile_phone_montage_plan` is the first Python-side producer that actually derives this bit from real recipe content (a `PhoneMusicBed` → `LibraryRenderAsset` + an audio-kind `TimelineTrack` clip), gated by `_resolve_phone_music_bed`'s publish/ready checks. Still "local-later" here because no `KriaMediaEngine` native primitive plays a library music asset yet — local-v1 is pending device-side verification, not schema/Python work. |
| `narrationAudio` | `GuidedStoryExecutionPlan.narration` | Recorded voiceover track. |
| `soundEffects` | `.licensed_sfx_intent`, `.editor_sound_effects` | `SoundEffectPlacement` (library + user uploads, `at_s`, gain, trim, effect groups) — `app/agents/_schemas/sound_effect.py`. |
| `mediaCards` | `.editor_media_overlays` | `MediaOverlay` (pip/fullscreen, entrance/exit tokens, source crop, playback rate, z-order) — `app/agents/_schemas/media_overlay.py`. |
| `customEffects` | `.editor_custom_effects` | 20 FFmpeg effect tokens (`boxblur`…`zoompan`), `pipeline/custom_effects.py`; combinations of up to six plus timeline semantics required. |
| `motionPresets` | (no dedicated plan field; authored via the web motion-runtime editor) | 9 presets in `src/packages/motion-runtime/creator-blocks.catalog.json` (`card_stack`, `cloud_break`, `donut_text`, `evolving_type`, `film_strip`, `flow_field`, `kinetic_word`, `offer_swap`, `tag_stack`). Not the same runtime as native `motionScenes` above. |
| `carouselEffects` | (authored via the carousel editor, not the guided-story plan) | 4 effects in `pipeline/carousel/effects.py` (`cards_stack`, `cover_flow`, `flipbook`, `scale_sweep`). |
| `captions` | speech/caption lanes (`pipeline/captions.py`, `services/smart_captions.py`) | Analysis/planning split and a native caption compiler are both outstanding per coverage.md. |
| `slidePosts` | ordered image/video slide posts (`pipeline/slide_post/`) | Full creation/export contract outstanding. |
| `semanticCamera` | (authored via creative-direction agents, not the guided-story plan) | `semantic_crop_pulse`, `sine_pulse` — `pipeline/camera_effects.py`. Distinct from the native `cameraEffects` primitive above. |
| `audioDucking` | `GuidedStoryExecutionPlan` audio options / `EditRecipe.audio.duck_original_during_music` | The field already exists on the recipe, but `CapabilityNegotiator.decide` special-cases it to an unconditional cloud fallback before the capability check ever runs (`Capabilities.swift`). Named here so a future PR can route it through the normal capability gate instead of a hardcoded early return. |

## cloud-fallback — not capability-shaped; depends on server-side generation at render time

Not named in `MediaCapability` because there's no fixed content shape a recipe could carry — the
cloud has to *decide* something at render time, not just draw fixed geometry:

- Song/track auto-matching (`app.services.music_matcher`) — the recipe can carry the *result*
  (`song_reference`) but not re-derive it.
- Creative-direction/copy agents (hook writing, intro text authoring) that run before a recipe is
  compiled — by the time a recipe exists, their output is already baked into `text_elements`.
- Caption correction and transcript alignment (`CAPTION_PUNCTUATION_ENABLED`,
  `SUBTITLED_CAPTION_CORRECTION_ENABLED`) — analysis, not rendering.

## Font/authored-text qualification is per-instance, not per-capability

`authoredText`, `editorMedia`, `visualBlocks`, `motionScenes`, and `cameraEffects` are each blocked
by an *additional*, more specific check in `app/services/phone_rollout.py` even when their bit is
present in `phone_render_verified_features` — see `tests/services/test_phone_rollout.py`'s
`test_qualified_font_does_not_qualify_authored_phases`,
`test_authored_phases_stay_blocked_until_device_parity_is_verified`, and
`test_camera_program_cannot_bypass_device_qualification`, all of which explicitly verify the block
fires *with the capability already verified*. These are deliberate defense-in-depth, not redundant
duplication of the capability check — the capability enum's granularity is coarser than what's
actually been device-qualified (e.g. `cameraEffects` may be verified for one exact configuration
while `phone_rollout.py` still blocks the general `CameraPulse` primitive pending broader parity).
**Do not remove them** when a capability's bit gets added to `phone_render_verified_features`.

The font-instance check specifically (`_has_unqualified_font_instance`) has two modes, selected by
`Settings.phone_font_qualification_strict` (env `PHONE_FONT_QUALIFICATION_STRICT`, default
`false`). **Strict** (`true`) is the original 2026-09-14 gate: only the two exact, byte/coordinate-
pinned `Fraunces-Bold.ttf`/`DMSans-Bold.ttf` instances qualify, and only on a plain `fade-in` layer
with no giant title — see `docs/runbooks/phone-rendering.md`'s "Default variable-font fade-in
qualification" section. **Default** (`false`, since 2026-09-18) relaxes this to: any font asset that
is a bundled `library`/`catalog == "font"` asset listed in `assets/fonts/font-registry.json` (i.e.
whatever `bundled_font_asset` in `render_library.py` can serve — the same file the iOS app ships
with its license), with non-empty `font_variations` when-and-only-when that bundled font file itself
carries variation axes (a non-variable face like `Inter-Bold.ttf` has nothing for CoreText to pick
differently, so empty coordinates are fine for it), on any of the 17 native-supported text effects
(`app.services.phone_rollout._NATIVE_TEXT_EFFECTS`, derived from `PortableTextLayer.effect`'s
Literal minus `lyric-line`/`caption-pop` — see `docs/reviews/kri-29/coverage.md`'s "## Text"
section). Giant-title combinations are allowed in default mode too, since `giant-title-wipe` has
native support for every effect except handwriting; the unconditional
`layer.giant_title is not None and layer.effect == "handwriting"` reject a few lines below the font
check is untouched by either mode. This is the pilot's kill switch: flip `true` to restore the
narrow per-instance gate byte-identically if the broader font/effect set ever needs pulling back
without a redeploy. Tests: `tests/services/test_phone_rollout.py`'s
`test_default_mode_qualifies_static_title_and_context_labels` (reproduces prod job
`b33e1c88-a8eb-4d51-81b6-388f7a32aebf`'s static Fraunces title + Inter-Bold static context labels),
`test_default_mode_rejects_font_outside_the_bundled_registry`,
`test_default_mode_rejects_variable_font_missing_variations`,
`test_default_mode_allows_giant_title_on_non_handwriting_effects`, and
`test_default_mode_still_blocks_giant_title_handwriting`; the original narrow-gate tests are kept
verbatim with `phone_font_qualification_strict` monkeypatched `true` as defense-in-depth pins.
