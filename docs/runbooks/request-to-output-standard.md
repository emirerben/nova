# Request-to-output standard (KRI-577 / KRI-559)

Apply this before implementation, review and each new PR. For user-visible fixes,
start from the creator's original request and finish with evidence for the affected
journey. Internal checks support that evidence; a passing count, plan or render
check alone cannot establish that the request succeeded. For changes without a
user-visible journey (for example, documentation or CI), state why that journey
is not applicable and verify the changed contract instead.

## Define acceptance before changing code

Read the Linear issue, relevant comments and original incident. Write observable
acceptance criteria before implementation: what the creator will see or hear,
what the product must refuse or clarify, and what result would still be wrong.
Keep the original request and its constraints visible through the investigation.

Trace the relevant real boundaries: request and media inputs → parsed brief →
source selection → approval and edits → dispatch → rendering/export → receipt.
Record where each requirement is preserved, checked or lost. Exercise the affected
chain without replacing its responsible planner, parser or verifier with a mock;
provider transport may be replayed. A bounded local fix is allowed, but name the
remaining gaps, their owners and the parent incident that stays open.

## Check meaning as well as structure

For a request such as “six named actions, two seconds each, with a photo after the
wave,” six ordered cuts prove count and order only. Establish source media IDs and
start/end intervals for each named action using independent evidence, such as a
reviewed source video with timestamped observations. The planner's own labels and
array positions are not independent evidence that a cut shows the action.

Keep these acceptance checks separate:

| Check | What it establishes | What remains unproven |
| --- | --- | --- |
| Plan executability | The supported planner can build the approved request. | The chosen footage depicts the requested actions. |
| Recipe fidelity | The exported picture, timing and audio match the recipe. | The recipe represents the creator's intent. |
| Request fulfillment | The inspected result satisfies the original visible/audible requirements, grounded in the sources. | Deployment to another account, cohort or revision. |

Include a negative control appropriate to the changed behavior. For named moments,
replace a requested action with the wrong footage while preserving count, duration
and order; the fulfillment check must reject it. Merely disabling the patch proves
the regression test responds to that patch, not that it detects wrong content.
Do not claim semantic verification if the check still accepts that control. A PR
that adds honest refusal or fixes a narrower boundary may proceed with that gap
explicit; the broader incident remains open.

## Select a proportionate case matrix

Record the original compound request plus relevant neighboring cases and omissions.
For a chat/edit change, consider correction after approval, clarification, retry,
unsupported effects and the “simpler version” recovery. Include EN/TR when parsing,
placement or reply language is involved; single versus multiple inputs and other
archetypes only where they share the changed requirement or boundary. Add unfamiliar
media/request wording when claiming a general fix. State why excluded cases do not
apply. This is not a requirement to test every feature on every platform.

## Attach evidence and limit the claim

Every PR records its issue/parent incident, acceptance criteria, relevant boundaries,
case matrix, negative control, exact checks and safe artifact links, tested revision,
evidence level, and remaining gaps. Keep media, signed URLs, tokens and private
transcripts out of git. A temporary local artifact needs a durable, safe summary
or retained artifact before handoff. Reviewers check what the evidence establishes;
CI can enforce required fields and mapped checks, but cannot turn an unchecked
claim into inspected output.

Use the PR template's single fenced `request-evidence` JSON record. It binds the
claim (`local_fix` or `request_fulfillment`) to `head_sha`, records the original
request, observable outcome, root cause and independent expectation sources, and
declares incident replay, wrong-content control, recovery, fulfillment, export
fidelity and rollout checks. Each check is `passed`, `pending` or `not_applicable`;
unverified work needs a reason and tracked gap owner (`owner_issue`), while excluded
work needs a concrete explanation of why it does not apply.
The validator in `scripts/ci/pr-evidence.py` checks these declarations and revision
binding; it does not execute commands from the PR body or certify semantic output.

Save the PR body locally and run:

```bash
bash scripts/preship-check.sh --pr-body .dev/pr-body.md
```

