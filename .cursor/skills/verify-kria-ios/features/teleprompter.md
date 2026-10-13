# Talking capture and teleprompter

Creators write a script, adjust its reading window and use it while recording.
A camera failure must preserve the script so they can edit and reopen it.

## Sub-features

- `teleprompter-script`: typed script survives camera failure and reopening.
- `teleprompter-window`: background, dimensions and placement restore on reopen.
- `teleprompter-accessibility`: script controls remain reachable at large text size.
- `teleprompter-recovery`: an existing take remains recoverable after failed intake.

## How to get to it (user POV)

Open projects → New chat → Talking (`format-talking_to_camera`) → Attach footage
→ Teleprompter (`teleprompter-open`). Write a script, then open its camera. Edit
script returns to the text editor; close/reopen restores the draft.

## Driving it with XCUITest

Preconditions: Launch/Doctor and an empty run-owned simulator. The test uses
`-ui-testing-chat`, `KRIA_CHAT_CREATION_FLOW=v1`, and
`KRIA_TELEPROMPTER_CAMERA_UNAVAILABLE=1` at the existing camera boundary.

```sh
"$KRIA_VERIFY_CONTROL" drive --run "$KRIA_VERIFY_RUN" --feature teleprompter \
  TeleprompterUITests/testScriptIsSavedAndCameraFailureKeepsItEditable
```

- **Enter script.** Tap `teleprompter-open`; `teleprompter-script-editor` appears
  and `teleprompter-open-camera` is initially disabled. Type exactly:
  `Today I want to share a simple idea.` followed by a newline and
  `Take your time and enjoy the story.`
- **Camera failure.** Tap `teleprompter-open-camera`. The
  `teleprompter-camera-error` appears; `teleprompter-record` stays disabled. The
  test captures `Camera unavailable preserves script`.
- **Edit and reopen.** Tap `teleprompter-edit-script`, then
  `teleprompter-close` → `teleprompter-open`. The original script remains and the
  test captures `Script restored on reopen`.
- **Independent persistence.** The helper reads
  `Library/Application Support/KriaTeleprompter/*/*/draft.json` in that app's
  container and requires its `text` to equal the full two-line script. It copies
  the draft into `persistence/` before cleanup. Inspect it and both screenshots.
- **Other mapped paths.** Use a new run and the same `--feature teleprompter`
  with `TeleprompterUITests/testWindowBackgroundAndDimensionsSurviveReopening`,
  `TeleprompterUITests/testWindowHandlesDoNotPauseOrScrollTheScript`,
  `TeleprompterUITests/testScriptControlsAreReachableAtAccessibilitySize`, or
  `TeleprompterUITests/testPreviousTakeAndFailedIntakeKeepTheRecordingRecoverable`.

## Gotchas

- Use a fresh run for the exact-script proof. Reusing its draft can append text
  and invalidate the persistence assertion.
- Camera unavailability is deliberate. No physical recording, microphone
  quality, camera orientation or hardware export claim follows from this test.
- Reopening the sheet proves that user path, not an app relaunch or live server
  persistence. The independent draft-file assertion additionally proves local
  disk persistence.
- Recovery tests seed a take and reject intake at the API fixture boundary.
  A real recorded/imported take and upload need physical-device/backend proof.
