# Automatic CI path proof

Executed locally on 2026-10-13 against base
`792ee6ab78d10506bd177bd1cb955ce8c481069e` with this branch's working-tree changes.
Native fingerprint: `30cc276aeb4e49eeef9692b6e56e987d7f725b39d78ac4aab1c90b3db38dfadd`.

- 54 CI-selector/gate checks and 100 iOS orchestration/policy checks passed.
- Real git-diff CLI scenarios selected no native proof for docs-only/skill prose,
  teleprompter for its screen, editor for its view, their union for mixed changes,
  and all five features for shared chat changes.
- The existing native build/unit preparation passed: 1691 tests, 22 skipped,
  zero failures. No second app compilation was needed for UI/evidence capture.
- The automatic smoke-plus-map selection ran 18 UI tests across all five feature
  areas, all passed without retries. UI execution took 440 seconds locally.
- Evidence capture verified all 18 named results, exported action/result
  screenshots, and copied one draft with the exact UI-entered teleprompter script.
- Owned simulator cleanup completed and proof artifacts survived. The new CI
  collector only reads evidence; it does not stop shared simulators.
- Python lint/format, workflow YAML parsing, and the repo pre-PR gate passed.

## Reproduce the automated selection

From a worktree with the app prepared by the existing iOS harness:

```sh
KRIA_VERIFY_FEATURES=projects,creation,teleprompter,editor,slides
KRIA_IOS_UI_GROUPS="$(python3 scripts/ios/verify-feature-map.py groups smoke "$KRIA_VERIFY_FEATURES")"
export KRIA_IOS_UI_GROUPS
python3 scripts/ios/verify-feature-map.py snapshot-build
KRIA_IOS_TEST_MODE=ui bash scripts/ios/verify.sh
python3 scripts/ios/verify-feature-map.py capture "$KRIA_VERIFY_FEATURES" "$KRIA_IOS_UI_GROUPS"
```

CI uses its already-selected simulator and compiled app, retains the affected
UI group, and adds mapped tests without duplicate IDs. The local broad-input
proof above exercises the 18-test smoke-plus-map fallback; full creation-group
coverage for mapped shared chat inputs is enforced by the selector policy tests
and executes on their PRs. It was not rerun exhaustively in this local proof.

Full local evidence: `test-results/verify-kria-ios/ci/` plus
`test-results/verify-kria-ios/automatic-ui.log`, `automatic-evidence.log`,
`selection-e2e.json`, `ci-policy.log`, and `ios-policy.log` in the execution
worktree `/private/tmp/nova-verify-kria-ios`. These artifacts are ignored by Git.
On GitHub, the `kria-ios-verification-<SHA>` artifact retains this evidence and
the unit/UI result bundles for 14 days, including failures.

The simulator/offline boundaries remain as described in [the first proof](proof.md).
This establishes native fixture UI and script persistence, not live backend,
physical camera, production, or output-media content. Model spend was $0.

## Final integrated-branch rerun

The 18-test automatic path was rerun after integrating the newer main revision.
Tested HEAD: `d461780365ae7d585e951e835d24304771a156bd`; native fingerprint: `6a9412ceae1b5a725453e38ac90efbd71a64804c8625b3aebcff3d92908b201a`.
All 18 selected UI tests passed without retries; exact-script persistence and
cleanup passed again. The local UI phase took 435 seconds. The final portable
checks passed 54 CI and 101 iOS orchestration/policy cases.

A deliberately failed Test Case result was rejected while exported screenshots
survived; its retained red-control evidence is under
`test-results/verify-kria-ios/failed-evidence-control/`. Debug app identity now
includes the launcher and code dylibs, and its held-out change check passes.
The earlier broad-input proof is retained under
`test-results/verify-kria-ios/automatic-ci-proof/archive/`; latest evidence is
under `test-results/verify-kria-ios/ci/` and `final-ui.log`/`final-evidence.log`.

A separate newer drawing PR landed while this run was finishing. GitHub's PR
merge-checkout CI supplies the combined validation before merge; the local
claim above remains tied to its recorded revision.
