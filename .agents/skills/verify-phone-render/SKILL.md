---
name: verify-phone-render
description: Model-check the iPhone render lifecycle (TLA+) behind "Kria couldn't prepare this edit on your iPhone", "Couldn't refresh this edit", "needs your attention" and "couldn't render". Use before changing DeviceRenderSessions, DeviceRenderCoordinator, NativeEditorSession save/preview/refresh, DeviceRenderPanel, or routes/services device_render.py.
---

# Verify phone render

Run `make verify-phone-render` (TLC v1.8.0, under a minute). Exit 0 pass, 1 fail, 3 pass with drift.

- **drift (exit 3):** a covered function in `formal/ios_edit_flow/coverage.json` changed. Re-read that code, update `PhoneRender.tla` if the behavior changed, then `python3 formal/ios_edit_flow/verify.py --resync`.
- **model FAIL:** a safety rule broke (stale publish, published flipping to attention, synced without publish, stale request reinstalled). The change is wrong; read the trace in `.dev/formal-ios-edit-flow/`.
- **finding no longer breaks:** you fixed one. Move the rule from `FINDINGS` to `PASSING` in `verify.py` and its line from `PhoneRenderFindings.cfg` to `PhoneRender.cfg`. Never weaken a rule to make it pass.

Findings and prod evidence: `formal/ios_edit_flow/findings.md`. Statecharts: `formal/ios_edit_flow/statechart/`. To extend the model to new code, use `/maintain-formal-verification-skill`.
