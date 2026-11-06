# Verification and rollout record

This is a local feature implementation, not a deployed or published release. The isolated branch is `codex/editable-split-screen`, rebased onto `05d9720d8`. A read-only remote lookup on 2026-11-06 still returned that main commit. The configured origin fetch and push destination is `https://github.com/emirerben/nova.git`. Draft publication and eventual merge are now authorized, conditional on the required acceptance and gates; no rollout is authorized. The final commit and working-tree status are reported in the handoff. User media and the live project were not used or changed.

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
| Native app build and focused unit tests | App and test targets compiled; 31 tests passed again at `5c9d43ad0`. The new real asynchronous replacement/reopen/compiler regression passed at `44dc5d213` after seven red assertions reproduced stale type, EOF and photo layout. | `.dev/split-screen/resumed-focused.log`, `replacement-red.log`, `accepted-native.log` |
| Broader native regressions | At `44dc5d213`: 423 tests executed; four cases failed with 58 assertions (one guided-label vector case and three narrated-audio compiler cases). Corrections retain the original expectations and await rerun. | `.dev/split-screen/accepted-native.log` |
| Native UI journeys | Mixed photo/video replacement, ordinary duplication and repeated-speaker selection/drag pass at `44dc5d213`. Three-panel selection, position and replacement assertions pass, but its preceding scroll-clearance assertion fails. A root gesture correction is implemented and awaits rerun. | `.dev/split-screen/accepted-native-results.xcresult`; screenshots and recordings in `accepted-native-attachments/` |
| Native interaction neighbors | Earlier runs passed ordinary footage duplication, existing speaker manipulation, repeated-speaker panel selection/drag and the ordinary subframe compiler regression | `.dev/split-screen/integrated-ui-validated.log`, `integrated-ui-fixture.log` |

Synthetic native media uses marked frames, colored crops, independent audio tones and decoded PCM comparisons. The tests inspect preview and exported MP4s for two/three panels, mixed photos/video, three concurrent video clocks, audio gains/mute, source crops, transitions, held frames, looks/backdrops, gaps and exact full-screen boundaries. Speaker tests exercise repeated owned subjects, unrelated footage, local masks/frames, timed backgrounds and focused images. The inspected `scoped-focus-montage.png` shows preserved independent top/bottom subjects with a white middle gap and panel-local focus.

One existing white-background export test failed six channel assertions: decoded `[253,253,252]` versus `[255,255,255]` at tolerance 2. An untouched `31477c8e9` package reproduced the exact failures. Assertions were not widened. Logs: `.dev/split-screen/white-background-{baseline,recheck}.log`.

## Gates and acceptance limits

An earlier bounded native run used PID `38844`, simulator `4B3ACE87-16A1-4E38-B37F-36B9C1AE4EF0` (Codex Split Screen Task 3), and `/tmp/kria-split-native-derived`. It executed unit tests and UI tests, ended test execution at 09:55:23 UTC, and finished result finalization with exit 65 at 09:57:11 UTC on 2026-11-06. A read-only process check confirmed the PID ended. The lane was released for Cunda; subsequent authorizations and releases are recorded below. Reading a completed result bundle does not run native tests.

The hold above was later released for two bounded batches. PID `44449` ran the focused checks at `5c9d43ad0`, released by 10:57:46 UTC: 31 unit tests and mixed-photo UI passed; three-panel UI failed. PID `50602` reproduced seven replacement-metadata assertions, then ended with exit 65; its process release was confirmed at 11:05:38 UTC. A subsequent parent clearance allowed native PID `73770` at `44dc5d213`, followed serially by the required formal aggregate. No native jobs were run concurrently by this task.

The last executed native code was `44dc5d213`. The full-row menu hit target and fixture audio correction are now runtime-verified. The inspected screenshot `resumed-focused-attachments/094C2E5B-9B85-4C5D-A8A2-8B5D50268CA5.png` shows the source video above the blue replacement photo. The asynchronous replacement regression passes after source kind/EOF/layout correction, preserving crop, transform, tilt, gain and section membership through reopen and compilation.

Follow-up corrections remain unverified: serial titles with stale `segment_id` again follow their actual timeline windows; narrated clips remain silent unless explicitly opted in; enabling missing/null/zero gain persists an explicit gain; and horizontal scrubbing no longer captures the label gutter's vertical scroll gesture. The new audio regression exercises session → reopen → compilation with narration. Existing label vectors, narrated-audio expectations and three-panel clearance/selection assertions were retained. Independent static review found no remaining concrete issue in these corrections.

