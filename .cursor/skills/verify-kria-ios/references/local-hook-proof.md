# Local hook proof

Executed 2026-10-13 on the installed iPhone 13 Pro / iOS 26.5 simulator runtime.
Tested commit: `c3f93920fb201d33b3500ac096221f12ed087b6b`.
Native build fingerprint: `3d4ce1ecac865859f426eed3e16faaa0aa95b81b83d1bf8036ac93504b50f6c5`.
Debug launcher/code hash: `59ec455576625e8a4419733248e81c405f34ef1d6f749f6cca615586b99c6061`.

## Executed path

- Created an isolated local clone and installed the actual Git dispatchers.
- Invoked its `pre-push` hook with the real commit/ref payload. It returned in
  0.437 seconds; the detached worker continued without blocking the hook.
- The worker created a frozen worktree at that commit, then ran the generated
  helper's Launch, read-only Doctor, Drive, evidence capture and Cleanup.
- Native preparation passed 1709 tests, 22 skipped, no failures.
- All 18 unique smoke-plus-map UI tests passed without retries. The UI phase
  took 440 seconds; the complete cold worker run took about 12 minutes locally.
- Talking's typed script survived camera failure and reopening. Independently
  inspected the exported screenshots and `persistence/0-draft.json`; its text
  exactly matches both UI-entered lines.
- The owned simulator and frozen worktree were removed. All 50 preexisting
  simulator identities/states were unchanged, and proof artifacts survived.
- A repeated PR enqueue reused the passing result, with only one request and
  no additional simulator/build/test run.

## Lifecycle and policy checks

110 iOS orchestration/map/lifecycle checks and 54 CI-selector/gate checks passed.
The lifecycle cases use real local Git operations but stub the expensive
simulator boundary: duplicate events, dirty source preservation, real pull and
rebase, non-current pushed branches, existing hooks/stdin/status, failed drives,
explicit retry and recovery after killing only the worker. An unrelated process
survived recovery. These checks were written against the failure cases before
implementing their corresponding behavior.

A later main integration changed CI/release documentation and receipt policy;
all seven updated workflow-policy checks passed. Native source, the helper,
feature selection and local worker code remain unchanged from the tested commit.
All CI workflows, selector and UI groups match current main; the former extra
CI evidence steps and 98-test shared-chat expansion are removed.

## Retained evidence and limits

Full local proof is under
`/private/tmp/nova-verify-kria-ios/test-results/verify-kria-ios/automatic-local-proof/`:
`hook-trigger.json`, `independent-evidence-check.json`, and
`runs/c3f93920fb20-18a2b6f27e7b-af307225/` contain commands, logs, build/fixture
identity, XCTest results, screenshots, the saved draft and cleanup evidence.
Portable results are in `local-hook-policy.log`, `local-hook-ci-policy.log`,
`local-hook-latest-workflow-policy.log` and `local-hook-preship.log` alongside it.
The first direct helper proof remains in [proof.md](proof.md).

This is offline fixture UI and local script persistence evidence, with $0 model
spend. Other entry points listed in the feature files, the full regression
suites, real exports/backend delivery, authenticated/live-model behavior,
physical camera/iPhone parity and production remain unverified by this run.
Enabling the hooks on Yasin's Mac still requires that Mac's normal repo setup
and the installed simulator prerequisites; a run here does not prove his setup.
