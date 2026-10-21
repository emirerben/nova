# PR evidence standard: implementation acceptance

Scope: KRI-577 / KRI-559, 21 October 2026. Base:
`ede1341f2e4608f28aef0a1b05a2e9168a496a70`.

The original request is to make the request-to-output methodology apply to new
PRs: shared session instructions, a required PR evidence record, and automated
rejection of missing evidence. This change implements that workflow. Actual
named-action source grounding and the Merope M2 phone export remain in
KRI-723/KRI-775; they are not claimed complete here.

## Observable acceptance and failure controls

- A new session reads the standard through the existing CLAUDE.md/AGENTS.md entry
  point. The symlink and 38,000-byte instruction budget remain valid.
- An affected PR without a filled record fails. A stale record, duplicate JSON
  block, template placeholder, invalid artifact or unknown revision fails.
- A record cannot promote pending wrong-content/recovery/output checks into a
  full fulfillment claim. A bounded local fix can name unfinished work without
  implying the parent incident is resolved; it still needs executed verification.
- Changing an original runtime path, deleting it, renaming it, or leaving an
  untracked runtime file cannot silently turn the requirement off. Inventory
  failures are errors, not an empty diff.
- The PR body's commands are data only. The lightweight body-edit workflow uses
  the baseline validator and policy, and reads candidate Git history as data.
  Candidate policy edits cannot classify their own runtime changes as exempt.
- The initial introduction of both validator and workflow is explicitly labelled
  as bootstrap. Candidate validation/tests in the existing required lint lane
  cover that one introduction; a missing baseline checkout on later PRs fails.
- PR body edits rerun the lightweight check without starting native suites. The
  existing required lint check also validates evidence on normal PR code events.
- Local preship accepts a body file and includes staged, unstaged and untracked
  changes. Docs-only changes skip automatically; runtime authors cannot self-exempt
  by calling their change documentation.

## Verification scope

Tests use temporary Git repositories and the real validator CLI, and execute the
workflow shell with adversarial baseline/candidate fixtures. They verify the
changed policy boundary without an API database, paid model calls or a phone.
They do not prove the truth of an author's video-review claim. Existing journey
and reliability gates continue to execute their mapped tests independently.

The PR template and parser are checked together. A passed metadata gate means
the declaration is complete and consistent with the claimed evidence level;
reviewers still inspect the independent source expectations and actual output.

The rejection tests include a synthetic credential-shaped URL. Its exact matched
span is recorded in the existing pre-push redaction allowlist; the push guard
remains enabled and the validator still rejects the URL. No credential is used.

## Activation and remaining work

The current main-branch ruleset requires `lint` and `test-api`. The new lightweight
`PR evidence` check must be added to that existing required-check list after this
workflow is available on main, preserving the existing checks and review rules.
This is separate from merging the implementation. Do not claim body-edit
enforcement is active until the ruleset includes the new check. Existing admin
and release-bot bypass actors are outside this change.

No runtime flag, account enrollment, application deployment or production media
was changed. KRI-577/KRI-559 remain open for the broader adoption and incident
acceptance work, including the requested next-three-fix pilot.

## Local verification results

From the isolated task worktree, after integrating the three agents and the
independent adversarial review:

- `python3 -m unittest discover -s scripts/ci -p 'test_*.py' -v`: **113 passed**.
  This includes **36** real-Git validator CLI cases and **5** workflow integration
  cases, plus the existing selector/journey/required-check contracts.
- Scoped Ruff lint and format checks on all three added Python files passed.
- `bash -n scripts/preship-check.sh`, Ruby YAML parsing of both workflows, and
  `git diff --check` passed.
- `bash scripts/check_claude_md_size.sh` passed at **37,989 / 38,000 bytes**;
  AGENTS.md remains the existing symlink to CLAUDE.md.
- Automatic affected-path inspection selects the new evidence policy and its
  focused instructions; no existing application incident gate maps to this diff.

The adversarial review found and fixed receipt reuse after dirty code changes,
rollout-only proof, misleading pending evidence levels, policy-document scope
omissions, prose standing in for expectation artifacts, and unit-only export
evidence attached to a full fulfillment claim. The tests retain these failure
cases. Expected artifacts accept non-English paths without English keyword tests.

Existing CI is responsible for its actual hosted run. The final PR body binds this
report to the exact committed head. This report is local CI-policy evidence, not
an M2 export, a live-model evaluation or observed production enforcement.

## Refreshed hosted failure classification

CI [39666544360](https://github.com/emirerben/nova/actions/runs/39666544360)
ran head `9564fe0bc8befc5457c1ae9212f4a7c4cb4f11ce`. Its independent
PR-evidence check passed, but normal CI had three distinct root failures:

- `api (0)` job `119453881150`: the four EN/TR + outro length-message
  assertions in `test_kri778_block_message`, the strict freeze XPASS in
  `test_kri775_merope_m2_replay`, and the checked-miss assertion in
  `test_unified_montage_dispatch` (six cases).
- `api (1)` job `119453880973`: the M2 card/model-summary assertion in
  `test_kri776_moment_ask_in_brief` (one case). These seven are the same
  independently reproduced main failures covered by separate draft #1735.
  Its exact head `5cacf936eed3a3ea55386e183abe7d149bc0f83d` now passes hosted CI;
  this policy branch does not copy that repair or claim it is merged.
- `journey-offline` job `119454315222`: `scripts/ios/phone-text-style-e2e.py:400`
  still expects Big Curls + İstanbul to require clarification, while the
  merged KRI-785 font fallback now selects Great Vibes. This script and the
  relevant font policy/coverage files are byte-identical to the immutable
  main base `110e87197ccaef219c5dfcfdd65f5f7b41dd712a`. Separate base validation
  is owned by the KRI-721 session; no font repair belongs in this policy PR.
- `lint` job `119455204151`: genuine policy defect. The synchronize event
  retained the previous head's PR-body evidence even after the live PR body
  was updated. The validator correctly rejected that stale snapshot.

`journey`, `build-and-test` and `test-api` failures are aggregate consequences
of those root jobs. Native validation was skipped after the offline failure;
it was not a passing native test run.

### Policy repair and focused verification

Both evidence paths now read the current body with a read-only GitHub token
immediately before validation. They require the same PR number, head SHA and
target branch, preserve this run's immutable base SHA, and treat the fetched
body as file data. A failed API read or mismatched identity fails closed.
The existing validator still checks all evidence fields and exact artifact
revisions; no enforcement is bypassed.

Three new failure-contract tests were added before the workflow change. They
exercise both real workflow shell steps with a local GitHub transport substitute:
corrected body + advancing base, changed head/PR/target branch, invalid body,
API failure, and shell-shaped text remaining inert. The eight affected workflow
tests passed across focused runs after updating the read-only-permission
assertion to distinguish local file writes from remote write permissions.
Scoped Ruff, workflow YAML parsing and whitespace checks passed. Previously
passing application suites were not rerun locally for this policy-only change.

A body edited after lint has already failed still needs that small lint job
rerun; the independent body-edit workflow does not overwrite code-check
results. Main's required-check activation remains a post-merge action.
