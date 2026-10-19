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
