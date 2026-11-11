# Backend receipt and recovery evidence

Snapshot: receipt/delivery and keyed catch-up source implemented on 2026-11-06. See the final0136 evidence below; earlier sections preserve historical checkpoint counts. Independent snapshot review remains required. Parent owns integration/publication. No native execution, model/provider calls, Redis/Celery worker, production access, or full aggregate run was performed for these checks.

## Isolation and migrations

All commands below ran from `/Users/emirerben/Documents/Codex/2026-11-06/task-5/nova/src/apps/api`, except initial DB setup from the repository root. The private database is **nova_auto_convergence_receipts_20261106_test**, local OS user, loopback port5432. `/tmp/nova-receipts-db-safe-python` rejects other database names and external socket destinations, disables dotenv, and supplies dummy service settings. The offline runner additionally blocks all sockets and psycopg connections. Broker/render calls in integration tests are counters or fixed results.

Initial setup (root cwd):

```sh
bash /tmp/nova-receipts-test-db.sh > /tmp/nova-receipts-db-migrate.txt 2>&1
```

The helper writes `.dev/test-db-receipts-url`, preserving the parent aggregate DB receipt. No database was dropped. Subsequent migration commands (API cwd):

```sh
/tmp/nova-receipts-db-safe-python -m alembic upgrade head > /tmp/nova-receipts-db-migrate-0134.txt 2>&1
/tmp/nova-receipts-db-safe-python -m alembic upgrade head > /tmp/nova-slide-migrate-0135.txt 2>&1
```

Initial setup migrated through0133; subsequent logs record0134 and0135. Migrations add durable receipt/snapshot identity, permanent execution claim/pending publication metadata, and independent finalization budget respectively.

## Final executed commands

```sh
/tmp/nova-receipts-db-safe-python -m pytest tests/routes/test_slide_post_delivery_postgres.py tests/routes/test_slide_post_generation_receipts_postgres.py -q --tb=short > /tmp/nova-slide-final-pg-48.txt 2>&1
```

**48 passed in11.53s before the new catch-up regression was added.** This includes actual SQL locks/transactions and two independently launched child processes killed by SIGKILL after reporting durable commit.

```sh
.venv/bin/python /tmp/nova-unknown-offline.py tests/services/test_slide_post_generation_receipts.py tests/services/test_job_dispatch.py tests/routes/test_slide_post_native_routes.py tests/tasks/test_slide_post_render.py -q --tb=short > /tmp/nova-slide-final-offline-80-refreeze.txt 2>&1
```

**80 passed in6.44s.** Warnings include existing framework deprecations and unawaited AsyncMock warnings; these are not promoted to clean warning-free evidence.

```sh
.venv/bin/ruff check app/cli/kria_contracts.py app/config.py app/models.py app/routes/plan_items.py app/services/job_dispatch.py app/tasks/content_plan_build.py app/tasks/generative_build.py app/worker.py app/migrations/versions/0133_slide_post_generation_receipts.py app/migrations/versions/0134_slide_post_delivery_claim.py app/migrations/versions/0135_slide_post_finalize_budget.py app/schemas/slide_post_generation.py app/services/slide_post_generation_delivery.py app/services/slide_post_generation_receipts.py tests/services/test_job_dispatch.py tests/tasks/test_slide_post_render.py tests/routes/test_slide_post_delivery_postgres.py tests/routes/test_slide_post_generation_receipts_postgres.py tests/services/test_slide_post_generation_receipts.py
.venv/bin/ruff format --check app/cli/kria_contracts.py app/config.py app/models.py app/routes/plan_items.py app/services/job_dispatch.py app/tasks/content_plan_build.py app/tasks/generative_build.py app/worker.py app/migrations/versions/0133_slide_post_generation_receipts.py app/migrations/versions/0134_slide_post_delivery_claim.py app/migrations/versions/0135_slide_post_finalize_budget.py app/schemas/slide_post_generation.py app/services/slide_post_generation_delivery.py app/services/slide_post_generation_receipts.py tests/services/test_job_dispatch.py tests/tasks/test_slide_post_render.py tests/routes/test_slide_post_delivery_postgres.py tests/routes/test_slide_post_generation_receipts_postgres.py tests/services/test_slide_post_generation_receipts.py
git diff --check
/tmp/nova-receipts-db-safe-python -m app.cli.kria_contracts --check
```

