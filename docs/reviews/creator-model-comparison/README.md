# Main Creator / Edit Copilot comparison

The approved real-request inference pilot remains within $5.66. **Production remains
Gemini; no quality winner is established.** The latest
[non-lip-sync continuation](next-content-results.json) ran six calls on two exact
real inputs: all three Main plans parsed/compiled, and two Copilot edits passed all
ten preservation checks while the third response failed JSON parsing. Human blind
preferences and actual export verification remain pending.

Current metered spend is **$1.168546**, with **$1.716306** retained unknown liability;
**$2.775148** remains. Read the initial [pilot report](pilot-report.md),
[aggregate results](pilot-results.json), [verification](verification.md),
[schema details](schema-feasibility.md) and [cost record](cost-estimate.md).

In the initial pilot, Main Creator's current v64 JSON pair parsed and compiled for Sonnet and Sol.
Sonnet was faster and costlier on two related inputs. Copilot's one reconstructed
unsupported-operation case parsed for Gemini and Sol; Sonnet JSON returned invalid
JSON. Full Sonnet native grammar was rejected by the provider and now fails local
preflight. Sol native passed all tested inputs. These are compatibility and latency
observations, not a request-fulfillment or export-quality verdict.

## Implementation

- Gemini, Anthropic Messages and OpenAI Responses (store=false), independent
  per-agent model/reasoning/output controls; production defaults unchanged.
- Typed model-owned Main fields and 66 Copilot operations; explicit null/omission
  semantics, original parsers/compiler/ownership checks. Full native Sonnet is
  blocked pending a verified capability-scoped grammar; JSON is explicit.
- Per-call accounting, SDK retry/deadline controls, $2 batches and one shared
  approved envelope. Unknown calls remain fenced and counted. A terminated call
  with documented conservative full-batch retention may permit independent cases;
  no caller-context flag can release or bypass the original reservation.
- Default-off private exact capture, labeled historical/reconstructed imports,
  current eligibility checks, blinded review and derived-copy expiry purge.
- Separate sealed holdout registration/execution and evaluator token; development
  access rejects holdouts and split overlap. No real holdout corpus was available.

Operator entrypoint: `src/apps/api/.venv/bin/python scripts/compare_creator_models.py
--help`. Private directories are 700 and files 600. Replay never constructs SDK
clients or reserves money. Live requires pinned manifests, current eligibility,
explicit approval and the durable development ledger. Reuse the same envelope ID.
Never retry unknown submissions under fresh identities. See plan.md for collection,
scoring and actual-export boundaries.

## Evidence and budget

Initial-phase confirmed spend was **$0.728250**, retained unknown liability **$1.716306**,
combined **$2.444556**. Original Gemini actual usage is still unknown; the full original batch remains
reserved rather than falsely settled. All returned usage reconciles to the ledger.
Source scope: one creator/event, two recorded Main inputs, one approved reconstructed
Copilot context, zero live holdouts. Candidate commands were never executed.

The current readable private review is
`/private/tmp/nova-model-comparison-private/readable-blind-review.html`;
`funded-blind-review.html` preserves the earlier pilot.
Human scoring, supported Copilot tasks, independent holdouts and finalist exports
remain outstanding. Run expiry/revocation purge while retaining private copies;
no recurring purge, capture cohort or production deployment was activated.

The [review and corpus preparation](review-and-corpus-preparation.md) now provides
a readable private blind board, four selected non-lip-sync Main inputs and eight
exact supported Copilot development inputs with offline parser/compiler checks.
Turn-specific human grading remains pending. The independent holdout intake is
prepared, with zero cases enrolled. Lip-sync is excluded from creative-direction
ranking after the user's feedback; its technical criterion is song timestamp matching.
