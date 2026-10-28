# Native inspector undo and UI verification

Closing the native inspector must finish its pending edit before the creator
uses Undo. The new regression reproduces a real failure on unchanged app code:
a chosen 3.30x speed and 1.8-second duration become 1x and 6 seconds after Reset;
Done and one Undo leave them at 1x and 6 seconds instead of restoring the choice.
All setup, changed-speed, Reset, dismissal, and enabled-Undo assertions pass.

## Finish the transaction when its inspector leaves

The retained in-memory trace shows Undo running with an active transaction.
Call stacks identify `NativeEditorSlider.editingChanged` and
`UISliderAccessibility`; the last edit callback opens a transaction that stays
open through Reset and dismissal. The exact platform callback cause is not
claimed. The recording confirms Reset changed the visible control.

`NativeEditorView.finishPanelEditing` already finishes transactions for the
editor's other panels. `NativeEditorInspectorView` only dismissed its sheet.
It now calls `session.endTransaction()` when it disappears. An already-closed
transaction remains a no-op. No slider value, duration, renderer, capability,
or persistence policy changes.

The new real-control UI regression chooses a faster speed with the SDK slider
API and records the actual visible rate and duration. After Reset, Done, and
one Undo, both must equal that previous choice. Capturing the prior state is
the Undo oracle; it does not replace exact endpoint coverage. The original
0.25x/4.00x, 24s/1.5s, Reset, and Undo test remains unchanged.

## Check the pinch warning during the unsafe phase

CI 40668700336 failed the old pinch assertion in three automatic attempts.
The recording shows the orange warning during enlargement at 55.5 seconds,
then a cleared guide after shrinking at 74 seconds. A diagnostic on unchanged
main `53dc75b3b` reports unsafe edges after enlargement and none after shrinking.
The test previously rejected that final safe sample.

The corrected test requires an available, unsafe sample after enlargement
and an empty sample after shrinking. It retains live sample counts, preview,
animations, and cancel checks. Initial Text readiness now requires the prepared
video and an enabled, hittable tool with stable finite bounds within the same
12-second budget. The exact cause of the occasional early Text miss is unknown.

## Preserve failed experiments

Physical slider-driver replacements did not qualify. Some reached the endpoints;
others stopped short or left Undo at an older rate. They were removed. The
original endpoint test and SDK driver remain intact. No tolerance, action retry,
or timeout increase was added. These experiments do not establish a production
endpoint defect or fix.

A local integration run printed an old assertion absent from its source despite
matching built and installed binaries. That result is unqualified and retained.
A fresh build directory and simulator then confirmed the changed test bodies
with runtime markers. Gameday and pinch passed, but speed failed after Undo.
Markers establish execution only; result status must establish success.

The first temporary NSLog diagnostic passed but did not recover transaction
logs. The later memory-only trace reproduced the open transaction, and a
call-site trace identified its owner. Diagnostic app fields, logging, and
environment flags were removed before the regression and final verification.
All failed results and recordings remain in the task artifacts.

The red regression is `inspector-reset-undo-red.xcresult`: one test failed
after all setup and Reset checks passed. The identical regression passed after
the one-line close hook. Before the later animation integration, `inspector-close-green.xcresult` passed
248 tests:
244 existing editor-session checks and four unique UI journeys, zero failures
and zero skips. Its summary SHA-256 is
`5c73fe02ba3fe6a92e13c24ea42423b849e16217e527218c5c2281b17365716c`.
The red and green test-source hashes match. Fresh original-runtime CI remains
mandatory.
Local iPhone 17 uses iOS 26.5; CI uses iPhone 17 Pro/iOS 26.2.

The earlier CI artifact is
[12811698140](https://github.com/emirerben/nova/actions/runs/40668700336/artifacts/12811698140).
Its unit suite had 2086 executions, 26 skips, and zero failures. Its selected UI
suite had 88 unique tests: 87 passed, and one pinch test failed after retries.
The earlier speed test recovered on retry. Successful Gameday creation evidence
is retained separately in artifact 12807399359. Two UIKit hierarchy warnings
remain recorded; this change does not claim to fix them.

The one added UI case is registered in the editor group with a 30-second estimate.
The iOS script checks passed 117 tests. Full required CI, current-base review,
and automatic client publication still gate delivery. This does not clear the
Cunda candidate, its corrected published text, the original 28.804-second timing
request, quiet-audio fidelity, or remaining original-media replays.

The native verification used main `69c3b701d` plus this working patch.
`NativeEditorView.swift` SHA-256:
`771890ed1719e54323dc78051ea1da21a9c4593e98828c8ab564c56c0c577b97`.
`NativeSpeedUITests.swift` SHA-256:
`e8003529fe8b182405791f96806fb0d45076ff6b514bf78bd5351b3656583612`.
The safe report records a local fix. No physical-device or installed TestFlight
verification of this new inspector hook is claimed.

## Latest-main integration

Merged main `06863782d`, including the six new native text animations, into
this task branch at `66b3188b08c094e49c3854673f23c6dc145deef4`. The regenerated app then passed
the same 244 session checks and four UI journeys again: 248 passed, zero failed,
zero skipped. Exact selected UI coverage was verified with the repository's
`ui_tests.verify_results` function. The summary SHA-256 is
`7f5d70f6baffd7f3e4a02c346d5fde9af3a49a5f613feb2f4fd9bca1a7ecda57`.

The latest native result is `latest-main-inspector-close.xcresult`. The original
endpoint method and new Undo regression are unchanged from the red/green proof.
The latest Gameday export's inspected title and fifth-source frames are byte
identical to the earlier inspected frames. This still requires fresh exact-head
CI and actual candidate publication.
