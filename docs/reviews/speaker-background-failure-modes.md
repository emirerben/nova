# Talk-to-camera green screen (speaker background) — failure modes (written before the code)

Linear: project `Talk-to-camera green-screen backgrounds` (P-KRI-21), phases KRI-1114 to KRI-1120.

Scope of the first cut: one background (image or video, muted, looping) behind the
speaker for the whole edit, on device-rendered Talk-to-camera (`subtitled`) edits.
Entry points: live camera preview, editor Background tool, chat (strategy + ops).
Cloud talking_head variants, the web editor, windowed or per-clip backgrounds and
blurred backgrounds are out of scope.

The pinned test names below are the planned names. A row is not done until its
test exists and is green, or it is marked `device (owed)`.

## Decisions that remove whole classes of bugs

- **Segment the speaker layer, not the composited scene.** The text-behind-subject
  matte segments the composited canvas, which is right for hiding text. For a
  background the mask must come from the speaker clip only, after its transform and
  before cutaways, fullscreen cards and overlays, otherwise a cutaway or card would
  be cut out of the picture. The mask still lives in canvas space by construction.
- **Soft alpha, not a hard cut.** The text matte hard-cuts at 0.40. A full-frame
  matte with a hard cut gives blocky, haloed hair. The speaker matte keeps the soft
  confidence and smooths it over time.
- **Failure holds, it never flashes.** The text matte answers "no mask" on any
  failure, which paints plain text. For a background that would show the creator's
  real room for the rest of the cut. The speaker matte holds the last good mask. If
  it never produced one, the frame renders without the background layer at all.
- **One recipe lane, absent when unused.** `speaker_background` is omitted from the
  recipe when None, so every recipe that does not use it keeps its digest.
- **The take records raw.** The camera previews the background live but records the
  original video, so the background stays editable and removable afterwards.

