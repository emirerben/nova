# Native editor

## Sub-features

Caption editing, document dirty state and Save availability. Source is
`Kria/Features/NativeEditorSession.swift` and
`Tests/KriaUITests/NativeEditorInspectorUITests.swift` under `src/apps/ios/`.

## How to get to it (user POV)

Open a ready edit, select a caption, change the text and approve the line.

## Driving it with XCUITest

Run `drive editor`. The helper selects
`NativeEditorInspectorUITests/testApprovingACaptionLineCommitsItAndEnablesSaveOnceThePanelCloses`.
The new line must commit and Save must become available after the panel closes.
Also run `NativeEditorSessionTests/testReadyIndexAbsentFromFreshPoolPreservesDocumentAndPlacement`
when investigating imported clips or source-pool mismatches.

## Gotchas

The selected UI check stops at Save availability. It does not prove Save,
reopen or final export. Those require their original recipe and actual media.
