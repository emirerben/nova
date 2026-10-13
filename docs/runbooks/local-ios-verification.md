# Automatic local iPhone verification

Emir and Yasin's Macs run affected offline native UI journeys in the background.
GitHub's existing CI jobs, required checks and UI-group selection remain intact.
The hook executes the verification skill's helper; it does not start an AI agent.
No API/database stack, account credentials or model calls are needed ($0 models).

## Enable on each Mac

After this change merges, pull it and start a normal Codex/Claude session or run
the normal worktree setup. Those existing entry points install the Git hooks
for that clone and enqueue relevant committed changes. Each Mac needs Xcode,
XcodeGen >=2.38, Python 3 and iOS 26.5 installed. Missing prerequisites produce
a failed/unverified result and retained logs; they never become a passing proof.
First-time Swift package downloads may require network access.

Manual setup, useful for a terminal-only clone:

```sh
python3 scripts/ios/auto-verify.py install
python3 scripts/ios/auto-verify.py enqueue --reason setup
```

Installation wraps existing Git hooks and preserves their arguments, stdin and
exit status. It is per clone, shared by that clone's worktrees. It leaves global
Git configuration and existing hook files intact. Old worktrees without the
new helper continue their existing hooks but cannot enqueue native verification.

## What triggers a run

- A pull/merge fires `post-merge`; a rebase fires `post-rewrite`.
- A push queues the pushed branch commits, including a branch other than HEAD.
  This covers PR creation/updates from terminal, agents and GitHub's web UI after
  the local push. The hook returns immediately while verification continues.
- Codex/Claude completion queues committed changes, covering agent PR creation.
  Session startup also catches changes pulled before hooks were installed.
- A terminal-only PR opened against an already-pushed commit needs the original
  push hook to have been installed, or this explicit command:

  ```sh
  python3 scripts/ios/auto-verify.py enqueue --reason pr
  ```

There is no Git event for opening a PR on GitHub. An installed local hook only
runs when that Mac performs its trigger; web-only changes, sleeping/offline Macs,
disabled hooks and other developers' clones cannot guarantee a run before merge.
Existing CI remains the shared merge gate. Local completion is separate evidence.

Changes select features from the skill's
[journeys.json](../../.cursor/skills/verify-kria-ios/features/journeys.json).
Docs-only and native unit-only changes skip these UI journeys. Shared/unknown
UI input selects all five areas: 16 mapped tests plus smoke, 18 unique tests.
The 98-test creation-group expansion is not part of this automation.

## Execution and evidence

One worker per clone executes queued runs serially. It creates a detached Git
worktree at the queued commit, then runs Launch → Doctor → Drive → Cleanup.
Your active checkout, dirty files, selected simulator and physical iPhone are
untouched. A second event reuses an existing result for the same commit, map
version and sufficient feature coverage, including a reported failure. New code
or missing coverage needs new proof. Queued commits keep their original revision
even when you continue editing, switch branches or pull again.

The worker copies logs, XCTest bundles, screenshots, fingerprint, fixture config
and exact-script draft evidence out before removing its own snapshot. It cleans
the owned simulator after failures too. Abandoned runs are recovered on the next
worker wakeup and reported as interrupted; no unfinished run is called passed.

The shared evidence location is in the primary checkout of the clone:
`test-results/verify-kria-ios/auto/runs/<request-key>/`. Worktree removal therefore
does not remove the proof. Each directory includes `automation.json`,
`automation.log`, `run.json`, and any produced native artifacts; failed setup
may have only automation logs. Evidence is retained until explicitly deleted.
`KRIA_VERIFY_AUTO_ROOT` can choose another local evidence/queue directory.

macOS receives a local completion notification (subject to notification settings).
Agent hooks also display recent statuses on the next session/completion. No
messages or artifacts are sent to Slack, GitHub or another person.

```sh
python3 scripts/ios/auto-verify.py status
python3 scripts/ios/auto-verify.py status --json  # includes request keys
python3 scripts/ios/auto-verify.py retry REQUEST_KEY
```

Retry creates a fresh run and preserves the earlier failed evidence. Workers
start automatically; `python3 scripts/ios/auto-verify.py worker --root /absolute/queue`
is the internal/resume entry point. `bash scripts/hooks/verify-kria-ios.sh session`
and `... finish` are the agent/bootstrap hook invocations. Installed Git
dispatchers are internal and forward the original Git event automatically.

## Disable and restore

```sh
git config --local kria.verifyIos false  # skip future automatic enqueue/install
python3 scripts/ios/auto-verify.py uninstall  # restore previous hooks configuration
```

`KRIA_VERIFY_AUTO_DISABLED=1` disables hooks for one command; CI also disables
automatic enqueue. Existing runs finish their owned cleanup. Re-enable with
`git config --local kria.verifyIos true`, then normal session setup or `install`.
`KRIA_VERIFY_AUTO_NOTIFICATIONS=0` suppresses local notifications while retaining
reports. Cleanup/restoration preserves all proof artifacts.

Fixture uploads, server persistence and exporters retain their documented limits.
Physical capture, real output media, live backend/models and production behavior
remain unverified without their separate evidence. Keep the feature map current
with `/maintain-verification-skill`.
