# Main Creator / Edit Copilot comparison

The provider adapters and private development experiment workflow are implemented.
Production defaults remain Gemini 3.1 Pro, high thinking, with existing retry policy.
No paid calls, deployment, model selection or human intent approval was performed.
The full experiment and native schema improvement are **not complete**.

Read [verification](verification.md), [cost estimate](cost-estimate.md),
[plan](plan.md), [failure cases](failure-matrix.md), and
[native-schema limitations](schema-feasibility.md). KRI-727 remains the acceptance
context: judge request fulfillment independently of parsing and recorded exports.

## What is implemented

- OpenAI Responses (`gpt-6.1-sol`, store=false) and Anthropic Messages
  (`claude-sonnet-5-5`), explicit reasoning, no SDK retries, visible text only.
  Both text adapters reject media/foreign Gemini URIs before sending anything.
- Reservation and usage settlement through the existing PostgreSQL ledger for all
  three providers; separate cache/read/write and inclusive reasoning accounting.
  Transport failures and 5xx retain unknown outcomes; a concrete 429 can retry.
- Independent environment settings: MAIN_CREATOR_MODEL / MAIN_CREATOR_THINKING_LEVEL,
  EDIT_COPILOT_MODEL / EDIT_COPILOT_THINKING_LEVEL. Defaults are unchanged.
  Main Creator's Gemini fallback is disabled when explicitly configured to another
  provider. No production switch is recommended until the experiment passes.
- Input/output observability redaction for sensitive agents; Copilot now opts in.
- Default-off exact capture after input validation, before model submission, in
  the shared runtime reached by both invocation paths. Bound to owner, training
  grant, scope, prompt/spec/input hashes, timestamps and 30-day expiry. Files are
  exclusive, private and outside Git; signed capabilities make captures incomplete.
- A read-only admin eligibility endpoint that discloses no source bodies, rechecks
  ownership/current grant/deletion/scope, and excludes current/historical holdouts.
- Frozen manifests, deterministic arm shuffling, fresh instances, replay/dry-run,
  first-attempt/eventual parsing, latency, usage, private results, aggregate reports,
  durable unknown fences, manifest-bound <=$2 live approval, expiry/revocation purge.

## Operator commands

Use the API virtual environment; the wrapper reads root .env without printing keys.
All examples below are offline. Create an external private directory first:

```sh
mkdir -m 700 /private/tmp/nova-comparison-operator
src/apps/api/.venv/bin/python scripts/compare_creator_models.py --help
src/apps/api/.venv/bin/python scripts/compare_creator_models.py schema-audit \
  --output /private/tmp/nova-comparison-operator/schema.json
src/apps/api/.venv/bin/python scripts/compare_creator_models.py freeze \
  --case src/apps/api/tests/fixtures/agent_evals/main_creator/montage_strategy.json \
  --arm G0:gemini-3.1-pro-preview:high:json:legacy \
  --arm S:claude-sonnet-5-5:high --arm O:gpt-6.1-sol:medium \
  --output /private/tmp/nova-comparison-operator/development.json
src/apps/api/.venv/bin/python scripts/compare_creator_models.py dry-run \
  --packet /private/tmp/nova-comparison-operator/development.json
src/apps/api/.venv/bin/python scripts/compare_creator_models.py replay \
  --packet /private/tmp/nova-comparison-operator/development.json \
  --directory /private/tmp/nova-comparison-operator
```

`report --journal <private journal>` returns aggregate metrics, no creator excerpts.
`purge --packet <packet> --directory <private directory>` removes expired copies and
run bodies; `--check-eligibility` also rechecks the production eligibility service.
Schedule/operator-run this purge while holding copies; no recurring task was created.
A missing eligibility service fails closed. Review-packet and journal files are 600;
parent directories must already be 700. Do not upload private artifacts to public CI.

Replay directly parses recorded responses through authoritative agent parsers and
structural checks. It never reserves cost or constructs provider clients. Runtime
retry/accounting behavior is tested separately using actual SDK HTTP mocks and
real scratch PostgreSQL. Replay across model labels is not a live model comparison.

## Before any live batch

1. Merge/deploy through the normal approval workflow. No deployment was requested
   for this phase. The eligibility endpoint must be deployed before it can be used.
2. Configure a durable private capture directory outside the checkout; enable
   CREATOR_COMPARISON_CAPTURE_ENABLED only for explicit
   CREATOR_COMPARISON_CAPTURE_USER_IDS. Existing training eligibility and collector
   scope are additional requirements; the allowlist is not a consent grant.
3. Capture complete actual invocations. Capture never fetches media, changes drafts,
   approves wording, or renders exports. Protected holdouts are intentionally skipped.
4. A human reviews each exact input and source pointers, then supplies an
   `intent_review` with `decision: approved`, `reviewed_by`, matching `input_sha256`
   and nonempty `expectations`. The assistant has not supplied these attestations.
5. Freeze new packets from those private records. Recompute cost from their actual
   prompts. Historical fixtures cannot pass the live exact-capture eligibility gate.
6. Obtain approval for that manifest hash and its cost. Live additionally requires
   NOVA_EVAL_MODE=live, development usage environment, enforced cost control and paid
   attribution. It refuses estimates above the approved <=$2 cap. There is no bypass.

The live command is `live --packet ... --directory ... --approval-sha256 ...
--approve-max-usd ...`; these flags are not approval from this assistant. Every
provider submission rechecks current eligibility, including legacy retries. Unknown
submissions stop the run and remain blocked on resume until reconciled. Never delete
an unknown ledger row or change run identity to obtain a retry.

## Remaining experiment work

The production queue read on 19 October had 25 visible records, 24 unreviewed and
one approved. That approved record contains events, plans and saved job state,
not original agent input/prompt snapshots. It cannot be represented as exact replay.
The corpus target (20 development / 30 supported holdouts / ambiguity and recovery
controls) is not met. Existing fixtures include 61 Main Creator and 179 Copilot
cases, many hand-authored; their provenance remains unchanged.

A restricted held-out executor is not enabled. The development path rejects those
bodies. Build and review the separate restricted path, freeze arms and prevent
creator/media-event overlap before interpreting any holdout score. Full native
schemas require the bounded wire-contract work above. Live results need human
obligation scoring/calibration and downstream compiler/save/reload/actual-export
verification. Existing tests check those authorities; this runner reports inference
only and never claims it saved or rendered a candidate response.
