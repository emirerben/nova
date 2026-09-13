# Change-based CI

PRs targeting `main` or `dev` select whole suites from their merge-base diff.
Every push to `main` runs full web, API, lint and iOS regression coverage.
PRs run the affected iOS build/unit phase; the exhaustive iOS UI phase runs on
main and manual dispatch. The test assertions, web partitioning and iOS test
execution remain unchanged.
Other specialized workflows and deployment triggers retain their existing rules.

## Selection policy

| Changed files | PR coverage |
| --- | --- |
| `src/apps/web/**`, web test runner scripts | Web tests and web lint |
| Native app, media engine, resources, UI tests, `scripts/ios/**`, iOS workflow | iOS build/unit on PRs; full iOS coverage on main and manual dispatch; shell tests and mobile contracts |
| Native unit tests/fixture or `Kria/Generated/**` only | iOS build, unit tests and mobile contracts; no PR UI execution |
| Web public fonts and type-posters bundled in Xcode | Web and iOS build/unit on PRs; full iOS coverage on main |
| API internal implementation, prompts, tests | API tests and API lint |
| API routes/schemas, Kria contracts, models/config/main/worker, mobile identity, API dependency definitions | Web, API and native build/unit contracts; fixture-driven UI tests are not selected |
| Shared packages, assets, root dependencies/build config, selector, general CI workflows or unknown paths | All suites |
| `docs/**`, `plans/**`, `agents/**`, listed root documentation, `VERSION` | No heavyweight suites |
| Root `package.json` / `package-lock.json` changing only release versions | No heavyweight suites |

The `ios_ui` output identifies UI-affecting changes and always implies
`ios=true`; PR workflow policy currently defers the slower UI phase. Every main
push still runs both phases. See the
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

Web CI installs from the checked-in `src/apps/web/package-lock.json` with
`npm ci`, so dependency resolution is locked rather than based on an unlocked
pnpm install. API tests run as two deterministic round-robin shards over the
complete non-quality test-file set; both shards feed the same stable `test-api`
gate, so sharding changes wall-clock scheduling without reducing coverage.

## Verification and rollback

```sh
python3 -m unittest discover -s scripts/ci -p 'test_*.py' -v
node --test scripts/ci/web-tests.test.mjs
actionlint .github/workflows/ci.yml .github/workflows/ci-changes.yml .github/workflows/ios.yml
```

Regression cases cover real git histories, renamed/deleted/mixed files, unusual
filenames, moving base branches, release metadata versus dependencies, missing
diffs, and gate success/failure/cancellation combinations.

Local validation passed all 320 web suites. The slow Chat group contained 112
tests and completed in 6 seconds locally; an older CI run took 796 seconds for
that group, so the 7–10-minute turnaround target remains a goal rather than a
measured guarantee.

To inspect a PR locally, set `CI_EVENT=pull_request`, `CI_BASE=<base SHA>` and
`CI_HEAD=<head SHA>` when running `python3 scripts/ci/select-tests.py`. Without a
PR event the selector runs all suites, making manual diagnosis conservative.

To disable selection without changing required-check names, make `selection()`
return all suites, then update the selector tests. Do not turn failed detection
into an intentional skip or remove the final gates.
