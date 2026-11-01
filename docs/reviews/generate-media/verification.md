# Generate media verification

The initial separate generator sheet is superseded by the selected inline Visuals implementation. See [inline-design.md](inline-design.md) for current acceptance results and screenshots. Older results below remain historical evidence.

The implementation adds a native Visuals entry with prompt and automatic modes. It creates a read-only grounded plan, reviews individual assets, and applies accepted overlays through the existing native editor. It does not enable production provider flags or budgets.

## Verified locally

- Native build and 17 selected tests pass on the isolated iPhone 17 Pro simulator running iOS 26.5. The selection includes the real Visuals screen, number insertion, stable identity, stale-plan rejection, undo, snapshot reopen, generated-media recovery contracts and the matching native frame-clock fixture.
- A transparent synthetic PNG passes through the production native compositor and exports to MP4. Pixel checks inspect frames before, during and after its 0.5 to 1.5 second window. The graphic appears during the window and disappears afterward. The exported frame was also inspected visually.
- Backend planning and adjacent clock/provider-contract suites pass with 120 tests and 24 subtests. Wrong-word and unowned-asset controls, original word timestamps, silent visual context, OCR, unavailable context, explicit times, Carousel conversion and crossing-interval rejection are included.
- Mapped reliability gates pass with 514 tests and no environment skips or errors.
- The timeline gate's 14 Jest suites pass with 209 tests. Its browser stages pass separately with 28 desktop and 42 mobile tests. The first browser attempt could not bind its fixture server inside the sandbox; the authorized rerun used the same tests with local-server access.
- The existing phone-render TLA+ lifecycle check passes with no residual findings. It checks its modeled lifecycle, not new image-generation semantics.
- Scoped Ruff lint and formatting, skill validation, the instruction-size guard and diff checks pass.

The native command uses xcodebuild with OverlayGenerationTests, GeneratedMediaTests, the NativeEditorInteractionTests marked-frame-clock case, and OverlayGenerationUITests. The backend command selects test_overlay_generation, test_guided_timeline_equivalence, test_kria_editor_guided_timeline, test_carousel_timed_lane_projection, test_segment_kill_switch, test_credit_plan_contracts and test_generated_media_transport.

## Retained artifacts

All three Library items were read back from successful upload responses at version 1.

| Artifact | Confirmed Library ID | Meaning |
| --- | --- | --- |
| Native synthetic overlay export | libfile_61411bbc76f08191b40e34f4ad2ea25a | Actual compositor and MP4 export, synthetic asset |
| Plan review screen | libfile_3d8ba3d096188191ad533ba5360650b9 | Actual native UI with an offline proposal |
| Added overlay screen | libfile_ebe3d5d38efc81918052877c72b4c3e2 | Actual native UI after insertion |

The UI fixture is explicitly synthetic. Its proposal does not prove that a speaker said "number ten." Exact speech grounding has separate backend source-word fixtures. No video bytes, private transcripts or credentials are committed.

## Independent review and fixes

The independent Astra review found orphaned recovery after replanning, Carousel output-clock drift and frame-clock differences at retimed transitions. Earlier plans now remain accessible with their original proposal IDs. Stale plans can recover previews while generation and application stay disabled. Shared clock arithmetic now matches the native fixture. Intervals that would stretch across a Carousel insertion receive clarification.

The first UI test also showed that selecting the inserted text immediately dismissed the hosting Visuals sheet. Application now leaves selection unchanged until the creator returns to the editor. The subsequent UI test passed.

## Remaining acceptance work

- Live-model semantic and taste quality have not been evaluated. This run made no paid provider calls.
- Image recovery across replanning was reviewed in code. The native UI fixture covers text; it does not exercise that complete image-provider interruption journey.
- Face and caption safety use available source metadata plus mandatory creator review. Moving occlusion throughout the interval is not certified.
- Missing original word timings cannot support exact spoken triggers. Changed speech timing may require Save and reopen. Whole-shot descriptions cannot locate precise gestures.
- New generation is available only for on-device-rendered edits. Legacy cloud edits show an availability message. Intervals inside inserted Carousels are not supported by the base-time overlay lane.
- The database-specific generated-media service tests were skipped locally because their fixture requires a named isolated database. The mapped reliability tests did run without environment skips.
- CI, merge, backend deployment, TestFlight delivery and phone acceptance have not been completed.

## Publication blocker

Automatic approval review denied creation of the proposed Linear project "Generated overlays." The stated reason was that creating a persistent roadmap project is an external organizational commitment and feature-build authorization did not explicitly cover that project or its commitments. No project or ticket was created, and the action was not retried or routed through another tracker.

The repository's request-evidence gate requires a real KRI owner issue. That tracking requirement remains unresolved. This implementation must remain a draft until the owner approves the project and ticket or identifies an appropriate existing owner issue. An unrelated closed ticket must not be used to make the gate pass.

Publishing the branch was also denied. The exact action was git push -u origin codex/generate-media-overlays. Automatic approval review stated that publishing an external repository change required explicit authorization beyond building and testing. The push was rejected before execution. No remote branch or PR was created, and no alternate publication route was attempted. Runtime code and tests are committed locally at 6a3bcb308d7e3234b070180aec1923a0868213e5. The prepared draft description remains in the worktree at .dev/pr-body.md.