The body defaults to `.dev/pr-body.md`; `PR_EVIDENCE_BODY` can select another path.
Preship considers working changes as well as committed changes. The lightweight PR
evidence workflow also rechecks body edits; code pushes still run the existing lint
and mapped gates. Match the final PR body and final SHA to the reviewed record.

Label evidence separately:

- **Offline replay:** deterministic fixtures and real internal boundaries; no live
  provider, device or deployment claim.
- **Live model:** actual provider call with recorded input reference, result and
  cost; no native-output claim. Paid runs require the existing explicit approval
  and budget rules; this standard does not make them mandatory for every PR.
- **Native output:** an inspected device/simulator/renderer artifact, with the
  platform identified. API or recipe success does not prove a phone export.
- **Production:** deployed revision, relevant process/client build, UTC observation
  and observed result for the affected user/cohort.

Bind checks and artifacts to the final code SHA; after relevant code or fixture
changes, rerun the affected evidence and mark superseded evidence stale. Record
unknown values as `unknown`. Say “implemented,” “offline verified,” “native export
verified” or “deployed, verification pending” according to the evidence available.
A missing device or paid check limits the completion claim; it does not create an
unapproved paid run or authorize a deployment.

## Verify delivery separately

After an authorized rollout, confirm the final revision and effective API/worker
settings (both can differ), client build/protocol where relevant, and account/cohort
eligibility. Re-send the original request on the affected path, inspect the result
and receipt, and record UTC time, job reference and outcome. A default flag or a
merged PR does not establish exposure. Keep the parent incident open until its full
acceptance criteria are met, even when individual local fixes are complete.

Use the [PR acceptance checklist](pr-acceptance-checklist.md) for commands and the
[agent navigation guide](agent-navigation.md) for safe diagnostics. The
[KRI-559 baseline](../reviews/kri-559-journey-verification.md) records prior evidence
and ownership. This policy enforces evidence declarations; actual named-action fulfillment
remains separate work in KRI-775/KRI-723. It does not establish that any production
rollout has occurred.

## Record format and activation

The template's `schema_version: 1` record names the KRI issue, original request,
`request_provenance`, observable `expected_outcome`, `root_cause` and exact
`head_sha`. Use `claim: local_fix` when the overall request still has gaps. A
`request_fulfillment` claim also needs `request_artifact` and passed incident,
wrong-content, recovery, fulfillment and export checks. Its fulfillment and export
evidence must include inspected native/renderer output or production.

Each of the six check kinds appears once. A passed row supplies `evidence_level`,
`command` and `artifact`. Passed incident, wrong-content and fulfillment rows also
supply `expectation_source`: an immutable reference to an expected fixture or
reviewed source observations. Expected and result artifacts must differ for
wrong-content and fulfillment checks. This field is a reference, not an English
sentence asserting correctness. A pending row uses `evidence_level: pending` and
names `reason` plus `owner_issue`; a non-applicable row uses
`evidence_level: not_applicable` and explains why. Even a local-fix claim needs an
executed implementation check.

References can be regular, nonempty files tracked at the declared head, or pinned
GitHub blob/commit, numbered Actions artifact, or run-attempt URLs. Mutable branch
links, symlinks, missing files and signed/query-string URLs are rejected. Remote
availability, run-to-commit association and media content still require review;
the validator makes no network request to certify them.

Inspect scope without making a verification claim:

```bash
python3 scripts/ci/pr-evidence.py --base origin/main --head HEAD --working --requirements
```

Commit affected implementation and policy files before validating evidence; a
record for the old head cannot cover dirty affected files. Then run preship with
the body file and use that exact file when creating or updating the PR. Every new
commit needs an updated head and fresh affected evidence.

For initial activation, merge the policy through normal review, run the new
workflow against current main, and add `PR evidence` to the existing main ruleset
alongside `lint` and `test-api`. Preserve review requirements, strict checks and
existing bypass actors. Verify that a body-only edit removing evidence fails that
required check without starting API/native suites. Until this additive activation,
body-edit failures are visible but not merge-blocking; the code-push lint
requirement remains enforced.
