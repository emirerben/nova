# Release metadata before expensive CI

The November 6 request was to assess PR delays and improve the process. This
change addresses a remaining dependency gap in CI. It does not estimate how many
hours this gap cost the reported November 4–5 batch.

## Contract and decision

The existing release ownership workflow rejects feature edits to `VERSION`,
`CHANGELOG.md`, and root package version fields. That workflow runs independently
of suite selection. An invalid PR can therefore queue API and native work before
its author sees the ownership failure.

The reusable selector now runs the same guard before emitting suite selections
for PRs targeting main. No validator rules change. The guard uses the event head
and the current base's merge base, matching the separate ownership workflow.
The existing required checks still fail when selection fails.

Moving PR evidence validation into this dependency would require a separate
recovery design. A body edit currently runs only the lightweight evidence
workflow. A native job skipped after invalid evidence would need a safe restart,
including fresh fixture artifacts bound to the run attempt. This change leaves
that workflow and its trusted baseline policy untouched.

## Acceptance and failure cases

The cases were written before changing the workflow. The tests execute its real
shell block against local Git histories and the real release metadata guard.

| Case | Required observation |
| --- | --- |
| Manual release file or root package version edit | Nonzero exit before suite selection |
| Newer main merged into a feature, followed by dependency changes | Selection remains allowed |
| Checkout differs from the event head | Validate the event head |
| Missing head history or unavailable base ref | Fail before selection |
| Invalid metadata followed by a corrected commit | Selection recovers without another main update |
| Main push, nightly, manual run, or PR to dev | Added guard does not apply |

The creator's media journey is not applicable. This change only orders an
existing CI policy check. There are no product, render, prompt, deployment, or
repository setting changes.

## Offline evidence

Before implementation, the six preflight tests failed because the workflow had
no early ownership step. After implementation, all six passed, including five
different invalid metadata changes and the corrected-commit recovery.
The complete offline CI contract suite passed all 167 tests when written; after
merging origin/main on 2026-11-11 it passes all 178. Scoped Ruff,
workflow YAML parsing, the CLAUDE.md size guard, and diff checks also passed.

Reproduce from the feature worktree:

```sh
python3 -m unittest discover -s scripts/ci -p test_release_metadata_preflight.py -v
python3 -m unittest discover -s scripts/ci -p 'test_*.py' -v
ruff check scripts/ci/test_release_metadata_preflight.py
ruff format --check scripts/ci/test_release_metadata_preflight.py
bash scripts/check_claude_md_size.sh
git diff --check
bash scripts/preship-check.sh --pr-body .dev/pr-body.md
```

These are offline Git and workflow contract checks. Hosted Actions execution and
independent exact-head review remain separate gates. No native build or paid
model run is needed to verify the changed contract locally. The workflow change
still selects the existing hosted suites conservatively.

## Remaining process work

Read-only inspection on November 6 found one active default-branch ruleset,
[22795779](https://github.com/emirerben/nova/rules/22795779). It required `lint`,
`test-api`, strict up-to-date checks, one approval, and code-owner review. It
dismissed stale approvals. No merge-queue rule was configured, and the repository
reported auto-merge disabled. Classic branch protection returned 404 because
protection is expressed through the ruleset. These observations are dated, not a
new settings policy.

The following owners are responsibilities for follow-up, not new assignments.

| Improvement | Owner responsibility | Dependency |
| --- | --- | --- |
| Send each green PR for review as soon as it is ready | PR author and shipping coordinator | Exact head, evidence, pre-review findings, and named next reviewer |
| Avoid repeated branch-update waves | Shipping coordinator | Current rules and a deliberate integration order; no-conflict status alone does not prove compatibility |
| Evaluate a merge queue | Repository administrator and CI owner | Confirm plan eligibility, add and verify `merge_group` coverage before any settings change |
| Profile slow native tests | Native CI owner | Coordinated runner access, preserved export assertions, and critical-path timing |
| Prompt identities | Runtime and prompt owners | Superseded on main: Main Creator and brief-extractor versions are now automatic fingerprints ([history](../pipelines/prompt-version-history.md)). This row is retained as history; no work is assigned here |

The repository already has scoped suites, stale-run cancellation, selector-chosen
native legs (`native_legs`; PRs run the unit leg and affected UI groups), manifest checks before Mac allocation, and a separate body-edit evidence
workflow. Any further change must account for those existing mechanisms. No
modeled savings or 10x claim is established by this PR.
