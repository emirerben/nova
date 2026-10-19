# Main Creator / Edit Copilot comparison

The provider integration, typed output transport, private request workflow and sealed
holdout runner are implemented. The approved $5.66 pilot ran partially and is
**blocked**, with no model recommendation or production change.

Read the [pilot report](pilot-report.md), [verification](verification.md),
[schema details](schema-feasibility.md), [cost record](cost-estimate.md) and
[original plan](plan.md). KRI-727 remains the acceptance context: assess request
fulfillment independently of parsing and recorded exports.

## Implemented

- Gemini, Anthropic Messages (`claude-sonnet-5-5`) and OpenAI Responses
  (`gpt-6.1-sol`, store=false), with independent per-agent model/reasoning/output
  controls. Production defaults remain Gemini high, JSON, existing runtime retry
  policy; native output is opt-in.
- Typed model-owned Main Creator fields and all 66 registered Copilot operations,
  preserving authoritative product parsing, identity and execution checks.
- Explicit SDK retry/deadline control, metered reservations, normalized usage,
  per-batch caps and one shared approved envelope. Unknown outcomes retain money
  and prevent another paid submission across that envelope.
- Default-off exact invocation capture, separately labeled historical input recovery
  and editor-context reconstruction, current owner/consent/expiry checks, private
  files and redacted observability. No creator text is committed here.
- Immutable manifests, replay, dry-run, blinded private review packets, resume
  fences and expiry/revocation purge including registered derived copies.
- Separate sealed holdout registration/execution and evaluator eligibility token;
  creator/media-event split overlap is rejected. Ordinary development readers deny
  holdout data. The executor returns aggregate metrics without output bodies.

## Operator workflow

Use `src/apps/api/.venv/bin/python scripts/compare_creator_models.py --help`.
Commands include `schema-audit`, `freeze`, `dry-run`, `replay`, `live`, `report`,
`purge`, `seal`, `sealed-run` and `approve-envelope`. External private directories
must be mode 700; input, packet, journal and review files are mode 600.

Replay uses recorded responses through the real parsers; it never constructs an
SDK client or reserves money. Live requires `NOVA_EVAL_MODE=live`, development
attribution, cost control, current eligibility and manifest-bound approval. Batches
remain capped at $2 even within an approved envelope. Keep the same envelope ID
when superseding a preflight; prior costs and unknown reservations still count.
Runtime drift invalidates frozen profiles and requires a new pinned manifest.

For prospective production capture, deploy through the normal approval workflow,
configure durable private directories and explicit user allowlists, and preserve
existing training eligibility. The allowlist is not a consent grant. Holdouts
require their separate directory/token. Historical reviewed source imports can
instead recheck the existing review-export endpoint; these do not require deploying
the new capture endpoints. Source review is retained as source review, never
presented as fresh human approval of a reconstructed input.

Run `purge --packet ... --directory ... --check-eligibility` while retaining copies.
It removes expired/revoked bodies and registered derived files. No recurring purge
or production capture was activated. Never upload private packets to public CI.

## Remaining experiment evidence

Two exact recorded Main inputs and one reconstructed Copilot context are ready,
all from one reviewed creator/source. The holdout queue has no eligible cases.
The reconstructed Copilot preference remains pending. Historical prompts were not
fully archived; Main inputs were evaluated with pinned current v63 prompts.

Sonnet needs a funded configured account or an explicitly selected existing funded
endpoint. The timed-out Gemini receipt needs authoritative usage reconciliation;
never release an unknown reservation or retry it merely to unblock the experiment.
Human obligation scoring, live Copilot outputs, holdout repetitions and finalist
save/reload/export review remain outstanding. Three valid compiled plans establish
inference compatibility only; their commands were never executed.
