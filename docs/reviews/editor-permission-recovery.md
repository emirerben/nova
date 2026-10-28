# Editor permission recovery

## Request and scope

A creator reported that Range and timeline clip resizing stayed unavailable until
an offline Save eventually succeeded. A later Save showed a text-style rejection.
Related recovery work is tracked in KRI-821. This report does not establish that
KRI-821's original Troya incident and this report have the same cause.

The local acceptance case is that a successful authoritative refresh updates the
current edit's permissions without requiring a Save, discarding unsaved text, or
letting Undo restore stale permissions.

## Confirmed mechanism

`NativeEditorSession.runPromptRevisionSync` reads updated permissions, then returns
when the rendered generation has not changed and local edits remain. It does not
install those permissions. Range and timeline edge resizing read the old values.
A subsequent Save can refresh the full document and make the controls work.

The synthesized native test starts with timeline permission disabled, stages a
text style change, fails a refresh offline, then answers with timeline permission
enabled for the same generation. Before the fix, its exact output included:

```text
XCTAssertNil failed: "Clip edits are locked for this render."
XCTAssertTrue failed
XCTAssertNil failed: "Clip edits are locked for this render." - Undo must not restore stale permissions
Executed 1 test, with 3 failures (0 unexpected)
```

After the fix, `ChatDraftStagingTests/testRecoveredPermissionsUnlockRangeWithoutSavingLocalText`
passes. The test checks Range availability, retained unsaved text, no commit, and
Undo. The focused run also passed all 74 chat-draft tests, 11 save-error tests,
and `NativeSourceWindowUITests/testRangeSheetSlidesAndResizesTheWindow`.

The data shape remains `[String: EditorCapability]`. Permissions are server
metadata. Refresh updates that metadata in the current document, clean baseline,
and undo and gesture history. It leaves authored lanes untouched. A larger move
of permissions outside `EditorDocument` was rejected for this bounded repair.

## Separate save rejection

`unsupported_phone_edit` covers several compiler failures. The app always calls
it a text-style or layout failure. The copy now describes an edit-preparation
failure without attributing it to text. The raw server reason remains private.

The reported video's saved text and timeline passed an in-memory replay of the
real commit validator against production settings. That replay used a detached
copy under a read-only database transaction. It did not save, queue a render, or
change the user's video. It does not prove that the unsaved request will pass.

The exact unsaved request and app build remain unavailable. iPhone Mirroring
reported that the phone was in use. The creator was asked for the last change and
TestFlight build. The compiler cause of that unsaved Save remains unresolved.
There is no claim of full request fulfillment, physical-device verification, or
deployment.

## Reproduction commands

From `src/apps/ios`, generate the project with `make ios-generate` from the repo
root, then run this command with an available simulator identifier:

```bash
xcodebuild -project Kria.xcodeproj -scheme Kria -skipPackagePluginValidation \
  -derivedDataPath .derived-data -destination 'platform=iOS Simulator,id=SIMULATOR_ID' \
  -parallel-testing-enabled NO CODE_SIGNING_ALLOWED=NO \
  -only-testing:KriaTests/ChatDraftStagingTests \
  -only-testing:KriaTests/EditorSaveErrorTests \
  -only-testing:KriaTests/EditorSaveCodeCatalogTests \
  -only-testing:KriaUITests/NativeSourceWindowUITests test
```

The before and after result bundles are retained locally as
`/private/tmp/dryer-permissions-before.xcresult` and
`/private/tmp/dryer-editor-after.xcresult`. The summary above is the durable,
redacted record. Private production snapshots are outside the repository.