| # | Failure mode | Treatment | Pinned by |
|---|---|---|---|
| S1 | Hard, haloed or blocky edges (hair, fingers, glasses) | Soft confidence kept (no 0.40 cut), temporal smoothing, edge feather or guided upsampling against the full-resolution frame. Settings (quality level, resolution) chosen by the Phase 0 spike, recorded below. | `SpeakerMatteTests.testSoftEdgeKeepsIntermediateAlpha`; engine render test samples a pixel row across a synthetic soft edge |
| S2 | Silhouette steps or lags when the person moves (mask rate below 30 fps) | Rate chosen by the spike. If below output rate, interpolate the confidence between ticks instead of holding a stale mask. | `SpeakerMatteTests.testMaskInterpolatesBetweenTicks` |
| S3 | Segmentation fails or throws on a frame (memory pressure, no model on simulator) | Hold the last good mask for a short grace window. After the window the frame renders with no background layer (the original footage). Never a black or half-cut frame, and the export never fails. | `SpeakerMatteTests.testErrorHoldsLastGoodMask`, `testNeverProducedMaskRendersOriginal` |
| S4 | Speaker leaves the frame | Coverage under the minimum (nobody in frame) means no speaker pixels to keep, so the frame is the background alone. The grace window in S3 does not apply here, an empty mask is a legitimate answer. | `SpeakerMatteTests.testEmptyMaskShowsBackgroundOnly` |
| S5 | Mask swallows the frame (person against a wall of similar color, coverage above the maximum) | Treated as an unreliable tick: hold the last good mask (S3 policy). Never replace the whole frame with the background because of one bad tick. | `SpeakerMatteTests.testFullFrameMaskIsUnsane` |
| S6 | Two people or passers-by | Vision's person request is class-level, so every person is kept. Accepted for the first cut and documented. Tiny fragments are dropped like the text matte. | `SpeakerMatteTests.testTinyFragmentsAreDropped` |
| S7 | Hard cut between clips: previous clip's silhouette leaks onto the next | Temporal state (smoothing history, held mask) is keyed by the active speaker clip id. A key change, or a time jump above 0.25 s (preview seek), resets it. | `SpeakerMatteTests.testClipKeyChangeResetsHistory`, `testSeekResetsHistory` |
| S8 | A cutaway, fullscreen card or overlay is cut out like a person | Compositor order: background, then speaker x mask, then the cutaway / fullscreen / overlay tracks, then text, then drawings. The tracks above the speaker are never masked. | `RecipeVideoCompositorTests.testCutawayCoversSpeakerAndBackground` |
| S9 | Background shorter than the edit (video) | Loops from `source_start`. An image is held for the whole edit. A zero-duration or unreadable asset fails the recipe at compile time, not mid-render. | `SpeakerBackgroundTests.testShortVideoLoops`, `test_speaker_background_zero_duration_is_dropped` |
| S10 | Background aspect differs from the canvas (portrait vs landscape, odd sizes) | Cover fit centered, same helper the fullscreen cutaway uses. HEIC and rotated assets are normalized by the existing visual-pool upload path. | `SpeakerBackgroundTests.testLandscapeBackgroundCoverFits`, `testRotatedImageUsesOrientation` |
| S11 | Video background audio bleeds into the mix | Always dropped. The recipe carries no audio for the background asset, and the audio mix is built from the speaker track only. | `NativeCompositionTests.testBackgroundVideoContributesNoAudio` |
| S12 | Speaker clip is itself landscape, face-filled, or cropped | The matte reads the speaker layer after `face_fill_transform` / `fit_transform`, so crop and zoom are baked in. | `SpeakerBackgroundCompositionTests.testFaceFilledSpeakerIsMatted` |
| S13 | Speech cuts retime the edit | The background is whole-edit, so cuts do not move it. A video background keeps looping on output time, not source time. | `test_speaker_background_survives_speech_cut` |
| S14 | Perf and thermals: 30 fps preview, seek p95 at most 250 ms, 60 s export at most 120 s | Budget measured by the spike. Segmentation resolution and rate chosen to fit it. Segmenter created lazily, only when the recipe has a background. | device (owed) |
| S15 | Preview differs from export | One compositor class, one instruction, one matte object shared by `AVPlayer` and the exporter. | `SpeakerBackgroundInPreviewAndExport` |
| S16 | Old app build receives a recipe it cannot decode | `speaker_background` is an unknown key below protocol 27 and the decoder is strict, so the job would fail. Compile strips it below the minimum protocol or when the token is missing. A pinned recipe that already carries it answers 426 `client_update_required` (`speaker_background`) to an older build, on poll and on Save. | `test_speaker_background.py::test_protocol_below_min_strips`, `::test_device_render_poll_tells_an_old_build_to_update`, `::test_save_from_old_build_is_426` |
| S17 | Flag off or token not verified | `SPEAKER_BACKGROUND_ENABLED=false` (default) or `speakerBackground` not in `PHONE_RENDER_VERIFIED_FEATURES`: compile strips the lane, recipe byte-identical to today, trace `speaker_background_phone_fallback`. `validate_phone_pilot_recipe` refuses a recipe that still carries it without the token. | `test_flag_off_is_byte_identical`, `test_verified_token_missing_strips`, `test_pilot_validation_refuses_unverified` |
| S18 | Chat asks for a background that cannot be applied (cloud variant, gate closed, asset missing) | Honest receipt "Couldn't ..." with the reason, never a silent plan change. A camera pick is written into the strategy by deterministic repair when the agent drops it (the creator's explicit pick wins). | `test_background_media_repair_keeps_camera_pick`, `test_set_background_unavailable_is_honest` |
| S19 | A background asset the creator did not attach (asset id from another item or user) | `set_background` and the strategy field only accept ids in this item's pool (`PlanItemAsset`), like `add_overlay`. The signed URL is re-checked per exact generation at device render. | `test_set_background_rejects_foreign_asset` |
| S20 | Editor parity drift: an iOS `setSpeakerBackground` with no agent op | New group `speaker_background` in `PARITY_GROUPS`. | `test_ios_editor_actions_have_agent_ops` |
| S21 | Capability drift Swift and Python | `speakerBackground` added to both `MediaCapability` mirrors and the capability-matrix doc. | `tests/kria/test_capability_matrix.py` |
| S22 | Protocol number collision | Server already uses 26 (`RENDERED_TIMELINE_CLOCK_PROTOCOL`), so this feature takes 27. Re-check open branches and `Services.swift` before merging. Header assertions in `KriaTests.swift` and `SuggestionChipsTests.swift` bumped in the same commit. | `KriaTests` header assertions |
| S23 | Live camera: second capture output breaks recording (dropped frames, audio desync, the KRI-653 silent-take class) | Add `AVCaptureVideoDataOutput` beside the movie output with `alwaysDiscardsLateVideoFrames`, on its own queue, never touching the audio path. If both outputs cannot run together on target devices, stop and decide between `AVAssetWriter` and a still-frame preview. | spike (owed on device) |
| S24 | Live preview matte differs from the rendered result | The preview is a convenience. The recorded take is raw and the editor render is the source of truth, so the camera shows "Preview" and never claims pixel parity. | n/a, copy review |
| S25 | Camera interruption, backgrounding, thermal step-down while the matte runs | Matting stops with the session. On resume the preview falls back to the raw feed until the matte produces a frame. Recording is never gated on the matte. | `TeleprompterRecordingFlowTests.testRecordingDoesNotDependOnMatte` |
| S26 | Memory: full-resolution background plus matte buffers plus recording | Background image downscaled once to canvas size. Matte buffers pooled. Video background decoded by the compositor like any visual. | device (owed) |

## Phase 0 spike (decided by measurement)

Questions, each answered by running something. Results are written here, not asked.

| Question | How | Result |
|---|---|---|
| Edge quality: `.balanced` vs `.accurate`, soft vs hard mask, with and without guided upsampling | Engine harness on real talking-head frames, montage of cutouts over a busy background | TBD |
| Mask rate: 15 Hz with hold vs 30 Hz vs 15 Hz with interpolation | Same harness on a moving subject, look for stepping | TBD |
| Cost: 60 s export, 30 fps preview, seek p95 | Physical iPhone only. Simulator and Mac numbers do not satisfy this. | owed (iPhone 13 Pro `unavailable` at the time of writing) |
| Camera: can `AVCaptureMovieFileOutput` and `AVCaptureVideoDataOutput` run together | Physical iPhone only. The simulator has no camera. | owed (same) |

Device tests owed before the flag can be flipped (same standard as
`text-behind-subject-phone-failure-modes.md`): on an iPhone, a Talk-to-camera take
with a photo background, then a video background, must show the creator cleanly
cut out in both the preview and the export, a cutaway must cover the speaker, a
60 s export must finish within 120 s without a thermal step-down, and the
recording made with the live preview on must have clean audio.