The 423-case broader batch includes session lifecycle, interaction, document, speech and label/compiler tests. The newly corrected cases still need a rerun before acceptance. The mixed-media UI test checks source replacement controls and retains a screenshot; its pixels were inspected separately and are not asserted by the UI test.

The initial `make verify-phone-render` attempt did not run TLC. The pinned v1.8.0 download returned a JAR declaring 2.0, SHA-256 `654aa50197faa48fa0136b41257270de6469d29ef323e7822ead59d2be43ce10`, instead of required `fc6878c603f0fdc18b9fe3e5bf51860cb90ab5b1154e3c371660c036d6bdd63e`. The checksum guard was retained. The base already had 26 covered-function drifts. The branch adds a local invalid-layout Save guard and another protocol-floor check; the existing TLA model represents accepted Saves and parameterized protocol floors. No blanket hash resync or model-pass claim was made at that point. Logs and branch/baseline hash comparison are retained under `.dev/split-screen/phone-render-*`.

The artifact blocker was subsequently resolved: the parent supplied `/Users/emirerben/Documents/Codex/2026-10-30/task/nova-overlays/.dev/formal-ios-edit-flow/tla2tools.jar`. Independent SHA-256 inspection matched the unchanged required digest exactly. A task-local copy at `.dev/formal-ios-edit-flow/tla2tools-pinned.jar` also matched. The supported `TLA2TOOLS` override and installed Java 21 were used for the serial aggregate; the pin was not changed.

The aggregate completed: happy publication, 20 model scenarios, five historical counterexamples and 33 mutation checks behaved as expected, with no residual finding. It reported **27 covered-function drifts** and exited 3 (`make` exit 2). This is not an all-green gate. The separate Swift/Python source audits identified substantive pre-existing model gaps around durable Stop/Resume, ownership and asynchronous command/run fencing. A blanket coverage resync is not justified. See [formal alignment](formal-alignment.md), `.dev/split-screen/phone-render-recovered.log` and `.dev/formal-ios-edit-flow/report.json`.

The formal runner/model/coverage inputs were unchanged from `44dc5d213`. The later label/audio/scroll fixes were not validated by that native run, and the lifecycle proof does not validate media output. Relevant checks must run again on the final integrated revision. The complete native/formal lane was confirmed released at 11:39:25 UTC; all further native/package/simulator/heavy-formal launches are held for Cunda until explicit parent clearance.

The preship request-evidence gate stopped on affected uncommitted changes during the recorded run. Publication evidence requires a real KRI issue and an issue owning each pending gap; the validator does not require a new Linear project. No matching split-screen issue was found in read-only searches. The roadmap convention recommends a feature project, but one issue in an appropriate existing project is the smallest external record needed for CI evidence. Creating the proposed Linear project was rejected by automatic approval review because the destination and implementation-detail payload lacked explicit approval. No project or issue was created; no alternate route or retry was attempted. The drafts remain local, and the parent owns the permission question. No identifier or PR evidence was fabricated.

No live model request, physical iPhone journey or production rollout was executed. Recorded structured provider output exercises the real parser and operation compiler; it does not prove general natural-language planning quality. Native AVFoundation artifacts prove local renderer behavior, while simulator UI and backend persistence evidence remain separately identified.

## Qualification and rollout

The protocol is 43 because background focus landed on 42 during this work. A qualified device needs `splitScreen` in the verified feature set; this branch does not enable a production cohort. Older clients receive `client_update_required`, and explicit editor capability admission keeps unsupported/cloud paths closed. Independent source timing, ordinary media properties and section membership are persisted rather than flattened into a rendered-video workaround.

Before draft publication: test the corrective changes, obtain the feature's tracking issue, bind honest evidence to the reviewed final head, and run preship. Keep the PR draft while acceptance or review is incomplete. Required checks, source/model alignment and full acceptance must be satisfied before merge. No push, PR, merge, deployment or rollout was performed.

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
  -only-testing:KriaUITests/NativeEditorInspectorUITests/testOrdinaryFootageCanDuplicateBeforeCreatingSplitScreen \
  -only-testing:KriaUITests/SpeakerFrameDirectManipulationUITests/testRepeatedSpeakerPanelsSelectAndDragTheChosenOccurrence \
  test CODE_SIGNING_ALLOWED=NO
```
