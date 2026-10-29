# Text behind people on the phone — failure modes (written before the code)

Scope of this first cut: export + live preview through `RecipeVideoCompositor`,
one person, Vision `VNGeneratePersonSegmentationRequest`. The cloud lane
(`docs/pipelines/text-behind-subject.md`) stays as it is; the phone lane is a
second, independent implementation of the same alpha-multiply idea.

Design decision that removes a whole class of bugs: the phone segments the
**composited canvas frame** (video layers + motion scenes, before text), not the
source clip. The mask therefore lives in canvas space by construction — the
clip's crop, cover fit, zoom, rotation, transition blend and camera pulse are
already baked into the pixels Vision sees. There is no source→canvas mask
re-registration step to get wrong (the cloud's `_mask_for_shape` resize class
of bugs has no phone twin).

| # | Failure mode | Treatment | Pinned by |
|---|---|---|---|
| F1 | Mask misregistered under crop / transform / transition / camera pulse | Segment the composited canvas frame (above). The occlusion multiply happens on the text AFTER its own placement transform, in canvas space. | `SubjectOcclusionTests` (rectangle mask → zero alpha inside, full outside, in canvas coordinates); `NativeSubjectMatteCompositionTests` (real `AVPlayerPreviewComposer` build, cropped clip, sampled frame) |
| F2 | Strobing silhouette (confidence flapping near the cut) | Trailing 3-frame median before the 0.40 hard cut; presence-flip and large-IoU-jump counters over a trailing window latch the clip segment to "unstable" → plain text for the rest of that cut. | `SubjectMaskTreatmentTests.testSingleFrameBlipIsRemovedByTheMedian`, `testStrobingPresenceLatchesUnstable`, `testLargeShapeJumpsLatchUnstable` |
| F3 | Text fully hidden (mask swallows the frame: wall-of-person, camera against a shirt) | Per-tick coverage > 0.85 → mask dropped for that tick → plain text. Never a black/empty text. | `SubjectMaskTreatmentTests.testFullFrameMaskIsUnsane`; compositor test "full-frame mask paints plain text" |
| F4 | Two people / background passers-by | Vision's person request is class-level, so every person occludes. Accepted for the first cut (documented limit: "single person" means we do not pick one). Tiny fragments (< 0.2 % of the frame) are dropped like the cloud. | `SubjectMaskTreatmentTests.testTinyFragmentsAreDropped` |
| F5 | Subject leaves the frame | Coverage < 0.01 → nil mask → plain text; nothing is held from a previous tick once the subject is gone (the median window is per-frame confidence, so an empty frame empties within one tick). | `testEmptyMaskIsNil` |
| F6 | Hard cut: previous clip's silhouette occludes the first frames of the next clip | Temporal state (median history, flip counters, held mask) is keyed by the active primary clip id(s); a key change resets it. A time jump > 0.25 s (preview seek) also resets. | `testCutKeyChangeResetsHistory`, `testSeekResetsHistory` |
| F7 | Thermal / perf (gate 7: 30 fps preview, seek p95 ≤ 250 ms, 60 s export ≤ 120 s) | Segmentation at ≤ 15 Hz (every 2nd output frame at 30 fps) with hold; input downscaled to ~270×480; `.balanced` quality; `OneComponent8` output; only runs on ticks where an active text layer is flagged. The segmenter is created lazily and only when the recipe uses the flag. **Not measured on a device in this cut** — the device test must time a 60 s export. | device test (owed) |
| F8 | Preview ≠ export | Same compositor class, same instruction, same matte object for `AVPlayer` and the exporter. One deliberate divergence: the direct-manipulation text drag (`textInteractionLayers`) paints the selected text without occlusion while the finger is down. | compositor test runs through `AVAssetImageGenerator` on the preview composition (the export path) |
| F9 | Old client gets a recipe it cannot decode | `behind_subject` is an unknown key for protocol ≤ 20 (`rejectUnknownAssetFields`) → the recipe would FAIL, not render wrong. Compile-time: the server strips the flag (plain text + fallback trace/log) for a request below `text_behind_subject_phone_min_client_protocol`. Read-time: a pinned recipe that already carries the flag answers 426 `client_update_required` (`text_behind_subject`) to an older build, mirroring `text_bend`. | `test_text_behind_subject_phone.py::test_protocol_below_min_strips`, `::test_device_render_poll_tells_an_old_build_to_update` |
| F10 | Flag off / token not verified | `TEXT_BEHIND_SUBJECT_PHONE_ENABLED=false` (default) or `behindSubject` ∉ `PHONE_RENDER_VERIFIED_FEATURES` → compile strips the flag; the recipe is byte-identical to today's. `validate_phone_pilot_recipe` refuses a recipe that still carries it without the token (a caller that mutated after compile). | `test_flag_off_is_byte_identical`, `test_verified_token_missing_strips`, `test_pilot_validation_refuses_unverified` |
| F11 | sRGB / linear mismatch | The compositor works in linear sRGB. The mask is loaded with `.colorSpace: NSNull()` so its 0…1 values are NOT re-curved on the way in; the frame handed to Vision is rendered to an sRGB buffer (display-referred, what the model was trained on). A binary mask only has its feather pixels at risk either way. | `SubjectOcclusionTests.testHalfMaskGivesHalfAlpha` (a 0.5 mask → ~0.5 alpha, not 0.21) |
| F12 | HDR / HEVC sources | The composition is SDR Rec.709 before the compositor sees any pixel (see `MediaCapability` doc comment), so the segmenter always gets tone-mapped SDR. No special path. | existing `hdr` capability notes; no new test |
| F13 | Vision unavailable / throws (simulator without a model, memory pressure) | Every segmenter error is swallowed into "no mask" for that tick → plain text. The export never fails because of occlusion. | `testSegmenterErrorFallsBackToPlainText` |
| F14 | Text layer validation: flag on a shape the painter cannot occlude | `behindSubject` is refused with a giant-title transition (the cloud keeps `theme_transition` separate too); allowed on every other effect because the multiply happens on the final placed bitmap. | `PortableTextLayer.validate` + Python `valid_window` |
| F15 | Capability drift Swift ↔ Python | `behindSubject` added to both `MediaCapability` mirrors and the capability-matrix doc. | `tests/kria/test_capability_matrix.py` |

Device test (owed, not done here): on an iPhone, a montage with a person
walking through a "behind subject" title must show the title hidden where the
person overlaps it on at least one frame of the export AND the preview, and a
60 s export must finish within the gate-7 budget (≤ 120 s) without a thermal
step-down.
