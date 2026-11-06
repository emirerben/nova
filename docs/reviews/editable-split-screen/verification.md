# Verification and rollout record

This is a local feature implementation, not a deployed or published release. The isolated branch is `codex/editable-split-screen`, rebased onto `05d9720d8`. The final commit and working-tree status are reported in the handoff. User media and the live project were not used or changed.

## Executed evidence

| Boundary | Result | Retained evidence |
| --- | --- | --- |
| AI schema, recorded transport, parser, operations, source clocks, audio, receipts and regression neighbors | 1,345 passed after rebase and speaker-copy correction | `.dev/split-screen/ai-final.log` |
| Backend Save, source admission, initial creation, global/source ownership, song controls and regression neighbors | 475 passed; later speaker-window projection checks: 277 passed | `.dev/split-screen/backend-final.log`, `backend-background-final.log` |
| Real database manual → AI layout edit → manual text Save → reopen | Passed; three immutable saved drafts retain independent IDs, clocks, crops, gain and transform | `.dev/split-screen/persistence-postgres-final.log`; `tests/kria/test_split_screen_persistence_postgres.py` |
| Protocol recovery guidance | Failed with 42, corrected to 43; 43 split tests passed before the later background suite | `.dev/split-screen/protocol-guidance-{red,green}.log` |
| Native compositor and export | 14 split tests passed after background-focus integration; 42 focus/speaker manipulation neighbors passed | `.dev/split-screen/panel-focus-green.log`, `focus-neighbors.log` |
| Generated contracts | API contract, TypeScript projection and mobile OpenAPI check passed | `.dev/split-screen/contracts-check.log` |
| Required timeline regressions | 209 Jest and 28 desktop Playwright passed; mobile: 41 passed, one case passed on retry | `.dev/split-screen/editor-timeline-{gate,desktop,mobile}.log` |
| Mapped API incident regressions | 1,672 passed again after rebase; all changed paths mapped to 156 journeys | `.dev/split-screen/preship-final.log` |
| Native app build and focused unit tests | App and test targets compiled; 31 tests passed: 24 split model/session/compiler/hydration, five timeline-row and two speaker geometry tests | `.dev/split-screen/integrated-ui-source.log` |
| Native UI journeys | Two journeys failed: source replacement menu did not expose its choices, and the three-panel selection assertion did not report Middle selected. Acceptance remains open. | `.dev/split-screen/integrated-ui-source-results.xcresult`; saved hierarchy and recording in `source-attachments/` and `source-all-attachments/` |
| Native interaction neighbors | Earlier runs passed ordinary footage duplication, existing speaker manipulation, repeated-speaker panel selection/drag and the ordinary subframe compiler regression | `.dev/split-screen/integrated-ui-validated.log`, `integrated-ui-fixture.log` |

Synthetic native media uses marked frames, colored crops, independent audio tones and decoded PCM comparisons. The tests inspect preview and exported MP4s for two/three panels, mixed photos/video, three concurrent video clocks, audio gains/mute, source crops, transitions, held frames, looks/backdrops, gaps and exact full-screen boundaries. Speaker tests exercise repeated owned subjects, unrelated footage, local masks/frames, timed backgrounds and focused images. The inspected `scoped-focus-montage.png` shows preserved independent top/bottom subjects with a white middle gap and panel-local focus.

One existing white-background export test failed six channel assertions: decoded `[253,253,252]` versus `[255,255,255]` at tolerance 2. An untouched `31477c8e9` package reproduced the exact failures. Assertions were not widened. Logs: `.dev/split-screen/white-background-{baseline,recheck}.log`.

## Gates and acceptance limits

The final bounded native run used PID `38844`, simulator `4B3ACE87-16A1-4E38-B37F-36B9C1AE4EF0` (Codex Split Screen Task 3), and `/tmp/kria-split-native-derived`. It executed unit tests and UI tests, ended test execution at 09:55:23 UTC, and finished result finalization with exit 65 at 09:57:11 UTC on 2026-11-06. A read-only process check confirmed the PID ended. This task has released the native lane and is not waiting on another owner. Per the parent coordination instruction for Cunda recovery, no further build, test, simulator or diagnostic launch is authorized until that temporary hold is released. Reading the completed result bundle does not run native tests.

