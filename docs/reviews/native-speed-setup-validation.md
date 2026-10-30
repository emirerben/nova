# Closing speed Reset/Undo setup repair

2026-10-30. Product base: `dc7af6621a053642af5dfdfb02c360be12520d09`.
Final direct-capture test source SHA256:
`029faa7a2a3a72fed0916587ffc618e52e78a8192be6b7f2cba8c98948c9156d`.
Scope: test setup and evidence. No product changes persist.

## Final contract

Closing requests the SDK slider's interior normalized position0.5, then
unwraps its actual rate and duration immediately after the gesture completes.
It requires the x unit, a parsed rate greater than1 and at most4, and duration
equal to six source seconds divided by that rate, formatted to one decimal.
Reset remains exactly1.00x /00:06.0. After closing, one Undo must restore both
captured values exactly. There is no repeated gesture, polling guard or
post-Undo adjustment of expected values.

A rate such as2.35x /2.6s exercises this contract: Reset changes both values
and Undo must restore both. Closing does not require calibration to the maximum.
The separate endpoint test retains exact minimum0.25x, maximum4x, Reset and
Undo checks. Its portable held-drag helper is unchanged from `6dcd31524`;
see `native-speed-endpoint-validation.md` for its independent causal proof.

## Why the additional settling guard was removed

Hosted setup previously returned3.90x /1.5s and3.70x /1.6s after requesting
normalized1. Fixed4x or1.5s Closing prerequisites therefore tested calibration
unrelated to Reset/Undo. The initial interior repair added three consecutive
identical accessibility samples within five seconds. Fresh hosted PR1931
creation then failed only that guard: slow accessibility reads permitted only
two sampling polls before timeout, while value consistency and Reset/Undo
assertions subsequently passed. The parent retained its log as
`evidence/pr1931-733cc-creation.log`.

The final repair removes this incidental sampling requirement. Direct capture
retains the observable rate/duration relationship and exact Reset/Undo proof;
it does not extend a timeout or wait for an expected rate.

## Final-source validation

Artifacts are under `/Users/emirerben/Documents/Codex/2026-10-29/task-8/evidence/`.
Raw ActionTestMetadata confirms these final-source results:

| Artifact prefix | Actual result |
| --- | --- |
| `speed-setup-direct-capture-pairs` |6/6 Success: three Closing and three endpoint executions; exit0|
| `speed-setup-direct-capture-negative` |Expected Failure, exit65; Undo disabled and exact restoration assertions fail|
| `speed-setup-direct-capture-restored` |1/1 Closing Success after byte-exact product restoration; exit0|

The collective control temporarily removed the slider editing-end transaction
commit and the inspector disappearance commit. Setup and Reset passed, then
Undo was disabled (line85); rate and duration remained1.00x /6s instead of
captured2.35x /2.6s (lines89–90). This proves the collective observable contract,
not either hook's individual necessity. Both product files were restored
before the final passing execution.

All three variants emitted the v4 direct-capture marker and had matching built
and installed test executable SHA256
`b6852c00909ff88aa5de0d46ac1655689c8a4d24e758df8418e330343448b1c1`.
Source hash remained029faa7a throughout. The negative installed product dylib
was `bca710a2386accf1293a3f9fabd4d27a7c5b917369918679ce5aa14249c76311`;
the restored dylib was
`6e0425dc6fbf503f8f1e9d52ac9f02eeb0e3176ec5531a45bed57613d94da5b5`.
Per-prefix metadata and identity JSON records accompany the bundles/logs.

Restored source hashes:
- `NativeEditorInspectors.swift`: `7ce61e8a3c7eb87400ed205eef6c507ef99b4f727da001150203c42640b30d04`
- `NativeEditorView.swift`: `3274a90e6aeac8d1cd8c32f6cd306caa74a4b2f992b435993b7f0e7c79940941`

The endpoint helper had14/14 successes plus its target-specific red control
on prior sourceff6408e4, documented in the endpoint record. Its additional three
successes above are on final combined source029faa7a; the helper is unchanged.

## Historical evidence, not final-source acceptance

The earlier source `9c6d03e98a1bb0b77d2839290c63726ae3d84326f25831d4dd4d87e8fd18c7c8`
passed ten Closing cases with its sampling guard. In the same paired set the
original endpoint SDK driver passed9/10; that failure was preserved. Prefixes:
`speed-setup-interior-positive` (3pairs) and `speed-setup-interior-expanded`
(7pairs). These results do not prove the final direct-capture source.

The earlier collective negative control removed both slider editing-end and
inspector disappearance transaction ends. Undo became disabled and failed to
restore the captured speed/duration; restoring product bytes passed. This
proved the collective boundary contract, not the necessity of either hook
alone. Removing only the disappearance hook had passed because a completed
slider drag can already commit its transaction. Earlier artifacts and full
historical record remain in `speed-setup-settled-validation-historical.md`.

## Source and runtime limits

Only owned simulator `F0614893-2CB6-447A-A402-322C2F8CB49F` (iOS26.5) is used,
with Kria Debug, `/tmp/unattended-speed-rate-build`, parallel testing disabled,
and CODE_SIGNING_ALLOWED=NO. The task-owned ephemeral test harness is refreshed
between variants; product app data and other simulators are not manually removed.
Runtime markers identify source, not success: they can print after assertion
failures. Local results do not replace required hosted CI or waive failed checks.
The original rare corner-resize cause remains unproved and outside this repair.
