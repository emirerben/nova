# Partial live pilot — 19 October 2026

**No winner; comparison incomplete.** The approved $5.66 pilot produced three
parseable Main Creator plans. Sonnet was blocked by insufficient account credits;
a Gemini request timed out with unknown billing, stopping further paid work.

| Agent/input | Gemini high, current JSON | Sonnet high, native v1 | Sol medium, native v1 |
| --- | --- | --- | --- |
| Main: correction input | Parsed, 54.2 s, $0.064488 | Account credit rejection, no inference | Parsed, 27.8 s, $0.050764 |
| Main: original input | Timeout, 131.4 s, $0.433948 held | Blocked without submission | Parsed, 17.8 s, $0.048564 |
| Copilot: reconstructed context | Not run | Not run | Not run |

Times are runner elapsed times. All three parsed responses passed the real pure
strategy compiler with zero notices. No commands, persisted candidate edits or
renders were executed. Request fulfillment remains unscored pending human review;
parsing and compilation do not establish quality.

**Settled $0.163816; unknown held $0.433948; combined $0.597764.** A separate durable
local pilot ledger retains all paid receipts. The shared envelope is
`e8a5c866-dd24-426e-8322-0b6a1789488b`; do not reset its identity or retry the unknown
request. Each batch remains capped at $2. See `pilot-results.json` for aggregate
rows and `pilot-preregistration.json` for the pre-call frozen configuration.

## Source and interpretation limits

Two complete recorded Main inputs were recovered from one human-reviewed eligible
production source. The only recovered redaction was an empty field proven by its
zero length and empty-string checksum. These are exact recorded inputs, evaluated
with the pinned current v63 prompt; the original full prompts were not archived.

One Copilot context uses the real correction and the real server snapshot builder
on persisted prior job state. It is explicitly reconstructed, not an original
Copilot invocation; the user's preference remains pending. All three tasks share
one creator/source. No live held-out case exists. The sealed executor is
implemented but has no corpus to assess.

Sol's two successful responses and observed latency are useful integration
observations. They do not establish superior quality or reliability. Provider,
reasoning, output transport and retry policy differ; their effects are not isolated.
Timeouts and blocked rows remain visible. No production model setting changed.

## Accompanying improvements

The typed schema covers model-owned Main fields and 66 Copilot operations. Latest
v2 preserves explicit null versus omission; the paid responses used v1. Private
capture/import, source eligibility, consent/expiry purge, blinded review and sealed
holdout registration/execution are implemented. Cost enforcement covers the shared
approval across batches and refuses new submissions after any unknown outcome.

The pilot also exposed Gemini SDK retries outside the runtime ledger and a
transport request that could outlive the runtime deadline. SDK retries are now
disabled and transport timeouts match the runtime budget, with recorded HTTP
coverage. These changes remain unshipped. The accepted old request remains held;
new timeout controls do not retrospectively prove it was cancelled or unbilled.

## Required to finish

1. Reconcile authoritative usage for the exact timed-out Gemini receipt; preserve
   its timeout as an observed failure even if usage later arrives.
2. Restore Anthropic credit or explicitly select an existing funded route.
3. Resolve reconstructed Copilot input use or collect a fresh exact invocation.
4. Freeze updated runtime/schema profiles inside the same remaining approval,
   retaining previous costs and failures. Complete missing live arms, then human
   obligation scoring using the private blinded packet.
5. Collect independent supported holdouts before the preregistered repeated
   reliability comparison. Finalists still need save/reload and actual export
   verification. This pilot alone cannot authorize a model rollout.

The private script-free review packet is
`/private/tmp/nova-model-comparison-private/pilot-blind-review.html`; detailed
source bodies and outputs remain outside Git in mode-600 files under a mode-700
directory. The packet contains randomized labels and no recorded human scores.
