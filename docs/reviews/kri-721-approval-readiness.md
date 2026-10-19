# KRI-721: executable requirements before approval

## Acceptance plan (written before implementation)

The effective CreativeBrief remains the request authority. Existing renderer
capabilities, strategy policy, receipt checkers and render-contract fields supply
evidence. Readiness is a server-owned assessment, not a second creative plan.

Failure cases to exercise through the real runtime and approval boundary:

- An unknown requirement or a missing receipt must block approval.
- A known unsupported instruction must block even when the receipt calls it a
  format limitation; displaying a limitation is not permission to omit it.
- Requirements deferred by the legacy montage/speech receipt filters must still
  have a disposition before approval.
- A compound supported request must proceed without an extra question.
- English, Turkish and paraphrases must use the same typed requirements and gate.
- An explicit, revision-bound simpler-choice waiver may omit exactly its named
  requirements. Generic replies and stale choices cannot waive an instruction.
- A concrete creative promise cannot be treated as an open delegated choice.
- Draft/source changes and withdrawn capabilities invalidate prior readiness.
- A direct or repeated approval request cannot bypass a missing or failed check.
- Rejection leaves the existing draft, media and accepted output unchanged, creates
  no render attempt, and gives one actionable next step.
- Historical unstamped drafts and flag-off cohorts retain their existing behavior;
  once stamped, an approval remains guarded even if enrollment is disabled.

Baseline incidents: KRI-705's seven Talking requests and KRI-700's typed exposure
request. Their existing focused fixes are regression inputs, not assumed broken.
Add negative controls for a requirement with no checker and an explicitly
unsupported change that previously left approval available.

## Scope and timeline contract

This change controls readiness and consent. It does not edit a timeline. Existing
EditRecipeV2/compiler duration ownership, base-time lanes, ripple, scrub bounds,
resize and undo semantics remain the canonical behavior. Native exported-output
verification remains independent of this pre-approval check. KRI-722/723 own
prepared-execution persistence and broader montage planning.

## Evidence to retain

Record the fresh base, selected capability/cohort, red/green DB/HTTP cases and
physical-iPhone result bundle. Distinguish replayed provider decisions, native
output and deployed production. No production rollout or paid evaluation is
authorized by this implementation record.

## Implementation and rollout

`approval_readiness.py` assesses every live CreativeBrief ID with the existing
receipt checkers, selected-format capability manifest, runtime strategy policy/compiler and
render contract. An unchecked, missing or unsupported requirement blocks approval.
A missing extracted brief also blocks; it is not an empty successful checklist.
Duration evidence comes from the existing validated source-window plan, not the
model's requested duration. Original camera sound uses the existing render-contract
commitment. This assessment establishes executable commitments, not output quality.

Readiness is bound to the brief/version, exact strategy or compiled editor payload,
source identities, prepared window evidence, capabilities and adapter declarations.
The approval endpoint recomputes it before dispatch. A separate execution digest
prevents deleting the draft report from bypassing the reader. Stamped approvals
remain guarded after enrollment is disabled. Source/capability changes cancel
approval, preserve the saved edit and ask the user to prepare it again.

The existing named simpler-version choice can waive only the exact offered IDs at
that brief version. A generic reply cannot waive them. Existing-video scoped chat
edits now extract the full request before checking the real compiled editor payload.
Retry and Undo approval emitters share the same readiness reader; Retry keeps its
saved content rather than asking a model to rebuild it.

New enrollment defaults off. Enable `KRIA_APPROVAL_READINESS_ENABLED` or
`KRIA_APPROVAL_READINESS_USER_IDS` only for accounts already enrolled in
CreativeBrief extraction and BriefBinding. Deploy readers to API and workers before
enrolling the pilot account. No production settings changed in this work. Historical
unstamped, unbound drafts retain their existing behavior. This does not promote
unverified render adapters or replace KRI-722/723 execution preparation.

## Capability and evidence matrix

| Case | Expected behavior | Evidence level |
| --- | --- | --- |
| Exact title + six-second source-bounded cut | Approval without an extra question | EN/TR phone-source and cloud provider replay, real compiler/DB/HTTP approval |
| Keep original camera sound | Explicit render-contract evidence | Provider replay, real contract |
| Wind-only noise removal | Hold approval; name the missing change | Provider replay, real runtime |
| Typed shadow brightening | Hold approval even when legacy receipt calls it a format limitation | EN/TR typed requirement replay |
| Exact visual moment with no executable window | Hold approval | EN/TR typed requirement replay |
| Existing-video title color + unsupported glow | Hold the entire partial edit | Real Copilot and extractor parsers, editor compiler, runtime/DB |
| Source generation or capability withdrawn | Cancel direct/repeated approval; no dispatch | Real HTTP approval endpoint |
| Report/requirement evidence removed | Reject even after enrollment is disabled | Real DB + HTTP approval |
| Named omission vs generic agreement | Only exact offered choice supersedes that requirement | Real brief persistence and runtime |
| Missing extracted request | Hold approval | Red/green real runtime control |
| Historical flag-off draft | Existing behavior | Real runtime regression |

The EN/TR cases verify that equivalent typed requirements encounter the same gate.
They do not measure a live model's translation or requirement-recall accuracy.
Broader montage/speech/cloud/slide execution proof remains in KRI-722–725.

## Verification

Fresh implementation base: `b3a2a9edbd50a7d1d9a88791526dbf443ecf2e32`.
Scratch PostgreSQL: `nova_kri721_approval_readiness_test`.

- Baseline red: the wind-noise request received approval without executable evidence
  (`/private/tmp/kri721-baseline.log`).
- Scoped-edit red: only Copilot ran, so the remaining request could be omitted
  (`/private/tmp/kri721-scoped-red.log`).
- Phone-source red: the selected phone renderer materialized an original-audio
  default that the new strict gate compared with the pre-normalized proposal
  (`/private/tmp/kri721-phone-red.log`). The writer now canonicalizes against the
  proposed renderer before approval. The reader still refuses later changes.
- Missing-extraction red: no extracted request was accepted as a successful empty
  checklist (`/private/tmp/kri721-empty-brief-red.log`).
- Final focused command: `bash scripts/check-api.sh test tests/kria/test_approval_readiness_postgres.py tests/kria/test_runtime_simpler_version_loop.py tests/kria/test_render_retry_postgres.py tests/kria/test_plan_review.py tests/gates/test_reliability_gates.py -q -x` — **171 passed**.
- `bash scripts/preship-check.sh` — scoped lint/format and **622 selected incident
  regression tests passed**. Dedicated KRI-721 CI journey also selects the new tests.
- CI selector contracts: `python3 -m unittest discover -s scripts/ci -p 'test_*.py' -v` — **66 passed**. The new journey is explicitly included in the shared-planner selection expectation.
- No prompt/model changes or paid model calls. Ordinary first-draft checking adds
  no model call. Previously unextracted scoped chat edits add one bounded call to
  the existing Brief Extractor. Live latency and spend for that extra call are not
  measured here.

Physical approval UI evidence is recorded below separately from exported-output
verification and production deployment.

Final physical recheck is waiting for exclusive iPhone time after the Troya
production journey. Initial physical tests held approval but exposed the phone
normalization mismatch above; they are not claimed as a complete successful flow.
