# Verification wrapper acceptance

Record these failure modes before implementing the helper. Test through its CLI;
reuse the app's existing XCUITests rather than adding implementation-shaped tests.

- A second run in the same worktree could consume another run's build receipt.
  Launch must acquire an exclusive worktree lock before creating a simulator.
- A forged or copied run manifest could target an unrelated simulator. Doctor
  and cleanup must compare the worktree, owner token, UDID, and unique device name
  against an independent ownership record before using that target.
- A successful old build could drive changed native inputs. Doctor must reject
  a changed fingerprint, mismatched receipt, wrong installed binary, or stopped
  instance. Doctor itself must not write files or launch anything.
- Build failure, UI failure, interruption, or missing persistence could strand a
  simulator or erase useful evidence. Capture failures and clean up only the owned
  target; screenshots, logs, result bundles, and draft copies must survive.
- A selected test could never execute or pass only after a retry. Preserve the
  repo's smoke selection, result coverage verification, and flaky-test report.
- A Photos/export status or fixture response could overstate real behavior.
  Name every substituted boundary; export and backend claims need independent
  side-effect evidence. Unexecuted journeys remain unverified.
- The teleprompter UI could reopen cached text without writing the file. Require
  the exact typed script in a copied Application Support draft.json, alongside
  action/result screenshots and the selected XCTest result.

## First proof

Run launch, Doctor, and the mapped teleprompter script journey on a new iPhone
13 Pro simulator. Check a stale fingerprint and an unrelated simulator ID are
rejected; compare unrelated simulator inventory before/after cleanup. Confirm
the proof directory remains readable after the owned simulator is deleted.

The simulator stands in for an iPhone: it uses unsigned Debug installation,
fixture authentication/API replies, and deliberately unavailable camera input.
Those setup steps do not establish hardware camera, live account/API, or
production behavior. Models are never called by these fixtures.

## Automatic PR coverage failure modes

Before implementing CI selection, guard shared/unmapped files and mixed feature
changes against smoke-only coverage. Deleted/renamed test IDs or a broken map
must fail the selector. Preserve the existing affected group and smoke tests
without executing duplicate test IDs. Docs-only diffs should stay cheap.
An absent/truncated script file or missing selected XCTest result must fail
evidence capture; artifact upload must run on failed UI attempts too. Reuse the
existing compiled app/simulator and existing required gates for every PR author.
