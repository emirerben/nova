# Split-screen verification on November 7

The feature remains local on `codex/editable-split-screen`. Tracker: [KRI-1231](https://linear.app/kria/issue/KRI-1231/build-editable-split-screen-videos). No push, PR, merge, deployment or rollout occurred in these checks. The full scope remains in [the acceptance matrix](acceptance.md).

## Completed checks and their revisions

| Revision | Check | Result |
| --- | --- | --- |
| `2604afd9b0979e3b32b4db95e9181569701d463d` | Native media engine, original 81-case selection plus six pixel-conversion cases | 87 passed, zero failed or skipped. The unchanged white-fill assertion passed. |
| `2604afd9b0979e3b32b4db95e9181569701d463d` | Merge, three saved-recovery regressions, scoped speaker Save/reopen/reset | 36 passed, zero failed or skipped. This verifies the sparse-row correction. |
| `2604afd9b0979e3b32b4db95e9181569701d463d` | Affected API integration | 649 passed, zero failed or skipped. |
| `8e1fff813b14984711b76c2f589b6aaae6691ca4` | Product acceptance batch | 98 executed: 95 passed, three failed, zero skipped. All 86 unit cases and nine existing UI journeys passed. The three new UI journeys failed. |

Both revisions include main `502ecbe47c78d2ff1b4fd467218f3c65fea02456`. API, engine source, engine tests and formal trees are identical between them. The later native revision adds acceptance tests, fixture persistence and an explicitly enabled DEBUG document observer. Tree equality preserves the earlier evidence's relevance; it is not another execution at the later head.

The product batch used Xcode 26.6, the existing iPhone 17 simulator on iOS 26.5 build 23F77, two build jobs and serial testing. `xcodebuild` PID 5071 ran from 01:51:58 through 01:59:42 UTC and exited 65. The lane was released at 01:59:57 UTC after result collection; no owned heavy process remained and 275 GiB was free.

## Three failed journeys and corrections

- `testSplitPanelSpeedCropAndSourceGainKeepPeersExact` found no stored playback rate after an XCUI slider adjustment. The recording shows the slider thumb below the home indicator despite its hittable state. The corrected gesture requires the complete track inside the inspector and uses a physical thumb drag. Rate, duration, crop, gain, exact peers, global mix and undo assertions remain.
- `testSplitPanelTrimCutDuplicateAndUndoKeepPeersExact` lost its timeline target after pinch. The recording and failure hierarchy show that the gesture opened Sounds. The corrected pinch targets the exposed clip row and asserts decreased zoom and an unchanged document before editing. Trim, source continuity, duplicate identity, peer and undo assertions remain.
- `testCustomThreePanelLayoutSaveReopenAndFullscreenBoundary` passed its initial custom-weight, gutter and red/blue/gutter pixel checks, then timed out waiting for Saved. Its new fixture lacked the API/item setup used by other Save fixtures. The correction adds its explicit test flag to that existing DEBUG transport setup. The test retains exact committed rows and section equality after relaunch. It now retains serialized document attachments and explicitly checks the four-second section duration.

Independent source review found no further concrete blocker in these corrections. They are **not rerun**. The 95 passes do not certify them. The pre-Save screenshot also displays 7.6 seconds; the new complete document attachment will help distinguish the fixture's playback clock from authored section timing. That observation alone does not establish a product defect.

The next prepared selection contains these three journeys and `SpeakerFrameDirectManipulationUITests/testScopedSpeakerSaveReopenAndFullResetThroughClipInspector`. It must run on the final clean correction head after parent allocation. All native, package, engine, simulator and formal launches are held for the combined-main owner.

## Formal correspondence and delivery limits

A read-only source audit at `8e1fff813` extracted all 98 covered entries with nonempty results. There are 36 differences from recorded coverage and two covered feature differences from current main: `NativeEditorSession.saveOnce` rejects an invalid split layout before Save, and `_client_update_detail` returns the protocol-43 upgrade requirement. No covered body changed between the two tested revisions. No model, coverage, verifier or checksum was changed, and no formal checker ran in this batch.

The shared formal owner must still review coupled section persistence, sparse/fallback merge, scoped speaker policy, protocol admission, source timing and fallible pixel conversion. Its bounded reset proof does not establish this feature's aggregate correspondence. Shared recovery repairs remain unpublished and separately approval-blocked; their broader failures are not waived.

The connected physical iPhone 13 Pro was reported unavailable. Physical-device and live-provider acceptance remain unrun. Section drag, captured import completion and the remaining operation matrix also remain open. Publication still needs required formal alignment, applicable final-head acceptance, preship evidence and PR checks. Any eventual PR must remain draft while acceptance is incomplete.

Local evidence is retained under `.dev/split-screen/`: `completed-2604afd9b-evidence.json`, `product-8e1fff813-evidence.json`, both xcresult bundles, `native-8e1fff813-attachments/manifest.json`, `formal-source-audit-8e1fff813.json`, `formal-readiness-8e1fff813.md` and `source-equivalence-8e1fff813.json`. These artifacts contain synthetic test data. This tracked summary preserves results and limitations for reviewers without committing media.