The last executed native code was `8d220c165`. Saved recordings prove the Middle-row tap was covered by the floating clip toolbar. The timeline already has bottom scroll clearance, so the test now scrolls that row clear and asserts actual selection before the unchanged Middle assertion. The replacement menu now gives its entire visible row an explicit hit shape; its absent-popup failure is not yet proven fixed. The mixed-media fixture installed a real source resolver, which also triggered an account-dependent audio-recipe fetch. Its explicitly empty audio lane is now marked resolved, while the photo still goes through the normal asynchronous resolver and verified disk cache. Independent static review found no concrete issue in these follow-up changes. They have not been compiled or rerun during the hold.

The final broad native session/compiler/interaction/speech/label/lifecycle regressions remain unrun. The failing UI assertions are retained; passing unit and renderer tests are not a substitute for those journeys. The mixed-media UI test checks source replacement controls and retains a screenshot; it does not automatically compare rendered photo pixels. A passing rerun still needs visual inspection.

`make verify-phone-render` did not run TLC. The pinned v1.8.0 download returned a JAR declaring 2.0, SHA-256 `654aa50197faa48fa0136b41257270de6469d29ef323e7822ead59d2be43ce10`, instead of required `fc6878c603f0fdc18b9fe3e5bf51860cb90ab5b1154e3c371660c036d6bdd63e`. The checksum guard was retained. The base already had 26 covered-function drifts. The branch adds a local invalid-layout Save guard and another protocol-floor check; the existing TLA model represents accepted Saves and parameterized protocol floors. No blanket hash resync or model-pass claim was made. Logs and branch/baseline hash comparison are retained under `.dev/split-screen/phone-render-*`.

The preship request-evidence gate stopped on affected uncommitted changes during the recorded run. Publication evidence also requires an assigned KRI issue. No matching split-screen issue was found in read-only Linear searches, and this task has no publication authority. No issue identifier or PR evidence was fabricated. The implementation acceptance matrix remains the local record until an issue and publication scope are supplied.

No live model request, physical iPhone journey or production rollout was executed. Recorded structured provider output exercises the real parser and operation compiler; it does not prove general natural-language planning quality. Native AVFoundation artifacts prove local renderer behavior, while simulator UI and backend persistence evidence remain separately identified.

## Qualification and rollout

The protocol is 43 because background focus landed on 42 during this work. A qualified device needs `splitScreen` in the verified feature set; this branch does not enable a production cohort. Older clients receive `client_update_required`, and explicit editor capability admission keeps unsupported/cloud paths closed. Independent source timing, ordinary media properties and section membership are persisted rather than flattened into a rendered-video workaround.

Before publication: complete the remaining native acceptance/gates, assign the feature's issue, bind evidence to the reviewed final head, and review any newer upstream drift. No push, PR, merge or deployment was performed.

## Resume after native resource clearance

The following command is a continuation recipe, not executed evidence. It rebuilds the follow-up corrections, repeats the failing UI cases, and broadens native model/session regression coverage. Inspect the mixed-media recording and confirm the preview contains both the video and photo after replacement. Further physical-device, live-provider and lifecycle acceptance remains separate.

```sh
xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria -configuration Debug \
  -destination 'platform=iOS Simulator,id=4B3ACE87-16A1-4E38-B37F-36B9C1AE4EF0' \
  -derivedDataPath /tmp/kria-split-native-derived \
  -resultBundlePath .dev/split-screen/resumed-native-results.xcresult \
  -jobs 2 -parallel-testing-enabled NO -disableAutomaticPackageResolution \
  -only-testing:KriaTests/NativeSplitScreenTests \
  -only-testing:KriaTests/NativeSplitScreenRowsTests \
  -only-testing:KriaTests/NativeSpeakerPanelGeometryTests \
  -only-testing:KriaTests/NativeEditorSessionTests \
  -only-testing:KriaTests/NativeEditorRenderCompilerTests \
  -only-testing:KriaTests/NativeEditorInteractionTests \
  -only-testing:KriaTests/NativeSpeechTimelineTests \
  -only-testing:KriaTests/GuidedLabelRebaseVectorTests \
  -only-testing:KriaTests/EditorDocumentTests \
  -only-testing:KriaUITests/NativeEditorInspectorUITests/testSplitScreenCreationKeepsOrdinaryClipControlsAndPanelRows \
  -only-testing:KriaUITests/NativeEditorInspectorUITests/testSplitPanelReplacesVideoWithUnusedPhotoAndKeepsCrop \
  test CODE_SIGNING_ALLOWED=NO
```
