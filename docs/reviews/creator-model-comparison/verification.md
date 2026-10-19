# Final verification — 19 October 2026

Task branch: `codex/creator-model-comparison`; isolated checkout:
`/private/tmp/nova-creator-model-comparison`. Integrated fetched main `ef671ddca`
(suggestion-chip runtime/daily-cap additions) after `db31fbe47` (editor Save matrix).
The shared checkout and physical-device sessions were left intact.

## Offline integration

**5,122 passed, 15 skipped, seven expected failures, zero unexpected failures** in
88.61 seconds. Dummy provider keys and `NOVA_EVAL_MODE=replay`; provider contracts
use actual SDKs with HTTP mock transports. PostgreSQL uses only the disposable
`nova_creator_model_comparison_test` database. The paid ledger is a separate database.

The final run covers all agent and replay-eval suites, accounting/provider ledger,
private capture/eligibility, sealed holdouts, recorded-source imports, JSON profile
selection, real draft persistence, request-following review, creator render contracts,
actual-export verifier/publication guards, editor Save matrix, draft/approval fences,
spoken-order regressions and the new upstream suggester guards/PostgreSQL caps.

Reproduce the selected run from the task root:

```sh
NOVA_EVAL_MODE=replay GEMINI_API_KEY=test OPENAI_API_KEY=sk-test ANTHROPIC_API_KEY=test \
  bash scripts/check-api.sh test tests/agents/ tests/evals/ \
  tests/services/test_ai_cost_control.py tests/services/test_text_provider_ledger.py \
  tests/services/test_comparison_capture.py tests/services/test_suggestion_chips_guards.py \
  tests/services/test_suggestion_chips_postgres.py \
  tests/integration/test_comparison_eligibility.py \
  tests/integration/test_comparison_development_inventory.py \
  tests/integration/test_comparison_persistence.py \
  tests/integration/test_request_following_queue.py \
  tests/incidents/test_request_following_private_review.py tests/test_edit_copilot.py \
  tests/test_copilot_outcome_explanations.py tests/services/test_creator_render_contract.py \
  tests/integration/test_actual_export_verification.py \
  tests/integration/test_actual_export_completion.py tests/routes/test_actual_export_publication.py \
  tests/routes/test_editor_save_contract_matrix.py tests/kria/test_editor_state_turns.py \
  tests/kria/test_approval_readiness_postgres.py tests/pipeline/test_unified_montage_spoken_order.py \
  tests/services/test_spoken_roles.py tests/tasks/test_kri585_spoken_order_montage.py \
  -q --eval-mode=replay -rs
```

Skips are existing live/judge-only checks, fixture applicability and missing unrelated
sidecars. The seven strict expected failures come from upstream suggestion-chip
validator-gap fixtures. They are not silently counted as passes. Before that upstream
integration the selected suite passed 4,215 tests with 15 skips.

Logs: `/private/tmp/nova-comparison-final-integration-tests.log` and
`/private/tmp/nova-comparison-final-selector.log`. Selector checks: **41 tests pass**.
Scoped Ruff/format, whitespace, release metadata, journey mapping and preship
reliability checks are recorded in `/private/tmp/nova-comparison-morning-preship.log`;
439 mapped incident checks passed. The manifest conflict from two appended journeys
was resolved by preserving both complete entries.

## Editor timing regression gate

The default `make verify-editor-timeline` passed 209 frontend checks but its default
browser concurrency produced six desktop failures and three flaky cases, primarily
navigation/load timeouts. No frontend product file differs from integrated main.
The initial failure log is retained; it is not reported as a green default invocation.

Running the same browser assertions with `--workers=2 --retries=0` passed:

- **28 desktop browser cases**, 40.8 seconds.
- **42 mobile browser cases**, 45.8 seconds.
- **582 backend timing checks**, 22.89 seconds.
- Frontend `npx tsc --noEmit`, exit zero with no output.

Commands for the browser override, from `src/apps/web`:

```sh
CI=true npx playwright test --project=desktop-editor --workers=2 --retries=0
CI=true npx playwright test --project=iphone13 --project=iphone14 \
  mobile-video-editor.spec.ts --workers=2 --retries=0
```

The backend half used the exact nine files in the Make target, through
`scripts/check-api.sh`, on the scratch database. Logs:
`/private/tmp/nova-comparison-editor-timeline.log`,
`/private/tmp/nova-comparison-desktop-serial.log`,
`/private/tmp/nova-comparison-mobile-timeline.log`, and
`/private/tmp/nova-comparison-backend-timeline.log`. Browser reports/traces are local
worktree artifacts. These are synthetic product regressions, not real candidate exports.

## Returned real-candidate evidence

The recent phases have 25 calls on seven real requests, including profile controls
and one native smoke. Twenty returned parsed outputs; **17 actionable candidates**
passed real scratch committed reopen, stale-write rejection, exact undo, restore,
reopen and cleanup. Three hook questions/options have no executable draft and are
marked not applicable. Five unusable responses remain in the denominators.

A real clip-label response exposed a synthetic fixture bug: cut-to-media identity
was dropped. A new acceptance failed first, then the fixture reconstruction was
fixed. A genuinely wrong clip still fails the drift guard, and creator-edited label
provenance is retained. Original live statuses were not rewritten; source-context
compilation and persistence are separate receipts against the same returned bytes.
Another acceptance caught server `json_guarded` configuration mistakenly selecting
native transport; the fix preserves the exact explicit selection and legacy defaults.

State checks are source-bound operator proposals, with human review pending. They
retain extra fade entrances and fade-out phases as preservation findings. Word timing
charts show exact adjacent overlaps instead of inferring visual fulfillment.
The owned native Save feasibility probe was refused by the local
`guided_story_editor_v2_disabled` gate; no gate was toggled to claim export evidence.

Private pages were checked with gstack at 1440 px and 320 px: eight panels, 24 slots,
two word charts and twenty prepared requests; no horizontal overflow, external
resources, console errors or visible provider identities. Raw responses and mappings
remain outside Git. Derived files are registered for conservative cleanup. The
loopback server checks current eligibility before serving page bodies; direct HTML
files still require a separate eligibility/purge check before reuse.

## Limits

No production routing, collection cohort, selected-model rollout or deployment was
activated. No paid judge ran. No candidate was rendered or watched. No independent
real holdout or human score was fabricated. See `morning-report.md` for the resulting
selection limits and `final-development-results.json` for reconciled money and counts.