Ruff: all checks passed,19 files already formatted. Diff check clean. Contract check matched tools.json, kria_mobile.json and generated mobile OpenAPI. These final checks were captured in tool output; earlier identical contract result is `/tmp/nova-slide-backend-contract-check.txt`.

Authorized generation command was `/tmp/nova-receipts-db-safe-python -m app.cli.kria_contracts --write > /tmp/nova-slide-backend-contract-write.txt 2>&1`. Only generated OpenAPI changed among generated contract artifacts.

## Important integration node IDs

Prefix every delivery node below with `tests/routes/test_slide_post_delivery_postgres.py::`:

- `test_killed_process_recovers_original_receipt_without_recompute[claimed]`
- `test_killed_process_recovers_original_receipt_without_recompute[staged]`
- `test_two_deliveries_claim_once_and_redelivery_never_recomputes`
- `test_real_task_entry_claims_before_body_even_when_recovery_flag_is_off`
- `test_concurrent_publishers_respect_one_attempt_window`
- `test_concurrent_finalizers_reserve_one_attempt`
- `test_finalization_commit_then_response_loss_does_not_repeat_publication`
- `test_staged_publication_retries_are_bounded_without_recompute`
- `test_scanner_contains_one_row_failure_and_finishes_following_result`
- `test_json_null_results_cannot_starve_a_later_staged_result`
- `test_acknowledged_queue_backlog_does_not_exhaust_unknown_attempts`
- `test_late_worker_after_policy_cancellation_cannot_publish_or_mark_ready`
- `test_completion_stamp_is_atomic_and_does_not_overwrite_newer_draft`

The `[claimed]` subprocess node also calls real authenticated GET slide-post and receipt GET after policy failure: public `render_status == failed`, precise receipt `job_status == processing_failed`. Both child cases verify exit `-SIGKILL` before redelivery/reconciliation, use a fresh interpreter/pool, and permit only the exact local test DB. No HOME override remains in child env. The60s test timeout is an infrastructure ceiling, not a product latency assertion. Exception-injection tests are separately named `test_claimed_exception_has_bounded_failure_without_resend` and `test_staged_result_survives_exception_and_finishes_without_compute`.

Additional receipt nodes (prefix `tests/routes/test_slide_post_generation_receipts_postgres.py::`):

- `test_global_uuid_race_rolls_back_losing_job`
- `test_new_uuid_cannot_replace_same_version_successor_from_stale_target`
- `test_receipt_get_observes_same_version_successor_without_rewinding`
- `test_rollout_gate_disables_capability_and_new_admission`
- `test_rollout_off_preserves_read_only_existing_receipt_observation`

## Tests-first red controls and limitations

Logs retained in `/tmp`:

| Log | Demonstrated failure before production fix |
| --- | --- |
| nova-slide-receipt-contract-red.txt | Missing strict mobile generation request path |
| nova-slide-receipt-extra-payload-red.txt | Unexpected payload fields accepted |
| nova-slide-receipt-get-red.txt | Missing receipt GET:2 failures,1 negative control passed |
| nova-slide-delivery-red.txt |6 missing delivery/claim behaviors |
| nova-slide-delivery-nonqueued-red.txt | Ineligible pending receipt not retired |
| nova-slide-admission-red.txt |2 failures: rollout capability/admission and target CAS contract |
| nova-slide-kill-status-red.txt | Actual killed-worker state returned processing_failed instead of canonical failed |
| nova-slide-json-null-red.txt |51 older JSON-null results consumed bounded selection; later staged result remained running |
| nova-slide-finalize-red.txt |5 failures: acknowledged backlog, unbounded staged exception, commit-response loss, concurrent reservation, scanner abort |

Initial genuine-kill run `/tmp/nova-slide-real-kill-pg.txt` had1 test assertion spelling mismatch (`generation_interrupted` versus existing `render_interrupted`) and1 pass. This was a test correction, not a product red control. Earlier TestClient BaseException injection broke its portal; that harness failure is retained in `/tmp/nova-slide-receipt-postgres.txt`. Neither is described as a production regression.

