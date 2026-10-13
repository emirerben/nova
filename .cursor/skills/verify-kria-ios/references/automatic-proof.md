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
