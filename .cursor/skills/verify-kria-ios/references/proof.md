# First executed proof

Verified on 2026-10-13 from `codex/verify-kria-ios`, based on
`bfd3a8147a6d4c4fec3dea0e676a1a1eb515323f`. The native input fingerprint was
`30cc276aeb4e49eeef9692b6e56e987d7f725b39d78ac4aab1c90b3db38dfadd`.

## Commands and results

From the selected worktree root:

```sh
.cursor/skills/verify-kria-ios/control-kria-ios launch --run teleprompter-proof
.cursor/skills/verify-kria-ios/control-kria-ios doctor --run teleprompter-proof
.cursor/skills/verify-kria-ios/control-kria-ios drive --run teleprompter-proof \
  --feature teleprompter \
  TeleprompterUITests/testScriptIsSavedAndCameraFailureKeepsItEditable
.cursor/skills/verify-kria-ios/control-kria-ios cleanup --run teleprompter-proof
```

- Preparation: unsigned Debug build succeeded; the existing unit suite reported
  1691 tests, 22 skipped, zero failures.
- Doctor: healthy; state/ownership/receipt hashes unchanged by the check.
- Negative controls: stale fingerprint rejected; changing the run's simulator
  ID to another simulator made Doctor and cleanup refuse before touching it.
- UI: five selected tests passed, no retries/flaky tests. Selection retained the
  four required smoke journeys plus the teleprompter script journey.
- Persistence: the app's saved draft contained the exact two-line script typed
  through the UI. One matching draft was copied before teardown.
- Visual inspection: the camera-unavailable screenshot showed the script,
  disabled Record and Edit script; the reopened script screenshot showed both
  full sentences and the saved-on-this-iPhone message.
- Cleanup: the owned simulator was deleted. Every preexisting simulator retained
  its name and state; an unrelated concurrently-created simulator was untouched.
  The saved draft, three screenshots, UI/unit result bundles and command logs
  remained after cleanup.

## Retained artifacts

The execution worktree was `/private/tmp/nova-verify-kria-ios`; full artifacts are
under `test-results/verify-kria-ios/teleprompter-proof/` within that worktree:

- `proof.json`, `run.json`, and `doctor.log`.
- `negative-controls.json` and rejection logs.
- `cleanup-checks.json` and before/after simulator inventories.
- `attachments/manifest.json` and three exported PNG screenshots.
- `persistence/0-draft.json` with the exact typed script.
- `prepare-ui.35g9bd/unit.xcresult`, `ui.Xz3fUU/ui.xcresult`, and their logs.

Artifacts are git-ignored; copy them to a retained local location before removing
the task worktree. This tracked summary preserves the result if temporary
artifacts are later removed; it does not replace a future rerun on changed inputs.

## Limits

This proves the teleprompter script/camera-failure/sheet-reopen path and local
disk persistence on an iPhone 13 Pro simulator with iOS 26.5, Xcode 26.6.
The four smoke tests also exercise baseline creation/navigation/playback.
Other mapped entry points and sub-features were not exhaustively executed.

The stand-in setup used an unsigned Debug install, fixture API/authentication,
an in-memory project cache, and a deliberately unavailable camera. Models cost
$0 because those transport fixtures never call providers. Live sign-in/backend,
live model quality, actual phone recording/render/export, and production remain
unverified. No output-video claim follows from this script-only proof.
