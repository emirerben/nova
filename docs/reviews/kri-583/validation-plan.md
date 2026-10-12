# KRI-583 validation plan (written before integration implementation)

Failures to exercise:
- Blank script cannot open camera; saved script persists through back/dismiss/reopen and is isolated by account/project.
- Denied/unavailable camera or microphone leaves the script intact and never creates a silent take.
- Countdown cancellation, backgrounding or dismissal cannot start capture later.
- Camera start acknowledgment starts script and elapsed clock; text pause/pace/drag/end never stops camera.
- Interrupted or corrupt candidate does not replace a previous reviewed take. Only explicit Use promotes a candidate; Previous discards only candidate.
- Metadata write failure cannot begin capture or attachment; finalized file stays available for retry.
- Capability failures or full clip slot retain take and cannot choose cloud fallback.
- Reopening an attaching take reconciles the existing recovery ID/server filename before enqueueing; successful enqueue is queued, not finished attachment.
- At large Dynamic Type and Reduce Motion controls remain reachable and manual next/previous is available.

Evidence: focused native playback/persistence tests; native compilation/unit gate; XCUITest script-entry, camera-unavailable and saved-draft recovery with screenshots. Physical camera proof must check synchronized audio/video, portrait/mirror framing, widget absence in saved pixels, interruption recovery and device-only editable intake. Simulator camera failure is expected, not proof of physical capture.

## Current evidence status

The integrated native run recorded 1,547 passing tests, 22 intentional skips and
zero failures; nine selected native UI tests and 93 offline orchestration checks
also passed. A separate signed QA run passed the physical camera take,
background interruption and review recovery, with saved movie tracks inspected.
Those checks used the offline Talking workspace and did not establish perceptual
spoken/clap sync, real-server Use take/editor/export, lock or audio-route recovery,
small/large-device reading comfort, or process termination/relaunch behavior.
The review fixture uses identical bundled footage, so its passing playback controls
do not prove selection between two different movies. Final native build and all
nine selected UI tests passed after the selected-take playback fix. The UI run
passed without retries after correcting its saved-font boundary precondition;
unit results are reused for the subsequent UI-test-only correction.
