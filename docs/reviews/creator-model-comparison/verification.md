# Offline verification — 19 October 2026

Base: `ed90bd9ea859129efb1853b327ca00562ecb54a0` from fetched origin/main.
Branch: `codex/creator-model-comparison`; isolated checkout:
`/private/tmp/nova-creator-model-comparison`. Shared original checkout untouched.

**3,724 passed; 15 skipped; no failures.** Provider keys were dummy strings and
NOVA_EVAL_MODE=replay. SDK contract tests used httpx.MockTransport. PostgreSQL tests
used the migrated local task database `nova_creator_model_comparison_test`.
No live provider evaluation, paid judge or production mutation ran.

Repeat:

```sh
NOVA_EVAL_MODE=replay GEMINI_API_KEY=test OPENAI_API_KEY=sk-test ANTHROPIC_API_KEY=test \
  bash scripts/check-api.sh test tests/agents/ tests/evals/ \
  tests/services/test_ai_cost_control.py tests/services/test_text_provider_ledger.py \
  tests/services/test_comparison_capture.py tests/integration/test_comparison_eligibility.py \
  tests/integration/test_request_following_queue.py \
  tests/incidents/test_request_following_private_review.py tests/test_edit_copilot.py \
  tests/test_copilot_outcome_explanations.py tests/services/test_creator_render_contract.py \
  tests/integration/test_actual_export_verification.py \
  tests/integration/test_actual_export_completion.py \
  tests/routes/test_actual_export_publication.py -q --eval-mode=replay -rs
```

The skips are existing live/judge-only checks, fixture applicability, and missing
unrelated template ground-truth/transcript sidecars. They do not establish live
model quality or physical-device export verification.

The initial broad run exposed synthetic reservation leakage from the new provider
ledger tests. Runtime reservation keys are hashes, so the reused fixture's prefix
cleanup missed them. A dedicated attributed-run cleanup fixed the issue. Earlier
synthetic rows were removed only from the task scratch database; the final run
started with zero reservations and passed. No production ledger was touched.

Other checks:
- SDK HTTP contracts: exact settings, one request on timeout/500, cache/thinking usage.
- PostgreSQL: settlement, unknown no-retry fence, aggregate/concurrent budget guards.
- Real admin HTTP + PostgreSQL: wrong admin token, owner mismatch, stale grant,
  protected holdout and revocation rejected; no private body returned.
- Capture: complete context/prompt binding, 600 files, duplicate publication,
  disabled/revoked capture and signed capability removal.
- CLI: freeze, immutable prompt/spec/price binding, offline replay with ledger enabled,
  resume, manifest-bound paid approval refusal, expiry purge and redacted projections.
- Three reference cases x three arms: nine parser/structural passes at $0.
- Scoped ruff and format, git diff whitespace checks, preship reliability gates:
  242 mapped gate checks passed. Journey manifest includes the new comparison paths.

These results verify infrastructure and existing product contracts. They do not
mean any candidate model fulfilled a new request, saved a candidate draft or
rendered a video. See cost-estimate.md and README.md for the remaining live stages.
