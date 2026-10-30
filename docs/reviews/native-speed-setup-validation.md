# Closing speed Reset/Undo setup repair

2026-10-30. Product base: `dc7af6621a053642af5dfdfb02c360be12520d09`.
Scope: Closing UI test setup and this record. No product changes persist.
Restored-product confirmation passed; this record is not a whole-suite
green claim. The unchanged endpoint test has a separately observed flake.

## Change and preserved contract

Closing now requests the SDK slider's interior normalized position0.5,
then captures three consecutive identical rate/duration pairs within five
seconds. It requires the `x` unit and a parsed rate greater than1 and at
most4. The displayed duration must equal six source seconds divided by
that rate, formatted to the UI's one decimal place. The UI slider steps by
0.05 and reports two decimals, so this comparison uses its actual value.

Reset must still show exactly1.00x /00:06.0. After closing, one Undo must
restore both captured values exactly. Post-Undo assertions are immediate;
expected values never change. There is no repeated gesture until success,
no requirement that the sampled rate equal the requested normalized value,
and no custom coordinate driver in this repair.

The first test, `testDeviceClipSpeedAdjustsResetsAndUndoesThroughTheInspector`,
is byte-identical to the base, including its SDK driver and exact0.25x,
4.00x, Reset and Undo checks. A selected interior rate such as2.35x /2.6s
still exercises Closing's contract: Reset changes both values and Undo
must restore both.

Test source SHA256:
`9c6d03e98a1bb0b77d2839290c63726ae3d84326f25831d4dd4d87e8fd18c7c8`.

## Actual validation

Artifacts are retained under
`/Users/emirerben/Documents/Codex/2026-10-29/task-8/evidence/`.
Each prefix below has `.log` and `.xcresult`. Counts come from raw
`xcresulttool get object --legacy` ActionTestMetadata.

| Prefix | Closing | Unchanged endpoint | Result |
| --- | --- | --- | --- |
| `speed-setup-interior-positive` |3/3 Success|3/3 Success|exit0|
| `speed-setup-interior-expanded` |7/7 Success|6/7 Success|exit65; endpoint3.95x vs4x at max and Undo|
| `speed-setup-interior-negative` |1 expected Failure|not run|exit65; intended Undo checks fail|
| `speed-setup-interior-restored` |1/1 Success|not run|exit0|

Thus Closing passed all ten predefined positive executions. The full
paired sample was19/20: the unchanged endpoint method's ninth cumulative
execution missed4.00x and later restored the same3.95x. This failure was
preserved, not retried away or waived. An endpoint driver repair is separate
work; this change does not claim all required CI is ready.

The collective negative control temporarily removed both
`NativeEditorSlider.editingChanged(false)`'s transaction end and the
inspector's disappearance transaction end. Setup and Reset passed, then
Undo was disabled (line86). Reopening left1.00x /6s instead of captured
2.35x /2.6s (lines90–91). The v3 runtime marker confirmed the actual interior
setup. The negative installed product debug dylib hashed
`411b39a96abfda88b469685daa9c2353460548a65f801738d3870df7b603e852`.

This verifies the observable collective transaction/Undo contract. It does
not prove the disappearance hook alone is necessary: a completed slider
drag normally ends its transaction, so Reset can already have an Undo entry.
The earlier hook-only removal control passed and remains documented in the
investigation artifacts.

## Source and runtime identity

The current harness was freshly installed after removing only the owned
simulator's ephemeral UI-test runner. No product app or its data was manually
removed. Initial paired validation's built and installed test executables
both hashed `be8b1cff3fe38657c3d3789a1dba732683d41ecd1b80206c0d482ba5e4f81931`;
see `speed-setup-interior-positive-identity.json`.

The runtime marker `speed-reset-close-undo-v3 requested_normalized=0.5`
records chosen and calculated values. It establishes executing-source
provenance, not success: it also prints after assertion failures. Earlier
investigation runs occasionally emitted old assertion text despite updated
on-disk binaries; those artifacts were not promoted into final proof.

All temporary product edits were restored byte-for-byte:

- `NativeEditorView.swift`: `3274a90e6aeac8d1cd8c32f6cd306caa74a4b2f992b435993b7f0e7c79940941`
- `NativeEditorInspectors.swift`: `7ce61e8a3c7eb87400ed205eef6c507ef99b4f727da001150203c42640b30d04`
- `NativeEditorRetiming.swift`: `7e2e7dd0f3574c23d1df37004ee7d53ee454e53a283464e6de3df03f9fb9a585`
- `NativeFootagePanel.swift`: unchanged from the product base.

## Earlier diagnostic boundaries

Hosted setup failures included3.90x /1.5s and3.70x /1.6s after requesting1.
A fixed4x or fixed1.5s Closing precondition therefore tested calibration
unrelated to Reset/Undo. Custom geometry, slower gestures and a constant
Reset-row product variant did not reliably fix the separate endpoint case;
none of those changes are included here.

A buffered raw-input trace captured an endpoint miss whose largest input
was3.9000000000000004; the setter stored every input and never received4 or
an above-4 value. UIKit geometry and passive event observation confirmed
delivered screen coordinates on the same slider instance, with a native
value/thumb lag and an editing false→true transition. These diagnostics
do not establish an SDK root cause or justify a shipping product change.
All probes were removed. Full prior results and limitations are retained in
`speed-setup-all-result-metadata.json` and `speed-setup-investigation-notes.md`.

## Reproduction and limits

From the isolated worktree, use project `src/apps/ios/Kria.xcodeproj`, scheme
`Kria`, Debug, `CODE_SIGNING_ALLOWED=NO`, `-parallel-testing-enabled NO`,
`-skipPackagePluginValidation`, and DerivedData
`/tmp/unattended-speed-rate-build`. Only simulator
`F0614893-2CB6-447A-A402-322C2F8CB49F` (iOS26.5) was used.
Select `KriaUITests/NativeSpeedUITests` with `-test-iterations 3`, then7
for the predefined paired sample. Controls select Closing only and run once.
Pass a unique `-resultBundlePath` and retain stdout in the matching `.log`.

These are local simulator results. Exact-head hosted CI remains required;
no failed check is waived. The original rare corner-resize cause remains
unproved and outside this repair.
