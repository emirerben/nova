# Creation chat

## Sub-features

Format selection, footage attachments, direction, approval and render readiness.
Source is `Kria/Features/ChatWorkspaceView.swift` and
`Tests/KriaUITests/CreationUITests.swift` under `src/apps/ios/`.

## How to get to it (user POV)

Open Kria, start a chat, attach footage, request an edit and approve the proposal.

## Driving it with XCUITest

Run the helper's `drive chat`. It runs
`CreationUITests/testCreationWithAttachedFootageReachesConfirmationAndReadyForBothRuntimes`.
The test must observe attached footage, confirmation and the ready result for
both runtime versions. Read its result bundle and screenshots.

## Gotchas

The fixture account replaces API responses and model calls. It cannot prove
that a real provider follows the request. Repeat the original prompt on the
named release candidate and inspect its exported picture, text and sound.
