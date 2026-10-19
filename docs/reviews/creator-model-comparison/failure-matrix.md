# Acceptance criteria recorded before implementation

2026-10-19; base ed90bd9ea859129efb1853b327ca00562ecb54a0; no paid calls approved.

| Failure | External check |
| --- | --- |
| SDK silently retries accepted work | Actual SDK + HTTP mock records exactly one timeout/500 submission; outcome unknown fences fallback |
| Missing, duplicate or mispriced usage | Provider fixtures cover reasoning included in output, cache reads/writes, long context; PostgreSQL reservation settles once |
| New provider bypasses budget | Agent run through real PostgreSQL ledger; refused reservation makes zero HTTP calls |
| Refusal/truncation parsed as successful edit | Normalized finish status checked before parser; no completed outcome |
| Provider receives unsupported video or parameters | Preflight fails before HTTP; explicit model allowlist and reasoning controls |
| Native schema silently weakened | Full schema retained and checked locally; unsupported open maps/complexity rejected explicitly, JSON mode labeled separately |
| Private creator text leaks | Sensitive agent input/output logs contain only keys and hashes; private fixtures/artifacts mode 600 outside repository |
| Cases change under a resumed run | Immutable input/prompt/spec hashes; run identity includes arm/case/repetition; attempt status journal prevents duplicate submissions |
| Consent revoked, expired, re-granted, holdout exposed | Recheck current eligibility before every live call; exact grant key and expiry; sealed holdout excluded from development |
| Offline result mistaken for quality evidence | Offline report identifies replay/mocked transport; no winning model or actual-export claim |
| Changes alter production defaults or executor authority | Existing creator/copilot replay suites pass; instance specs do not modify class specs; no runtime/approval/compiler changes |

Use existing replay and real PostgreSQL integration coverage, SDK HTTP contract tests,
and runnable CLI artifacts. Human intent reviews and final-export checks remain independent
requirements; a parser pass is not request fulfillment.

## Approved continuation — 2026-10-19

The user approved $5.66 total for the priced pilot and requested the remaining
schema, holdout, capture and live comparison work. Before implementation:

- Every current replay response must survive typed-wire encode/decode without
  losing model-owned values; server-owned fields stay outside model wire contracts.
- Optional presence uses a typed 0-or-1 array, not JSON-in-a-string, avoiding
  provider optional/nullable-union limits. Local cardinality/semantic validation
  remains mandatory; provider projection lists every locally enforced constraint.
- Copilot operation names, fields and scalar/container types must be closed;
  unknown operations/keys fail before execution. Dynamic metadata is explicitly
  typed JSON and never substitutes for an operation schema.
- Held-out bodies never pass development freeze/replay/report paths. A sealed
  registration pins arms, source groups and source hashes before execution;
  development/holdout creator and media-event overlap is rejected. Only aggregate
  metrics leave sealed execution; normal review/export routes remain unchanged.
- All paid batches share an atomic $5.66 envelope in addition to their <=$2
  PostgreSQL run caps. Rejected requests, unknown work and restart fences count.
- Reconstructed real context is labeled separately from exact historical input.
  No human review is fabricated; source-reviewed expectations retain their original
  pointer and review identity. Unreviewed tasks cannot become a quality verdict.
# Retained-liability continuation, preregistered before implementation

- No usage receipt: preserve `unknown`, reserve the full original approved batch,
  label actual billing unknown, and never retry that logical submission.
- A still-running call, wrong envelope, decreased liability, invalid evidence hash,
  or retention below the locally established maximum: refuse reconciliation.
- Reconciled unknown remains counted across batches and concurrent reservations;
  independent cases may continue, but the original idempotency key stays fenced.
- An ordinary unknown without this durable reconciliation still stops the envelope.
- A later real receipt may settle actual usage; a conservative hold is never
  reported as metered spend or successful inference.

## Anthropic circular-schema correction, fixed before implementation

