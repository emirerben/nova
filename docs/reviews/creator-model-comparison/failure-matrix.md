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
