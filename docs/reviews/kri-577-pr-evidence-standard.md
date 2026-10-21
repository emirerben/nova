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
