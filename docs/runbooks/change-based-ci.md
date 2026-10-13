# Change-based CI

PRs targeting `main` or `dev` select whole suites from their merge-base diff.
Every push to `main` runs full API and iOS regression coverage.
PRs run the affected iOS build/unit phase and a focused UI subset when selected;
the exhaustive iOS UI phase runs on main and manual dispatch. Web application
tests and lint are retired; public web assets bundled into iOS remain covered
by native checks.
Other specialized workflows and deployment triggers retain their existing rules.

## Selection policy

| Changed files | PR coverage |
| --- | --- |
| `src/apps/web/**` | No web suite; bundled public assets still select iOS coverage |
| Native app, media engine, resources, UI tests, `scripts/ios/**`, iOS workflow | iOS build/unit and affected UI scenarios on PRs; full iOS coverage on main and manual dispatch; shell tests and mobile contracts |
| Native unit tests/fixture or `Kria/Generated/**` only | iOS build, unit tests and mobile contracts; no PR UI execution |
| Web public fonts and type-posters bundled in Xcode | iOS build/unit on PRs; full iOS coverage on main |
| `src/apps/ios/Kria/Core/NativeEditorDocument.swift`, `src/apps/ios/Kria/Core/SlidePost.swift` (Swift constants read by API drift guards) | API tests and API lint, plus iOS build/unit on PRs; full iOS coverage on main |
| API internal implementation, prompts, tests | API tests and API lint |
| API routes/schemas, Kria contracts, models/config/main/worker, mobile identity, API dependency definitions | API and native build/unit contracts; fixture-driven UI tests are not selected |
| Shared packages, assets, root dependencies/build config, selector, general CI workflows or unknown paths | All suites |
| `docs/**`, `plans/**`, `agents/**`, listed root documentation, `VERSION` | No heavyweight suites |
| Root `package.json` / `package-lock.json` changing only release versions | No heavyweight suites |

The `ios_ui` output identifies UI-affecting changes and always implies
`ios=true`; PRs run smoke and the affected UI group or changed test methods.
When selection is broad, PRs run the smoke tripwire. Every main push still runs
the complete UI suite. See the
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

Stable checks remain `lint`, `test-api`, and iOS `build-and-test`.
The heavy jobs are conditional; the small final checks use `always()` and verify:

- Selection completed successfully and provided a literal `true` or `false`.
- Selected API and native suites completed successfully.
- Unselected suites were actually skipped.
- Missing, failed, cancelled or unexpectedly skipped jobs fail the check.

No branch-protection changes are needed for the existing required `lint` and
`test-api` names. Unrelated PRs still use small Ubuntu selector/gate jobs but
avoid dependency installation, PostgreSQL/Redis services, five web workers and
macOS runners. Superseded PR runs are cancelled; main runs are not cancelled.

API tests run as two deterministic round-robin shards over the
complete non-quality test-file set; both shards feed the same stable `test-api`
gate, so sharding changes wall-clock scheduling without reducing coverage.
On PRs, a Linux mobile-contract job runs alongside one Mac job for native unit,
selected UI and native journey scenarios. That Mac job compiles once and reuses
the build for every selected scenario. The stable `build-and-test` gate requires
both jobs to pass when iOS is selected. On main and manual runs, the separate
iOS workflow runs the full regression in parallel shards, and main releases to
TestFlight as soon as its build and unit tests pass (see `ios-development.md`).

## Verification and rollback

```sh
python3 -m unittest discover -s scripts/ci -p 'test_*.py' -v
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