Original-base control used HEAD `4a95c2a41f149833ee539e460bc0c5bc179f0b97` and a pytest import plugin loading `routes.plan_items`, `tasks.content_plan_build`, and `tasks.generative_build` from `git show` copies in `/tmp`; shared source was never reverted:

```sh
/tmp/nova-receipts-db-safe-python -m pytest -p nova_slide_receipt_baseline tests/routes/test_slide_post_generation_receipts_postgres.py -k 'lost_response or first_render' -q --tb=short
```

Result:4 failed,9 deselected in4.83s; `/tmp/nova-slide-receipt-postgres-baseline.txt`, plugin `/tmp/nova_slide_receipt_baseline.py`, SHA `/tmp/nova-slide-receipt-baseline-head.txt`. Every failure was404 at the absent strict endpoint. This establishes missing endpoint support only; it does **not** independently prove baseline snapshot internals or provide a paired baseline run of the final45-case suite. Later regressions used focused pre-fix red controls listed above.

## Semantics, rollout and remaining gates

See `docs/runbooks/slide-post-generation-recovery.md`. Gate defaults off: capability0 and no new keyed admission until migrations0133–0135 and **all workers** are updated/drained. Claim and late-write fences always apply on new code regardless of flag. Existing receipt reads/replays remain read-only when disabled.

Unknown pending delivery has five bounded publication attempts. Positive broker acknowledgement instead waits up to35minutes before queue-policy failure. Running claims are permanent and never reclaimed. Persisted exact results may be published with five separately reserved finalization attempts; no result after35minutes becomes actionable policy failure. A deadline is not proof of worker death. All failure paths preserve the original receipt; only explicit Create authorizes a new logical intent. Native policy behavior belongs to the native owner's separate evidence.

Independent backend review, parent integration, broader release gates, and deployment remain outstanding. No claim of production rollout or native end-to-end readiness is made by this handoff.

Initial publication reservation follow-up: `/tmp/nova-slide-initial-reservation-red.txt` had1 failed (flag disabled after admission fell through legacy publication) and2 passing reservation controls. The keyed postcommit branch now always calls publish_pending, whose flag gate safely retains pending intent. Added nodes: `test_flag_disabled_after_admission_never_falls_back_to_legacy_publish`, `test_initial_send_reserves_budget_before_io_and_blocks_scanner_race`, `test_process_loss_before_first_send_spends_reserved_attempt_safely`. Final48-case command/log above includes these. Focused ruff/format on the changed dispatcher and test file passed again.


## Final0136 catch-up evidence

Working HEAD before final freeze: `4cbe7d2a7a5b89d0decdbd4a1405d2323250aa7a`; the backend implementation is in the shared working diff, not a standalone agent commit. Parent owns the final manifest. Source files add migration0136, `slide_post_render_revisions.py`, atomic PUT/compose staging, exact snapshot/generation rebuild entry, immutable artifact/cache identities, strict pinned sticker failure, current revision GET projection and first-result newer-token preservation.

Migration command (private DB only):

```sh
/tmp/nova-receipts-db-safe-python -m alembic upgrade head > /tmp/nova-slide-migrate-0136.txt 2>&1
```

This ran before AI migration0137 existed; log proves0135→0136 only. AI owner uses a separate migration/test lane.

Final serial integration command:

```sh
/tmp/nova-receipts-db-safe-python -m pytest tests/routes/test_slide_post_delivery_postgres.py tests/routes/test_slide_post_generation_receipts_postgres.py -q --tb=short > /tmp/nova-slide-revision-final-pg-69.txt 2>&1
```

Final offline command:

```sh
.venv/bin/python /tmp/nova-unknown-offline.py tests/services/test_slide_post_revision_inputs.py tests/services/test_slide_post_generation_receipts.py tests/services/test_job_dispatch.py tests/routes/test_slide_post_native_routes.py tests/tasks/test_slide_post_render.py -q --tb=short > /tmp/nova-slide-revision-final-offline.txt 2>&1
```

