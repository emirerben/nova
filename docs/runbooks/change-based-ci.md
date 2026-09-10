# Change-based CI

PRs targeting `main` or `dev` select whole suites from their merge-base diff.
Every push to `main` runs full web, API, lint and iOS regression coverage.
The test assertions, web partitioning and iOS test execution remain unchanged.
Other specialized workflows and deployment triggers retain their existing rules.

## Selection policy

| Changed files | PR coverage |
| --- | --- |
| `src/apps/web/**`, web test runner scripts | Web tests and web lint |
| Native app, media engine, resources, UI tests, `scripts/ios/**`, iOS workflow | iOS build, unit/UI tests, shell tests and mobile contracts |
| Native unit tests/fixture or `Kria/Generated/**` only | iOS build, unit tests and mobile contracts; no UI execution |
| Web public fonts and type-posters bundled in Xcode | Web and full iOS coverage |
| API internal implementation, prompts, tests | API tests and API lint |
| API routes/schemas, Kria contracts, models/config/main/worker, mobile identity, API dependency definitions | Web, API and native build/unit contracts; fixture-driven UI tests are not selected |
| Shared packages, assets, root dependencies/build config, selector, general CI workflows or unknown paths | All suites |
| `docs/**`, `plans/**`, `agents/**`, listed root documentation, `VERSION` | No heavyweight suites |
| Root `package.json` / `package-lock.json` changing only release versions | No heavyweight suites |

The `ios_ui` output selects the slower UI phase within the iOS job; it always
implies `ios=true`. Every main push still selects both phases. See the
[iOS runbook](ios-development.md#change-based-ci) for phase commands and cache reuse.

Mixed changes select the union. Runtime-tree Markdown (including prompts) is
classified as code before documentation rules. New/unrecognized paths select
all suites. Dependency versions inside a lockfile are not release metadata.
The exact policy lives in `scripts/ci/select-tests.py`; extend it with regression
cases when adding a new subsystem or cross-subsystem dependency.

## Detection and required checks

`.github/workflows/ci-changes.yml` is reused by CI and iOS. Each call checks out
full history, runs the selector's offline regression tests, and compares the PR
head with the merge base of its event base/head SHAs. Tests still execute on the
normal GitHub merge checkout. The diff uses NUL delimiters, has no API file-count
limit and disables rename detection so both old and new paths are considered.
No fork tokens or PR-supplied shell interpolation are needed.

An unavailable or empty diff selects all suites. A broken selector or failing
selector test blocks the final gates instead of claiming a successful skip.
The selection and reason appear in the workflow summary.

Stable checks remain `lint`, `test-api`, `test-web`, and iOS `build-and-test`.
The heavy jobs are conditional; the small final checks use `always()` and verify:

- Selection completed successfully and provided a literal `true` or `false`.
- Selected suites completed successfully (all web matrix jobs must pass).
- Unselected suites were actually skipped.
- Missing, failed, cancelled or unexpectedly skipped jobs fail the check.

No branch-protection changes are needed for the existing required `lint` and
`test-api` names. Unrelated PRs still use small Ubuntu selector/gate jobs but
avoid dependency installation, PostgreSQL/Redis services, five web workers and
macOS runners. Superseded PR runs are cancelled; main runs are not cancelled.

## Verification and rollback

```sh
python3 -m unittest discover -s scripts/ci -p 'test_*.py' -v
node --test scripts/ci/web-tests.test.mjs
actionlint .github/workflows/ci.yml .github/workflows/ci-changes.yml .github/workflows/ios.yml
```

Regression cases cover real git histories, renamed/deleted/mixed files, unusual
filenames, moving base branches, release metadata versus dependencies, missing
diffs, and gate success/failure/cancellation combinations.

To inspect a PR locally, set `CI_EVENT=pull_request`, `CI_BASE=<base SHA>` and
`CI_HEAD=<head SHA>` when running `python3 scripts/ci/select-tests.py`. Without a
PR event the selector runs all suites, making manual diagnosis conservative.

To disable selection without changing required-check names, make `selection()`
return all suites, then update the selector tests. Do not turn failed detection
into an intentional skip or remove the final gates.
