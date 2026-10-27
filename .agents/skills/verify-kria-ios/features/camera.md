# Camera and recording recovery

Source under `src/apps/ios/`: `Kria/Features/TeleprompterCamera.swift`,
`Kria/Features/TeleprompterRecordingFlow.swift`,
`Tests/KriaUITests/TeleprompterUITests.swift` and
`Tests/KriaHardwareUITests/TeleprompterHardwareUITests.swift`.

Start a Talking edit, open Record with script, enter a script and open the camera.
Run `drive camera`. Five simulator tests cover saved script, camera-unavailable
recovery, editable window geometry, accessibility and a previous take recoverable
after failed intake.

The opt-in `testPhysicalCameraTakeInterruptionAndRecovery` needs a paired,
unlocked iPhone, camera/microphone permission and
`KRIA_TELEPROMPTER_HARDWARE_TEST=1`. It records real footage. Use the device runner
documented in `docs/runbooks/ios-development.md` and inspect its movie.
A simulator skip or camera-error fixture cannot prove hardware capture.