- Provider rejects recursive dynamic JSON: detect reference cycles offline; preserve known rejections.
- Bounded projection changes valid values: declare the eight-level dynamic-JSON restriction explicitly; original local schema remains unchanged.
- Branch tags or maps change during projection: shallow values and all existing fixtures must retain the original codec and satisfy the provider projection.
- Sol/Gemini lose recursive support: their schemas continue accepting deeper values.

- Known full Sonnet grammar rejection: reject native profile locally; only an explicitly selected JSON profile can run, with all earlier rejections retained. No automatic format fallback.

- A fresh packet/request ID repeats the financially contained unknown case on the same model: reject using the protected case/model identity, even though independent cases may proceed. Missing case attribution also fails closed.

## Overnight completion acceptance, fixed before changes

- Recorded Copilot imports must bind the owned captured job and exact creator turn;
  wrong owner/job/agent, missing or nested redactions, altered input, ambiguous time
  windows and incomplete editor state must fail. No current editor reconstruction
  may silently replace a recorded input or acquire a human quality verdict.
- A prompt-only JSON transport guard must be a separately named, versioned opt-in
  profile. Existing JSON/native profiles and their historical failures stay intact.
  Frozen hashes include the guard, estimates count its tokens, and unparsed responses
  remain failures rather than being repaired or retried silently.
- Scratch persistence must refuse every non-local or non-`_test` database before
  writes. Use isolated synthetic owners and real draft services; prove committed
  reopen equality, stale-write rejection and undo, then remove the owned scratch rows.
  Never dispatch a render/broker or touch the paid pilot ledger database.
- State obligations are independently specified from the creator request and bound
  to its input hash. Report deterministic checks separately from human preference
  and picture/audio review. A missing/failed output cannot disappear from a denominator.
- Source shortages and unavailable exports remain incomplete evidence. Broad work
  approval does not create training consent, new independent creators, or human scores.

## Timing comparison boundary

The final timing cases evaluate existing operations; they do not introduce a new
editor timing feature or change product projection rules. The six timeline
choices remain those of the production compiler:

1. Duration is owned by the ordered compiler timeline, not model prose. Compare
   the compiled slots and total with the recorded input and source bounds.
2. Existing `compile_editor_ops` ripple/projection owns affected text lanes;
   continuous audio is not rewritten by a text/clip-duration request. Check each
   returned payload for unrelated lane changes.
3. This experiment has no scrub/playback surface. Existing virtual-timeline and
   editor browser gates retain canonical duration and inverse mapping coverage.
4. Duration edits trim/extend only within existing compiler/source limits. A
   model cannot relax minimum lengths, source bounds or transition rules.
5. One candidate is one draft snapshot; verify exact prior-state undo and a
   separate-session restored reopen. No model op is applied to production.
6. Existing `make verify-editor-timeline` and the editor Save matrix provide
   product-contract regression evidence. They do not prove these private
   candidates were rendered, previewed on a phone or exported successfully.

Before final hardening: a PostgreSQL URL can say `localhost/*_test` in its main
fields while query options override the host, host address, database or service.
The scratch verifier must reject those DSNs before writes, not trust only the
parsed host/database fields. No query overrides are needed by the task scratch URL.

Before final source hardening: Main imports must also reject a wrong agent/item,
missing/naive invocation or event time, an invocation outside the creator turn,
nested redactions and validation that changes the recorded input. A normalized
historical record must not remain eligible as an exact-input comparison. Audit the
prepared/pilot inputs without rewriting historical journals or making paid calls.

Final SDK audit, before the configuration fix: the qualified Google SDK is 2.10.0,
so the dependency declaration must not permit an unqualified 1.x runtime. The
qualified OpenAI 2.44 Responses effort contract has `xhigh` but no `max`;
Sol `max` must fail before SDK construction and experiment pricing. Keep the
Anthropic contract's separate effort set and never silently translate a label.
