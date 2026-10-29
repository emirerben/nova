# Native speed Reset/close/Undo setup validation

Date: 2026-10-29. Base: `dc7af6621a053642af5dfdfb02c360be12520d09`.
Scope: `NativeSpeedUITests.swift` only; no lasting product changes.

## Setup correction

Hosted PR #1933 creation job `123731745348` reported `3.90x` and
`00:01.5` after `adjust(toNormalizedSliderPosition: 1)`. Its only failure
was the Closing test's exact `4.00x` setup assertion. Reset and the later
exact restoration assertions passed. This establishes that the requested
normalized slider endpoint need not produce exactly 4.00x in this driver;
it does not establish an underlying SDK cause.

The Closing test now unwraps the actual rate and duration, verifies the `x`
unit, a parsed rate greater than 1 and at most 4, and the existing exact
`00:01.5` duration. Reset must still show `1.00x` / `00:06.0`. After closing,
one Undo must restore both captured values exactly. The separate endpoint
test, including its strict 0.25x, 4.00x, Reset and Undo assertions, is
byte-identical to the base. This changes the setup calibration requirement,
not the Reset/Undo contract.

Test source SHA256:
`af5743262744771ebfa5a723f08abb78924aba652745ff481c6b6dae04691751`.

## Actual native evidence

Artifacts are retained under
`/Users/emirerben/Documents/Codex/2026-10-29/task-8/evidence/`.
Each prefix below has a `.log` and `.xcresult` bundle. Results were checked
from raw `xcresulttool get object --legacy` ActionTestMetadata, not from
the printed proof marker (which also prints after assertion failures).

| Prefix | Product source | Result |
| --- | --- | --- |
| `speed-setup-positive` | Unmodified base | Both speed tests, three executions each: 6 Success |
| `speed-setup-negative` | Inspector `onDisappear` transaction end removed | Closing: 1 Success; this control did not isolate a necessary hook |
| `speed-setup-restored` | Both product files restored to base | Closing: 1 Success; restored product rebuilt and installed |
| `speed-setup-transaction-negative` | Both slider editing-end and inspector disappearance transaction ends removed | Closing: 1 Failure, with three intended Undo assertions failing |

The collective control failed with xcodebuild exit 65. Undo was disabled
(line 84); the reopened inspector remained at `1.00x` instead of captured
`4.00x` (line 88), and `00:06.0` instead of captured `00:01.5` (line 89).
Build, launch, slider setup and Reset succeeded. This is a regression
control for the transaction/Undo contract, not a build or setup failure.

## Why the hook-only control passed

`NativeEditorSlider.editingChanged(false)` normally ends the drag's
transaction. `NativeEditorSession.transactDocument` immediately appends an
Undo baseline when no transaction is open. Thus a completed slider drag
commits the chosen speed, and Reset gets its own Undo entry before the
inspector disappears. The hook-only green result is consistent with this
source path. The main editor also has `finishPanelEditing()` transaction
cleanup for other panel/exit paths; the inspector's Done directly dismisses
its sheet.

Suppressing both transaction ends keeps the speed change and Reset in one
open transaction. Returning to the original 1x state leaves no usable
Undo entry. The Closing test detects that observable failure. These runs
do not prove the inspector disappearance hook is independently necessary
or exercise an interrupted slider gesture that fails to deliver its end
callback. No hook-specific protection claim is made.

## Restoration and reproduction

Both temporary product edits were restored byte-for-byte from the base:

- `NativeEditorView.swift`: SHA256 `3274a90e6aeac8d1cd8c32f6cd306caa74a4b2f992b435993b7f0e7c79940941`
- `NativeEditorInspectors.swift`: SHA256 `7ce61e8a3c7eb87400ed205eef6c507ef99b4f727da001150203c42640b30d04`

Command shape, from the isolated worktree:

```sh
xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria \
  -configuration Debug \
  -destination 'platform=iOS Simulator,id=F0614893-2CB6-447A-A402-322C2F8CB49F' \
  -derivedDataPath /tmp/unattended1892-build -skipPackagePluginValidation \
  -parallel-testing-enabled NO -only-testing:KriaUITests/NativeSpeedUITests \
  -test-iterations 3 -resultBundlePath <artifact-prefix>.xcresult \
  CODE_SIGNING_ALLOWED=NO test > <artifact-prefix>.log 2>&1
```

Controls select only
`KriaUITests/NativeSpeedUITests/testClosingSpeedInspectorCommitsResetForSingleUndo`
and omit `-test-iterations`. Device: the task-owned
`Unattended PR1892 verification`, iOS 26.5. No other device was used.

These are local simulator results. Exact-head hosted CI remains required;
no failed check is waived. The original rare corner-resize failure's cause
remains unproved and is outside this correction.
