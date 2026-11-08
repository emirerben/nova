---
name: verify-phone-render
description: Model-check the iPhone render lifecycle (TLA+) behind "Kria couldn't prepare this edit on your iPhone", "Couldn't refresh this edit", "needs your attention" and "couldn't render". Use before changing DeviceRenderSessions, DeviceRenderCoordinator, NativeEditorSession save/preview/refresh, DeviceRenderPanel, or routes/services device_render.py.
---

# Verify phone render

Run `make verify-phone-render` (TLC v1.8.0, about 3 minutes). Add `--full` to `python3 formal/ios_edit_flow/verify.py` for the heavier event combinations (Stop with declines and lost capabilities, two saves with every condition, builds with Stop, Save with a decline, more liveness pairs). Exit 0 pass, 1 fail, 3 pass with drift.

What the model covers since round 3: builds as levels (this iPhone, an older device, an update that may still be too old), protocol-floor gaps (the GET's 426 names its request) versus schema gaps only the phone can see, the gallery editor opening the edit's own chat, a capability lost mid-render, an operator lowering a floor (`renders_locally`). Rules: the nine safety rules in `SAFETY` (incl. UpdateTold, NoRepinLoop) and AttentionHasWayOut, PreviewConverges, ClientServerAgree, CapableRenders.

Two models live in `formal/ios_edit_flow/`:
- `PhoneRender.tla` is the lifecycle abstraction last bound by `coverage.json`. Every rule must hold in its runs (`MODEL_RUNS`, plus `FULL_RUNS` with `--full`). A pass with drift proves that abstraction, not correspondence with all current source. Durable intent, owner/command awaits and newer automatic-read behavior require their maintained source mapping; never clear those drifts merely because this model passes.
- `PhoneRenderBefore.tla` is the code before the killer-screens fixes (origin/main ff27b0a65). `verify.py` checks it still breaks each rule the fixes made pass (`BEFORE_BREAKS`). Do not edit it to match new code; it is history. Rule texts are shared, so a rule change goes into both modules with the same text (helpers such as `Screen`, `Stuck`, `Told` are per module).

Steps and what a failure means:
- **drift (exit 3):** a covered function in `coverage.json` changed. Re-read that code, update `PhoneRender.tla` if the behavior changed, then `python3 formal/ios_edit_flow/verify.py --resync`.
- **model FAIL:** a rule broke on the code model. The trace is in `.dev/formal-ios-edit-flow/model-<run>.log`. Either the change is wrong, or the model no longer matches the code. The model must match the code: never weaken a rule or remove behavior to make it pass.
- **residual:** `RESIDUAL` lists real findings today's code still breaks, each in the scenario scoped out of `MODEL_RUNS`; `verify.py` checks they still break. It is empty now. When a fix makes one hold, move it to the passing runs (widen them to the scenario), add teeth, rerun.
- **before:** the old module stopped breaking a rule. Usually a shared rule text changed in a way that hides the old bug; fix the rule, not the old model.
- **teeth:** every passing rule has at least one mutation (`TEETH`) that puts one piece of the old behavior back; TLC must catch it. Every new fix needs its own tooth: one mutation per fix, never bundled (bundles hid fixes with no tooth in round 2). If a fix's mutation is caught nowhere, either it is unnecessary (say why in findings.md) or the runs lack the scenario that needs it: add the scenario.

Findings, prod evidence and what changed: `formal/ios_edit_flow/findings.md`. Statecharts of the fixed flow: `formal/ios_edit_flow/statechart/` (`editor-device`, `creation-rendering`). To extend the model to new code, use `/maintain-formal-verification-skill`.

## Saved-render receipt companion

The default verifier also runs `SavedRenderReceipt.tla` through
`verify_saved_receipt.py`. Run that bounded check alone with
`python3 formal/ios_edit_flow/verify_saved_receipt.py` and the same verified
`TLA2TOOLS` / `JAVA` settings. It checks saved-generation export safety, no export
during unresolved/coalesced reads, and replacement-editor isolation after a
suspended Save. Three mutation modes (four runs, checking both replacement windows separately) must fail, and a reachability
control must demonstrate export of the replacement.

Mapping, bounds and exclusions are in `formal/ios_edit_flow/saved-receipt-correspondence.md`.
This companion does not certify all behavior in the covered native functions.
Its addition does not authorize globally resyncing `coverage.json`; unrelated
baseline drift and unmodeled function branches must stay explicit.

## Split persistence companion

For split sections, timeline serialization, canonical asset hydration, merge or
Save acknowledgement changes, also run these commands serially with the same
verified `TLA2TOOLS` and `JAVA` settings:

```sh
python3 formal/ios_edit_flow/verify_split_persistence.py --check-source
python3 formal/ios_edit_flow/verify_split_persistence.py
src/apps/api/.venv/bin/python formal/ios_edit_flow/verify_split_protocol.py
```

`SplitScreenPersistence.tla` checks admitted rows/definitions, explicit-empty
unstack, captured submission, edits across acknowledgement, one coupled merge
case and canonical/compatibility asset identities. It uses one section with two
or three panels, one acknowledged Save and at most two edits. Fourteen mutation
switches run as fifteen independently targeted controls; fourteen positive
reachability controls prevent vacuous refusal. The protocol control removes
only the concrete split-screen floor branch in an isolated function copy.

Read `split-persistence-correspondence.md` for actual source boundaries and
exclusions. Its separate source map detects edits to reviewed files without
advancing global lifecycle coverage. There is no automatic repin. On source
drift, review changed semantics and controls before updating that scoped map.
These checks do not prove numeric media fidelity, physical-device behavior,
database crash recovery or aggregate lifecycle liveness. A passing companion
does not clear a failing or drifted original gate.
