# Verify API credit capabilities without live calls

The final targeted selection covers generated-media contracts, real-PostgreSQL budgets/admission, transport fixtures, authenticated native routes, judge acceptance, canonical lock guards, migration structure and the remaining root API tests. Use an isolated migrated `nova_test` database with `CI=1`, replay mode and no-op provider keys. The database is task-owned; no shared development or production database is involved.

```sh
cd src/apps/api
python -m pytest tests/agents/test_credit_plan_contracts.py tests/agents/test_comparison_workflow.py tests/agents/test_text_provider_contracts.py tests/agents/test_sealed_comparison.py tests/evals/test_online_judge_acceptance.py tests/services/test_credit_cap_budgets.py tests/services/test_credit_deadline_compatibility.py tests/services/test_generated_media_transport.py tests/services/test_generated_media_service.py tests/services/test_generated_media_admission.py tests/services/test_generated_media_admission_postgres.py tests/services/test_apple_revocation_postgres.py tests/services/test_text_provider_ledger.py tests/services/test_ai_cost_control.py tests/routes/test_generated_media.py tests/routes/test_content_plan_lock_mode.py tests/routes/test_lock_order.py tests/agents/test_deadline_ladder.py tests/tasks/test_kria_turn_failure_code.py tests/test_*.py -q
```

The admission migration replay additionally requires `NOVA_ADMISSION_TEST_DATABASE_URL` pointing to a separate disposable `nova_admission_test` database. Never use a shared database. PR1934 has now landed. All six deadline compatibility checks run against the merged runtime without a special checkout or skip; the historical exact-head overlay receipt is retained separately.

## Recorded evidence

- After the final narration lock correction and landed deadline-runtime integration, the selection above passed 1,660 tests and 24 subtests in 57.17 seconds, with zero failures, errors or skips.
- The demonstrated narration lock regression failed before correction: actual PostgreSQL statements locked CreationThread first. After correction, all 52 service/lock/schema checks passed, including Persona → ContentPlan → PlanItem → CreationThread order. No static guard was weakened.
- Before the final deadline-runtime merge, mapped reliability gates passed 286 tests with zero environment skips or errors. Final preship rechecks the mapped gates. Runs overlap; do not sum their counts.
- Native simulator evidence: `.dev/generated-media-final-journeys.xcresult`, 14 passes (nine unit, five UI), zero failures/skips; inspected attachments are retained beside it. Native implementation remained unchanged through both main merges. This is iPhone 17 Pro / iOS 26.5 simulator behavior, not physical-device export evidence.
- The broad API attempt reached 95% and stopped at its five-problem limit: 33,274 passes, 39 skips, 15 expected failures, 24 subtests, three failures and two setup errors. The failures exposed the generated service's lock mode/order and a stale migration-head expectation; all were corrected. The two Apple-revocation fixtures refused the earlier `nova_generated_test` database name and passed in the targeted selection on task-owned `nova_test`. A full suite at the final corrected revision has not been rerun; no green full-suite claim is made.
- Earlier default-FFmpeg drawtext failures both passed with the already installed `/opt/homebrew/opt/ffmpeg-full/bin` build. Contract snapshot drift was regenerated and independently checked.

See [verification.json](verification.json) for safe artifact summaries, [REVIEW.md](REVIEW.md) for review scope, and [the runbook](../../runbooks/api-credit-capabilities.md) for configuration. Raw local result bundles stay outside Git.

The refreshed [offline preflight](offline-preflight.json) estimates 0.384148 USD for the existing montage development fixture, frozen against the reconciled runtime and existing price table. It authorizes no spending and establishes no quality winner. No fresh real holdout is claimed.

Live calls, billing verification, eligible real cohorts and inspected physical-phone exports remain unverified. Transcript agreement does not establish pronunciation or audible fidelity; generated images still need inspection for unwanted lettering. All new flags remain false and budgets zero.

## CI continuation

Final-head CI exposed a missing native UI inventory registration before API jobs started. All five generated-media UI tests are now classified in the creation group; all 121 existing CI selector tests pass and independent review confirmed no guard, fallback or prior mapping was weakened. A full four-worker API rerun is in progress on task-owned `nova_test`, using replay, no-op keys, existing full FFmpeg and CI-style per-test timeouts. Its result remains pending.