Final serial PostgreSQL:69passed21.49s. Offline:85passed4.37s. Earlier `/tmp/nova-slide-revision-full-current.txt`55pass run briefly overlapped another private-DB check; it is not the final isolation proof. `/tmp/nova-slide-revision-final-pg.txt` had68passes plus one synthetic-fixture cleanup failure (Job deletion before clearing PlanItem.current_job_id FK); fixture cleanup was corrected, with runtime unchanged, before the final serial rerun. `/tmp/nova-slide-revision-budget-current.txt` also records the earlier synthetic fixture missing required raw_storage_path, not a production failure.

Additional reproduced controls:

| Log | Red behavior |
| --- | --- |
| nova-slide-catchup-red.txt | Actual queuedv1→PUTv2→first completion left zero catch-up dispatches |
| nova-slide-revision-inputs-red.txt | Missing generation cache identity helpers and strict pinned sticker behavior |
| nova-slide-revision-parent-red.txt | Missing revision settlement implementation |
| nova-slide-revision-stop-supersession-red.txt | Stop accepted a staged late result; v3 could not replace queuedv2 |
| nova-slide-revision-device-authority-red.txt | Authoritative device receipt incorrectly admitted pending cloud revision |
| nova-slide-revision-queue-clock-red.txt |34minute parent wait left only2minutes before false queue timeout |
| nova-slide-revision-contract-red.txt | New result inherited old proof/bypassed contract boundary and stamped ready |
| nova-slide-revision-stop-projection-red.txt | GET reported rendering immediately after Stop |
| nova-slide-revision-complete-cache-red.txt | Missing namespace separation from legacy partial-sticker derivatives |
| nova-slide-first-newer-token-red.txt | Late first upsert overwrote an already newer variant generation |

The first device fixture used a non-authoritative render_destination field and passed; it was corrected to `_device_render_v1` before producing the meaningful red. No false red claim is based on the earlier fixture.

Additional exact delivery nodes:

- `test_saved_newer_draft_before_first_variant_gets_one_fenced_catchup`
- `test_failed_parent_settles_saved_revision_without_repeating_first_intent`
- `test_old_revision_timeout_cannot_fail_newer_revision_or_global_job`
- `test_stop_settles_revision_and_save_cannot_revive_cancelled_job`
- `test_queued_v2_superseded_by_v3_does_not_consume_latest_render`
- `test_revision_worker_claim_is_exclusive_and_binding_is_checked`
- `test_save_after_first_upsert_does_not_start_legacy_render_before_finalize`
- `test_real_revision_task_uses_exact_snapshot_and_publishes_once`
- `test_late_revision_task_cannot_publish_after_newer_save`
- `test_device_owned_revision_never_enters_cloud_builder`
- `test_revision_queue_deadline_starts_at_publication_not_save`
- `test_revision_publication_rechecks_contract_without_inheriting_old_evidence`
- `test_stop_projection_is_terminal_before_revision_scanner_settlement`
- `test_killed_revision_process_settles_without_recompute[claimed]`
- `test_killed_revision_process_settles_without_recompute[staged]`
- `test_first_result_recovery_preserves_already_newer_variant_generation`
- `test_waiting_parents_do_not_consume_revision_completion_budget`
- `test_revision_unknown_dispatch_and_staged_failure_have_separate_finite_budgets`
- `test_revision_staged_publication_failure_is_bounded`
- `test_revision_commit_then_response_loss_observes_completed_receipt`

These use the same delivery-file prefix listed above. The51-waiting-parent case creates actual owned items/Jobs/parent receipts and verifies none consumes the eligible completion budget. Revision SIGKILL fixtures use `tests.slide_post_revision_kill_worker` with a fresh interpreter/pool and exact local database guard; no Celery worker/provider/render runs. Rendering in actual task-wrapper tests is a fixed-result function.

The current syntax/lint scope is the earlier19 files plus migration0136, revision service, revision input tests, and subprocess helper (23files). AI owner controls formatting of the concurrently appended AI model in models.py and final combined contract generation. No catch-up DTO schema shape was added; existing render_status/validation_errors project its state. Parent still owns native/full aggregate/release gates and production rollout.

Final freeze verification: all scoped Ruff checks passed; the11 files changed in the final correction pass (including shared models.py) were already formatted, and git diff --check was clean. Independent reviewer received the freeze notice after these checks. No production/native/provider execution occurred.
