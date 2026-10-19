# Offline verification — 19 October 2026

Base: `ed90bd9ea859129efb1853b327ca00562ecb54a0` from fetched origin/main.
Branch: `codex/creator-model-comparison`; isolated checkout:
`/private/tmp/nova-creator-model-comparison`. Shared original checkout untouched.

**4,128 passed; 15 skipped; no failures** after integrating current main. Provider keys were dummy strings and
NOVA_EVAL_MODE=replay. SDK contract tests used httpx.MockTransport. PostgreSQL tests
used the migrated local task database `nova_creator_model_comparison_test`.
This offline run made no live provider calls. The separately approved funded live pilot is recorded in pilot-report.md; no paid judge or production mutation ran.

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
  tests/routes/test_actual_export_publication.py tests/kria/test_editor_state_turns.py tests/kria/test_approval_readiness_postgres.py \
  tests/pipeline/test_unified_montage_spoken_order.py tests/services/test_spoken_roles.py \
  tests/tasks/test_kri585_spoken_order_montage.py -q --eval-mode=replay -rs
```

The skips are existing live/judge-only checks, fixture applicability, and missing
unrelated template ground-truth/transcript sidecars. They do not establish live
model quality or physical-device export verification.

The initial broad run exposed synthetic reservation leakage from the new provider
ledger tests. Runtime reservation keys are hashes, so the reused fixture's prefix
cleanup missed them. A dedicated attributed-run cleanup fixed the issue. Earlier
synthetic rows were removed only from the task scratch database; the final run
started with zero reservations and passed. No production ledger was touched.

The latest broad run completed in 78.83 seconds; its local log is
`/private/tmp/nova-comparison-upstream-final-tests.log`. Typed v2 round trips include
explicit null versus omission. Live pilot observations prompted SDK HTTP controls
that prove one Gemini submission and a matching transport deadline, plus a real
PostgreSQL unknown-outcome fence across separate batches in the same envelope.

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
  439 mapped gate checks passed. Journey manifest includes the new comparison paths.

These results verify infrastructure and existing product contracts. They do not
mean any candidate model fulfilled a new request, saved a candidate draft or
rendered a video. See pilot-report.md for the funded pilot results and outstanding evidence.

Final preship fetch found a new upstream Main Creator v64 camera-audio fix
(`220f75b3f`). It was integrated cleanly before delivery; historical paid rows remain
pinned to v63, and no paid comparison is rerun across that prompt change.

Funded continuation checks: retained full-batch liability preserves unknown status,
counts against envelope caps, permits an independent case and refuses duplicate
submission. Ordinary unreconciled unknowns still stop the envelope. Tests also
reject circular Anthropic schemas, preserve deep Sol values and require explicit
JSON selection for the provider-rejected full Sonnet native grammar. Returned
usage totals reconcile exactly to the durable pilot ledger. Six current Main plans
compiled without notices; Copilot returned no supported edit operations.

Final integration: merged fetched `ebcb9874e` cleanly, preserving the provider
factory in planner and both environment/journey additions. The post-merge suite
includes all three new spoken-order backend test files. Selector verification:
41 tests and 168 subtests passed. Model prompts did not change in this integration;
no new paid request ran. Pure candidate compilation was repeated with zero failures.
Upstream iOS changes were inherited; no new independent device verification is claimed.

After that full integration run, a final ledger-only hardening bound the retained
unknown to its protected case/model identity across freshly frozen manifests.
The final focused ledger/workflow suite passed 47 tests in 0.99 s; it rejects
missing case attribution and fresh-ID duplicates while preserving independent
cases and budget counting. No paid call followed this hardening. The original
unknown receipt was updated with the protected identity without changing money.
