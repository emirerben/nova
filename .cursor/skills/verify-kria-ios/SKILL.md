---
name: verify-kria-ios
description: Verify Kria's native iPhone app through its real UI with the existing XCUITest harness. Use for creation, media intake, teleprompter, native editor Save/reopen/playback/export, and slide-post checks; capture repeatable evidence on an owned disposable simulator.
---

# Verify Kria iOS

Read [the feature index](features/README.md), then the affected journey. Work from
the task's isolated Nova worktree. Default: offline Debug app, disposable iPhone
13 Pro simulator, iOS 26.5, fixture API/authentication, $0 model spend. Web/API
and physical iPhone checks have separate recipes in the repo's runbooks.

Local hooks run affected journeys in the background on each developer's Mac.
Normal agent startup/worktree setup installs them; pull/rebase, pushes of PR
commits, and agent completion enqueue checks. [journeys.json](features/journeys.json)
selects bounded tests; shared UI inputs select all five areas plus smoke (18
tests), without changing CI selection. Results are remembered by commit and
coverage. See [local automation](../../../docs/runbooks/local-ios-verification.md)
for setup, status, retry, and limitations. The commands below reproduce a run.

## Launch

Requires macOS, Xcode, XcodeGen ≥2.38, Python 3, and the installed iOS 26.5 runtime.
Swift packages may download during the first build. No `.env`, API, worker,
database, migrations, or live account is required for this offline fixture mode.

From the worktree root:

```sh
KRIA_VERIFY_CONTROL="$PWD/.cursor/skills/verify-kria-ios/control-kria-ios"
KRIA_VERIFY_RUN="teleprompter-$(date -u +%Y%m%d-%H%M%S)"
"$KRIA_VERIFY_CONTROL" launch --run "$KRIA_VERIFY_RUN"
```

Launch creates an empty simulator, runs `scripts/ios/verify.sh` in `prepare-ui`
mode with its explicit `KRIA_SIMULATOR_ID`, passes unit checks, installs
`com.kria.app.dev`, and launches `-ui-testing-chat` with
`KRIA_CHAT_CREATION_FLOW=v1`. The fixture supplies project/capability replies;
it never reads the developer's Keychain. `Ready for XCUITest` means the owned
fixture process and matching build receipt are ready. The required smoke journey
asserts the usable `What are we making?` workspace before acceptance.

One active run per worktree; separate worktrees can own separate simulators.
Never double-drive a shared simulator. A launch/build failure cleans its owned
target and retains logs. Teardown: `"$KRIA_VERIFY_CONTROL" cleanup --run "$KRIA_VERIFY_RUN"`.

## Doctor

```sh
"$KRIA_VERIFY_CONTROL" doctor --run "$KRIA_VERIFY_RUN"
```

This read-only check validates independent ownership, booted simulator, running
fixture app, installed binary hash, HEAD/native-input fingerprint, fixture mode,
and the one-use preparation receipt. It launches nothing and writes nothing.
Run it first if anything looks wrong. A changed build or consumed receipt needs
cleanup and a fresh run; do not recreate a receipt by hand. Fixture auth does
not establish authenticated backend access.

## Drive

```sh
"$KRIA_VERIFY_CONTROL" drive --run "$KRIA_VERIFY_RUN" --feature teleprompter \
  TeleprompterUITests/testScriptIsSavedAndCameraFailureKeepsItEditable
```

Tests tap accessible native controls through the production UI. Their existing
launch arguments set each journey's baseline. The helper adds required smoke
coverage, uses `scripts/ios/ui_tests.py` for selectors/result verification, and
runs the repo's serial UI phase with its existing retry/flakiness reporting.
One selection consumes the receipt; combine affected tests in that selection
or create another run. Use map-listed entry points, not convenient fixture
hosts as substitutes for an entire creation journey.

## Evidence

Evidence lives at `test-results/verify-kria-ios/$KRIA_VERIFY_RUN/`: `run.json`
contains commands, exit codes and ownership; `prepare-ui.*`/`ui.*` contain logs,
XCTest bundles and flaky-test reports; `attachments/` contains screenshots;
`proof.json` records selected coverage, revision and limitations.

The teleprompter script journey additionally requires a copied
`persistence/*-draft.json` containing the exact UI-entered script. Inspect the
action/result screenshots and stored text together. A final screen alone is
insufficient. For other mutations, independently inspect the resulting file or
saved/reopened value. Export requires the actual output/Photos asset, not a
success banner alone. Map notes identify exporters, uploads, Photos writers or
server persistence substituted by fixtures. Only external-boundary substitutions
already used by the repo's harness are acceptable; do not mock internal logic.

Report retries and failures, missing preconditions, and unexecuted entry points.
Passing fixture UI is offline evidence; live models, real backend, native output,
hardware and production each need their own proof. Relevant input/HEAD changes
invalidate the previous build. Follow the [KRI-559 evidence rules](../../../docs/reviews/kri-559-journey-verification.md).
There is no dry-run switch: launch installs an app and tests mutate the disposable
simulator. Build-time package downloads are distinct from model calls.

## Cleanup

```sh
"$KRIA_VERIFY_CONTROL" cleanup --run "$KRIA_VERIFY_RUN"
test -f "test-results/verify-kria-ios/$KRIA_VERIFY_RUN/run.json"
```

Cleanup validates ownership, shuts down/deletes only the run-created simulator,
and releases its lock/receipt. It never kills by process name or deletes proof.
Drive failures attempt cleanup automatically; repeat cleanup after an interrupted
run. Keep the worktree build cache for reuse. Preserve evidence outside the
worktree before removing it; do not commit media, credentials or raw user history.

## Helpers

`control-kria-ios` is executable, uses the Python standard library, and anchors
commands to its own worktree. `--help` lists its four commands. See
[acceptance checks](references/acceptance.md) for ownership/staleness/failure
controls, [the first executed proof](references/proof.md), and
[the local hook proof](references/local-hook-proof.md) for results and limits.
`python3 scripts/ios/auto-verify.py status` reports background results;
`python3 scripts/ios/auto-verify.py enqueue --reason pr` queues PR verification.
Update `features/journeys.json` alongside the feature recipes.
Use `/maintain-verification-skill` when keeping this map current.
