---
name: verify-phone-render
description: Model-check the iPhone render lifecycle (TLA+) behind "Kria couldn't prepare this edit on your iPhone", "Couldn't refresh this edit", "needs your attention" and "couldn't render". Use before changing DeviceRenderSessions, DeviceRenderCoordinator, NativeEditorSession save/preview/refresh, DeviceRenderPanel, or routes/services device_render.py.
---

# Verify phone render

Run `make verify-phone-render` (TLC v1.8.0, about 3 minutes). Add `--full` to `python3 formal/ios_edit_flow/verify.py` for the heavier event combinations (Stop with declines and lost capabilities, two saves with every condition, builds with Stop, Save with a decline, more liveness pairs). Exit 0 pass, 1 fail, 3 pass with drift.

What the model covers since round 3: builds as levels (this iPhone, an older device, an update that may still be too old), protocol-floor gaps (the GET's 426 names its request) versus schema gaps only the phone can see, the gallery editor opening the edit's own chat, a capability lost mid-render, an operator lowering a floor (`renders_locally`). Rules: the nine safety rules in `SAFETY` (incl. UpdateTold, NoRepinLoop) and AttentionHasWayOut, PreviewConverges, ClientServerAgree, CapableRenders.

Two models live in `formal/ios_edit_flow/`:
- `PhoneRender.tla` is the code on this branch. Every rule must hold in its runs (`MODEL_RUNS`, plus `FULL_RUNS` with `--full`).
- `PhoneRenderBefore.tla` is the code before the killer-screens fixes (origin/main ff27b0a65). `verify.py` checks it still breaks each rule the fixes made pass (`BEFORE_BREAKS`). Do not edit it to match new code; it is history. Rule texts are shared, so a rule change goes into both modules with the same text (helpers such as `Screen`, `Stuck`, `Told` are per module).

Steps and what a failure means:
- **drift (exit 3):** a covered function in `coverage.json` changed. Re-read that code, update `PhoneRender.tla` if the behavior changed, then `python3 formal/ios_edit_flow/verify.py --resync`.
- **model FAIL:** a rule broke on the code model. The trace is in `.dev/formal-ios-edit-flow/model-<run>.log`. Either the change is wrong, or the model no longer matches the code. The model must match the code: never weaken a rule or remove behavior to make it pass.
- **residual:** `RESIDUAL` lists real findings today's code still breaks, each in the scenario scoped out of `MODEL_RUNS`; `verify.py` checks they still break. It is empty now. When a fix makes one hold, move it to the passing runs (widen them to the scenario), add teeth, rerun.
- **before:** the old module stopped breaking a rule. Usually a shared rule text changed in a way that hides the old bug; fix the rule, not the old model.
- **teeth:** every passing rule has at least one mutation (`TEETH`) that puts one piece of the old behavior back; TLC must catch it. Every new fix needs its own tooth: one mutation per fix, never bundled (bundles hid fixes with no tooth in round 2). If a fix's mutation is caught nowhere, either it is unnecessary (say why in findings.md) or the runs lack the scenario that needs it: add the scenario.

Findings, prod evidence and what changed: `formal/ios_edit_flow/findings.md`. Statecharts of the fixed flow: `formal/ios_edit_flow/statechart/` (`editor-device`, `creation-rendering`). To extend the model to new code, use `/maintain-formal-verification-skill`.
