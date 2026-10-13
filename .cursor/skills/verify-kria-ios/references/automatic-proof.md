# Earlier native proof artifacts

Historical evidence from the former CI integration experiment. The final
implementation uses [local hooks](local-hook-proof.md); its GitHub CI extension
was removed at the user's request. The earlier artifacts remain intact.

- Base `792ee6ab78d10506bd177bd1cb955ce8c481069e`: 18 smoke-plus-map UI
  tests passed without retries in 440 seconds; native preparation passed 1691
  tests, 22 skipped. Exact-script draft persistence and cleanup passed.
- Commit `d461780365ae7d585e951e835d24304771a156bd`: the same 18 UI tests
  passed without retries in 435 seconds after integrating newer main.
- A deliberately failed Test Case result was rejected while its exported
  failure screenshots survived. The Debug identity guard includes code dylibs.

Ignored evidence in `/private/tmp/nova-verify-kria-ios/test-results/verify-kria-ios/`:
`automatic-ci-proof/archive/`, `ci/`, `final-ui.log`, `final-evidence.log` and
`failed-evidence-control/`. These establish offline native fixture UI/local
persistence, with $0 models; they do not establish production or hardware parity.
