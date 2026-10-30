# Native speed endpoint gesture validation

2026-10-30. Based on Closing repair `3ec4f08469f0c1dc3050f6b550957851f21d6a0e`.
Candidate test source SHA256:
`ff6408e4545b8ce0f995532c6dded893f92181fb5a413846f07555deb9921216`.

## Contract and change

The endpoint test replaces its two SDK normalized-position actions with a
single held drag for each endpoint. Maximum goes to the application's right
edge minus one point, inside the application. Minimum, start geometry,
velocity20, initial hold0.25s and final hold0.3s match the causal experiment.
The helper requires an enabled, hittable slider fully inside the application.
There are no repeated gestures until a value passes.

All original immediate assertions remain: minimum0.25x /24.0s, maximum4.00x
/1.5s, Reset1.00x /6.0s, and one Undo restoring exactly4.00x /1.5s.
The separate Closing test and its interior setup are unchanged. No product
code changes persist. The endpoint test adds no settling assertions or polling.

## Evidence and scope of inference

Artifacts are under `/Users/emirerben/Documents/Codex/2026-10-29/task-8/evidence/`.
The earlier normalized SDK driver missed exact maximum in one of ten cases;
two unconditional SDK maximum actions also failed. These failures are retained.

A temporary passive event/geometry probe observed correctly delivered screen
coordinates on the same stationary UISlider, with no application-setter input
rejection. At touch x354.667 the native value was0.88 (application3.55x),
while the visible thumb center was315.38. The track remained338pt wide.
A fit of639 moved-event samples between native0.1 and0.8 inferred a338.116pt
tracking span and maximum near x396.152 (R²0.999670). This empirical mapping
motivated a saturating in-window target; it does not establish UIKit internals,
a liquid-lens cause, or a production defect. See
`speed-setup-active-geometry-trace.log` and `speed-setup-active-geometry-fit.json`.
All instrumentation was removed before acceptance runs.

The fixed397 diagnostic passed every exact value assertion in three executions,
but one execution failed added settling guards. It is recorded as2Success /
1Failure, not three passing tests. The final endpoint candidate omits those
unrelated guards and retains the original immediate checks.

## Completed causal checks

| Artifact prefix | Result |
| --- | --- |
| `speed-setup-window-edge-positive` |3/3 Success, exit0; target401 on the402pt application|
| `speed-setup-window-edge-control-recovered` |Expected Failure, exit65; only target changed to354.5, maximum and Undo3.55x /1.7s fail exact4x /1.5s|
| `speed-setup-window-edge-restored` |1/1 Success, exit0; portable edge target restored|

The original `speed-setup-window-edge-control` was interrupted by execution
transport loss after its maximum assertions failed; its result bundle is
incomplete and it is excluded from completed proof. The identical control was
re-executed under the recovered prefix. The first ten-case batch
`speed-setup-window-edge-ten` was interrupted before any case completed and is
also excluded. Its unchanged replacement uses `speed-setup-window-edge-ten-recovered`.

Fresh harness installation and runtime gesture markers identify each variant.
The initial positive and restored built/installed test executable SHA256 was
`bf055ebcb56345b15e6324065df3658fdecac1d15a7286760aa82c55aedc8eb8`.
The completed control executable was
`fe1040c7fb7ddfa92093ba30bb1d43ab13f1f8a684e4155a46c4193e8d0163a3`.
Matching source/built/installed hashes are retained in each `-identity.json`.
Only the task-owned ephemeral test harness was removed. Product app data and
other simulators were not manually removed.

## Reliability batch

The predefined `speed-setup-window-edge-ten-recovered` batch completed with
ten ActionTestMetadata entries, all Success, exit0. All ten used unchanged
source and the matching built/installed executable hash above. Together with
the initial three and restored confirmation, the final edge candidate has
14/14 completed endpoint successes. The deliberately shortened-target control
remains a separate expected failure. Independent review and hosted validation
are separate from these local results.

All local runs use simulator `F0614893-2CB6-447A-A402-322C2F8CB49F` (iOS26.5),
Kria Debug, `/tmp/unattended-speed-rate-build`, parallel testing disabled,
CODE_SIGNING_ALLOWED=NO, and the single endpoint method selected explicitly.
This evidence is limited to that local simulator configuration. Required
hosted CI is separate; no queue was reset and no failed check is waived.
