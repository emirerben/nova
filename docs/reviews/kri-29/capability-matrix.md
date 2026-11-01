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
| `audioMix` | non-default `AudioMixRecipe`, audio track, or non-1 clip volume | Covers per-clip `audio_fade_in`/`audio_fade_out` (KRI-139: music-bed fades, the 0.5 s voice fade-out) and `target_lufs` (the exporter measures the mix with BS.1770 and applies one gain plus a -1.5 dBTP limiter, approximating the cloud's `loudnorm`). `duck_original_during_music` additionally requires `audioDucking`. |
| `variableSpeed` | `clips[].rate != 1` | |
| `visualBlocks` | `visual_fills`, `audio.mute_windows`, `clips[].visual_placement` | Additionally blocked in `phone_rollout.py` pending device parity. Narrower than the plan-level `editor_visual_blocks` lane (montage/text_card/media blocks with backgrounds) — see `visualBlocks` under local-later's plan-lane mapping. |
| `motionScenes` | `motion_scenes` | Additionally blocked in `phone_rollout.py` pending device parity. This is `KriaMediaEngine.MotionSceneProgram`, a *different* runtime from the cloud "motion presets" catalog (`src/packages/motion-runtime/creator-blocks.catalog.json`) — see `motionPresets` below. |
| `cameraEffects` | `camera_pulses` | `KriaMediaEngine.CameraPulse`, additionally blocked in `phone_rollout.py` pending device parity. Distinct from the cloud's semantic camera tokens (`semantic_crop_pulse`, `sine_pulse`) — see `semanticCamera` below. |
| `stillImages` | `asset_manifest.assets[].kind == visual` (no `media_kind`, or `image`) | Creator Visuals-pool photos drawn as main-track stills (KRI-121). `compile_phone_guided_plan` only emits them for `kind == image`, `lane == asset` moments with no `image_motion`, look or speed change (a fullscreen photo may carry a `sourceCrop`), as `fullscreen` or as `supporting_card` (`clips[].still_layout`, which needs no capability of its own), and only while this bit is in `phone_render_verified_features`. The device downloads each photo through the per-asset grant for its pinned generation, verifies its SHA-256 and mattes transparency over black, as the cloud normalizer does. Image motion and looks on photos still reject. |
| `visualVideos` | `asset_manifest.assets[].kind == visual` with `media_kind == video` | Creator Visuals-pool videos drawn as fullscreen main-track clips (KRI-121). The worker hashes and probes each one at its pinned generation; the grant also checks the row's kind; the device downloads it like a photo, verifies its SHA-256 and hard-links it under a playable extension. Same treatment limits as bound footage (fullscreen, 1x; a crop rides on the clip via `sourceCrop`, KRI-140; `golden_hour` only on exact-canvas unrotated sources). Gated separately from `stillImages` so photos and videos can be verified on device independently. |
| `sourceCrop` | `tracks[].clips[].source_crop` | A normalized top-left-origin rectangle the device crops out of the source before cover-fitting it to the canvas (KRI-140) — the same re-frame the cloud applies via `reframe.source_crop`. `compile_phone_guided_plan` emits it for bound footage, Visuals-pool videos and fullscreen Visuals photos; a `supporting_card` photo (drawn whole) and any clip with `visual_placement` still reject. Carried by `TimelineClip.source_crop`; derived from content by `EditRecipe.effectiveCapabilities`, and required in `phone_render_verified_features` (`validate_phone_pilot_recipe` refuses a cropped clip otherwise). KRI-44: the rectangle is in the UPRIGHT frame (the engine maps it through the inverse `preferredTransform`), and Voiceover edits and authored timelines can carry it too. |
| `freehandDrawing` | `drawing_layers[]` (V2 recipe) | Creator finger drawings (KRI-589): pen/marker/highlighter/neon/chalk/spray strokes painted by `NativeDrawingPainter` over the finished frame, with an optional draw-on reveal and fade. Compiled from the editor `drawings` lane on EVERY recipe build or patch (`compile_drawing_layers`); the phone compiles the same layers for its live preview, and `tests/fixtures/drawing-parity/compile_v1.json` pins both compilers. Required in `phone_render_verified_features` (`validate_phone_pilot_recipe` refuses a recipe with drawing layers otherwise) and gated by `freehand_drawing_min_client_protocol`. |
| `textBend` | `text_layers[].bend` (V2 recipe) | Curved/rainbow text (KRI-646): `bend` in [-100, 100] (+100 = the widest line bows up over a 180-degree arc, negative = smile) drawn ON DEVICE by the native text painter; the server only stores and passes it through (`compile_text_overlay` stamps it when `_bend_supported`; ink-reveal, handwriting, typewriter/stream-in, smooth-type, staggered-slice, dissolve-out, karaoke/lyric-line, typewriter animation phases and giant titles stay straight and a layer carrying `bend` with any of them is rejected). Derived from the layers (`EditRecipeV2` adds it iff any text layer has `bend`), required in `phone_render_verified_features` (`validate_phone_pilot_recipe` refuses otherwise) and gated by `text_bend_min_client_protocol` (8); older builds get 426 `client_update_required` (feature `text_bend`) instead of a recipe they cannot decode. Kill switch `TEXT_BEND_ENABLED`. |
| `singleWordText` | `text_layers[].karaoke.single_word` (V2 recipe) | Caption `Display: Word` as ONE WORD AT A TIME from one `karaoke-line` layer per cue: the runs are standalone-centred words (absolute canvas pixels), `starts[i]` is when word `i` appears, only the active word is drawn, in `highlight`. Derived from the layers (`EditRecipeV2` adds it iff any layer has `karaoke.single_word`; the mode is also `active_only`, so `authoredText` is required too), required in `phone_render_verified_features` (`validate_phone_pilot_recipe` refuses otherwise) and gated by `single_word_text_min_client_protocol` (11); older builds get 426 `client_update_required` (feature `single_word_text`). The server emits it only when BOTH `singleWordText` and `authoredText` are verified and the compile knows the build declared protocol >= 11; otherwise captions stay the full-line karaoke sweep. Wire contract: `docs/runbooks/phone-rendering.md` "Single-word karaoke". |
| `behindSubject` | `text_layers[].behind_subject` (V2 recipe) | Text behind people on the phone: the compositor multiplies the flagged layer's alpha by (1 - person mask) per frame, the mask coming from Vision `VNGeneratePersonSegmentationRequest` on the composited canvas frame (`KriaMediaEngine.NativeSubjectMatte`: 15 Hz ticks with hold, trailing 3-frame median, 0.40 hard cut, tiny-fragment drop, feather; coverage < 1 % / > 85 %, strobing or a segmenter error paint the layer plain for that tick, never fail the export). Derived from the layers (`EditRecipeV2` adds it iff any text layer has `behind_subject`), required in `phone_render_verified_features` (`validate_phone_pilot_recipe` refuses otherwise) and stamped by `compile_text_overlay` only when `text_behind_subject_phone_supported` (kill switch `TEXT_BEHIND_SUBJECT_PHONE_ENABLED`, default off; verified token; build >= `text_behind_subject_phone_min_client_protocol` 21 inside an app request) — otherwise the flag is STRIPPED and the layer is plain text (trace `text_behind_subject_phone_fallback`). A pinned recipe carrying it answers 426 `client_update_required` (feature `text_behind_subject`) to an older build. Refused with a giant title. Failure modes: `docs/reviews/text-behind-subject-phone-failure-modes.md`. |
| `freezeFrame` | `tracks[kind=video].clips[].hold_duration` > 1 frame (V2 recipe) | A creator's freeze frame (KRI-828): the main-track clip plays its source window at rate 1, then holds its LAST frame for `hold_duration` (the engine's 1-frame tail scaled to the hold); the clip's own sound is silent under the hold. Derived from the clips (`EditRecipeV2` adds it iff a main video clip holds longer than one frame; a <= 1-frame hold is composition rounding and needs nothing), required in `phone_render_verified_features` (`validate_phone_pilot_recipe` refuses otherwise) and planned only when `freeze_frame_planned_for(protocol)` (verified + a STAMPED build >= `freeze_frame_min_client_protocol` 16; KRI-898). The stamp is never read from request context: the draft uses the build stamped on the message (`client_protocol`), the render uses the Job's `creator_client_protocol` (the drafting build, lowered by an older approving build; a headerless web approval keeps the drafting build). Unstamped = no hold, honest "Couldn't". Builds that do not know the case fail to decode the recipe rather than drop the hold. |
| `speakerBackground` | `speaker_background` (V2 recipe) | Talk-to-camera green screen (KRI-1115): one photo or looping muted video behind the speaker for the whole edit (`asset_id` names a `VisualRenderAsset`, `media_kind` image or video, `source_start` for video, `loop`, `subject_track_id` the speaker clip's track, default `subtitled`). The phone cuts the speaker out of the speaker clip alone (after its crop/zoom, before cutaways, cards and overlays) with a soft Vision person mask and composites it over the background; the server only names the asset. Derived from the recipe (`EditRecipeV2` adds it iff `speaker_background` is set, plus `stillImages`/`visualVideos` from the asset), required in `phone_render_verified_features` (`validate_phone_pilot_recipe` refuses otherwise) and gated by `speaker_background_supported` (kill switch `SPEAKER_BACKGROUND_ENABLED`, default off; verified token; build >= `speaker_background_min_client_protocol` 27 inside an app request). The key is omitted when unused, so every other recipe keeps its digest. The phone Talking compile strips the lane while the gate is closed and the device-render poll and editor Save answer 426 below protocol 27 (server lane: `docs/pipelines/speaker-background.md`); until a build renders it, `CapabilityNegotiator` routes a recipe carrying it away from the local renderer. Failure modes: `docs/reviews/speaker-background-failure-modes.md`. |
| `clipTilt` | `tracks[].clips[].transform.rotation_degrees != 0`, `tracks[].clips[].backdrop` (V1/V2 recipe) | A clip turned to any angle (KRI-1134): `rotation_degrees` is CLOCKWISE on screen, and `backdrop` (`{kind: blank \| color \| blur, color: #RRGGBB}`, main video track only) fills the corners the turn uncovers. The compositor draws a colour backdrop with `CIImage(color:)` and a blur backdrop from a quarter-size cover-fit copy of the same frame. A supporting-card photo turns inside its card and keeps its blurred cover upright. Derived from the clips by `EditRecipeV1` and `EditRecipe.effectiveCapabilities` (a `blank` backdrop counts), required in `phone_render_verified_features` (`validate_phone_pilot_recipe` refuses otherwise) and gated by `media_tilt_min_client_protocol` (28); an older build polling a pinned tilted recipe gets 426 `client_update_required` (feature `clip_tilt`). Kill switch `MEDIA_TILT_ENABLED` (default off). A `look` clip still refuses a non-identity transform (`Composition-135`). Wire parity: `tests/fixtures/clip-tilt-parity/v1.json`. |
| `hevcDecode` | — | Declared since the V1 recipe shipped; nothing in either recipe schema derives it yet. Pre-existing gap, not introduced by this PR. |
| `hdr` | — | Same as `hevcDecode`. |
| `musicBed` | `GenerativeVariantDecision.music_track_id` (the montage-family `voiceover_music` variant); `GuidedStoryExecutionPlan.music` | The native primitive exists: an audio-kind `TimelineTrack` clip plays the library asset at its matched offset (`Composition.swift` `addAudio`), fading in and out over its `audio_fade_in`/`audio_fade_out` (KRI-139, 0.5 s each). The device resolver names the downloaded file so AVFoundation can open it (KRI-132). Derived by the compiler (`compile_phone_voiceover_montage_plan`), not by `EditRecipe.effectiveCapabilities`; the legacy `AudioMixRecipe.music_asset_id` whole-file path stays unused by phone compilers (setting it as well would play the bed twice). V6 guided plans still send a timing-only `song_reference` instead — see `docs/runbooks/phone-rendering.md` §"Matched songs as posting references". |
| `narrationAudio` | `AudioMixRecipe.narration_asset_id` (montage-family "voiceover", `narrated*`, guided-story `.narration`) | A second audio-kind track playing the private `VoiceoverRenderAsset`; the voice clip carries the cloud's 0.5 s `audio_fade_out` (KRI-139). Rollout gates: `settings.phone_narration_rendering_enabled` + this bit in `phone_render_verified_features` — see `docs/runbooks/phone-rendering.md` §"Recorded voiceover (KRI-132)". |
| `audioDucking` | `EditRecipe.audio.duck_original_during_music` (phone narrated footage bed) | KRI-139: goes through the normal capability gate (the hard `CapabilityNegotiator` refusal is gone). The composer reads the audio-kind tracks (the voice), runs ffmpeg's `sidechaincompress` gain computer over them with the cloud's narrated settings, and ramps the footage bed's volume with the result (`AudioDynamics.swift`). `compile_phone_narrated_plan` only requests it when `audioDucking` is in `phone_render_verified_features`; otherwise the narrated bed stays a flat gain. |
| `processedAudio` | `asset_manifest.assets[].kind == processed_audio` plus `processed-source-audio` timeline clips | An opt-in server-made, time-preserving stationary-noise-reduction derivative of a bound footage source. The recipe carries only immutable source/job/variant/generation identities and a hash; the server grant derives the private object path from the job's trusted receipt. Each original video clip is muted and gets an audio clip with the same source interval, rate and timeline position. Requires `phone_render_verified_features` plus protocol 14; disabled by default with `PHONE_ENGINE_NOISE_REDUCTION_ENABLED`. |

## local-later — vocabulary exists, recipe schema has no fields yet

| Capability | Plan lane / source catalog | What's missing |
| --- | --- | --- |
| `soundEffects` | `.editor_sound_effects` (KRI-608: compiled for guided / montage behind `PHONE_GUIDED_SFX_ENABLED`; `.licensed_sfx_intent` is provenance only) | Now renders: Talking (KRI-174), Voiceover (KRI-281) and guided / montage (KRI-608) share the `sfx` audio track from `phone_subtitled_plan._compile_sfx_track`. Still missing: custom uploads (catalog only) and loudness normalization — `SoundEffectPlacement` in `app/agents/_schemas/sound_effect.py`. |
| `mediaCards` | `.editor_media_overlays` | `MediaOverlay` (pip/fullscreen, entrance/exit tokens, source crop, playback rate, z-order) — `app/agents/_schemas/media_overlay.py`. |
| `customEffects` | `.editor_custom_effects` | 20 FFmpeg effect tokens (`boxblur`…`zoompan`), `pipeline/custom_effects.py`; combinations of up to six plus timeline semantics required. |
| `motionPresets` | (no dedicated plan field; authored via the web motion-runtime editor) | 9 presets in `src/packages/motion-runtime/creator-blocks.catalog.json` (`card_stack`, `cloud_break`, `donut_text`, `evolving_type`, `film_strip`, `flow_field`, `kinetic_word`, `offer_swap`, `tag_stack`). Not the same runtime as native `motionScenes` above. |
| `carouselEffects` | (authored via the carousel editor, not the guided-story plan) | 4 effects in `pipeline/carousel/effects.py` (`cards_stack`, `cover_flow`, `flipbook`, `scale_sweep`). |
| `captions` | speech/caption lanes (`pipeline/captions.py`, `services/smart_captions.py`) | Analysis/planning split and a native caption compiler are both outstanding per coverage.md. |
| `slidePosts` | ordered image/video slide posts (`pipeline/slide_post/`) | Full creation/export contract outstanding. |
| `semanticCamera` | (authored via creative-direction agents, not the guided-story plan) | `semantic_crop_pulse`, `sine_pulse` — `pipeline/camera_effects.py`. Distinct from the native `cameraEffects` primitive above. |

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
