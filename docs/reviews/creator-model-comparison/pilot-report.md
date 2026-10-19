# Initial funded real-request pilot — 19 October 2026

Historical phase report. Provenance correction: one initial Main context was
normalized during validation and is reconstructed, not an exact historical input.
See `provenance-corrections.json`; original inference bytes and money are unchanged.
Current scope, accounting and evidence are in
[morning report](morning-report.md) and `final-development-results.json`.

The approved inference pilot ran within budget. **Keep production unchanged.**
Sonnet is a promising Main Creator latency candidate in JSON mode; Sol has the
more consistent output-format evidence here. No quality winner is established.
The sample contains two related Main inputs and one reconstructed Copilot control
from one creator/source, with no independent holdouts or human output scores.

## Current Main Creator JSON comparison

Both models saw the same v64 prompt and input per row; Sonnet used high effort,
Sol medium. All four plans parsed on the first attempt and passed the real pure
strategy compiler with zero notices. No commands were executed.

| Input | Sonnet JSON | Sol JSON |
| --- | --- | --- |
| Correction | 14.247 s; $0.065110 | 18.061 s; $0.039404 |
| Original request | 8.560 s; $0.056074 | 15.427 s; $0.036864 |

Sonnet was faster and more expensive on both observations. These are paired
measurements, not p95 estimates or evidence of creative/request-following quality.
All recorded cache-token counts were zero. Sequential order, cold grammar work,
provider load and different reasoning settings limit attribution.

Sol's native v2 controls also parsed/compiled: 30.504 s / $0.052254 and
27.832 s / $0.052014. Native output was slower and costlier on this pair; quality
benefits remain unassessed. JSON/native instructions and serialization differ.

The earlier Gemini baseline used v63 and the earlier transport implementation:
one parsed/compiled plan at 54.201 s / $0.064488, and one timeout at 131.423 s.
It is historical evidence, not a matched v64 latency arm. The unknown submission
was never retried. Earlier Sol v1 results and every rejection remain in
`pilot-results.json`; they are not replaced by the later controls.

## Edit Copilot

The context was reconstructed with the real server snapshot builder from eligible
persisted prior job state; the user explicitly approved labeled reconstructions.
It elicited unsupported-operation responses, so this case measures refusal/format
behavior, **not supported edit execution**.

| Profile | Result | Elapsed | Metered cost |
| --- | --- | ---: | ---: |
| Gemini JSON, high | Parsed unsupported reply; no ops | 11.655 s | $0.058782 |
| Sol JSON, medium | Parsed unsupported reply; no ops | 7.017 s | $0.046030 |
| Sonnet JSON, high | Completed response was not valid JSON; parser rejected | 5.735 s | $0.073256 |
| Sol native v2, medium | Parsed unsupported reply; no ops | 12.426 s | $0.084646 |

The Sonnet format failure remains a paid first-attempt failure. It was not repaired
or quietly retried. A plausible prose answer does not satisfy the runtime contract.
Human review must still assess whether each refusal preserved the actual request
and accurately described the available capabilities.

## Native Sonnet boundary

Funding resolved the account-credit problem. Full native Main/Copilot schemas then
failed before inference: three circular-reference HTTP 400s, followed by three
compiled-grammar-size HTTP 400s after an acyclic typed projection. Those reservations
were released at $0; the earlier insufficient-credit rejection is also preserved.

The projection explicitly bounds dynamic JSON to eight nested containers and keeps
local semantic validation. Sol/Gemini retain recursion. This addresses circularity
but does not make the full grammar acceptable to Sonnet. Full Sonnet native profiles
now fail locally before network contact. JSON was a separately frozen explicit
profile, not an automatic fallback. Capability-scoped native grammars remain future
work; do not advertise full Sonnet native output as production-ready.

## Spend and Gemini reconciliation

- Approved total: **$5.660000**, same envelope UUID throughout.
- Confirmed, receipt-reconciled inference spend: **$0.728250**.
- Conservatively retained Gemini liability: **$1.716306**.
- Combined accounted budget: **$2.444556**; remaining after the hold: **$3.215444**.
- No active provider-started/reserved call remains. Each new batch stayed below $2.

Google returned no matching Cloud request logs; the local billing export was not
configured. The old request ended after 2,372,988 ms in ReadError without usage.
A transport error is not evidence of an unbilled request. Google's
[billing documentation](https://ai.google.dev/gemini-api/docs/billing) distinguishes
known failed HTTP requests from returned usage; no known HTTP status was available
for this call.

Reconciliation therefore retained the **entire originally approved Gemini batch**,
increasing the hold from $0.433948 to $1.716306. Local source/SDK inspection established
a single SDK attempt; a byte-based input allowance plus two complete output budgets
estimated $0.517458, below that retained batch. This is financial containment,
not authoritative actual usage. The row remains `unknown`, counts against every
budget, and its idempotency key and case/model identity remain fenced across new manifests. Durable reconciliation permits
independent cases only; it never releases money or marks the unknown call successful.
A future authoritative receipt can settle it. Do not clear it because it is old.

Automatic approval review rejected resending the exact creator prompt to the token
counting endpoint; no such request was made. The evidence used for continuation
was local-only. No keys, source text or model bodies are committed in this report.

## Improvements and verification

Implemented provider adapters, independent model/reasoning/output controls,
private capture/import, current eligibility checks, expiry/revocation purge,
sealed holdout registration/execution, typed model-owned schemas, and shared-envelope
accounting. Gemini now has explicit per-request retry/deadline controls. The SDK
mock demonstrated that inherited retry configuration could escape accounting;
inspection found the historical default client had retries disabled. No historical
internal retry is claimed as observed.

**4,128 offline tests passed; 15 skipped; preship gates green** (439 mapped incident
checks; 43 mapped journeys). Upstream integration also passed 41 selector tests (168 subtests); final ledger/context hardening passed 47 focused tests. Six current Main plans passed pure compilation with
zero notices. Copilot returned no edit operations to compile. Candidate saves,
renders, exports and production model changes were not performed.

## Decision and next evidence

Retain Gemini in production. Shortlist Sonnet JSON for Main Creator latency and
Sol for continued Copilot evaluation, subject to quality review. Native Sol is
usable on these inputs; its quality benefit over JSON has not been demonstrated.
Sonnet needs an enforceable output contract before it is a reliable Copilot option.

Review the private randomized packet:
`/private/tmp/nova-model-comparison-private/funded-blind-review.html`.
It contains seven primary candidate outputs across three panels, including Sonnet's
unparsed Copilot response. Human scores are not prefilled. Source-review expectations
are preserved as source review, not relabeled as a human verdict on new outputs.

A selection-grade experiment still requires supported Copilot requests, independent
creator/event holdouts, repeated runs, human obligation scoring, save/reload and
actual export evidence. None can be inferred from parser success or this one-source
pilot. No larger experiment or rollout is authorized by the remaining pilot budget.
