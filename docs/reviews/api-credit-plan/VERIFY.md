# Verify API credit capabilities without live calls

Historical first-increment evidence is 53 tests and 24 subtests. Intermediate integrated evidence is 245 tests and 24 subtests across 16 selected backend suites, then 13 boundary/concurrency tests. Judge runs reported 107 and 74 tests in overlapping selections. Do not sum these counts. Final consolidated evidence remains pending.

From the repository root with the API environment active, run the fixture-backed checks below. They require no live-provider submission.

```sh
python -m pytest -c src/apps/api/pyproject.toml src/apps/api/tests/agents/test_credit_plan_contracts.py src/apps/api/tests/agents/test_comparison_workflow.py src/apps/api/tests/agents/test_text_provider_contracts.py src/apps/api/tests/agents/test_sealed_comparison.py src/apps/api/tests/agents/test_online_eval.py src/apps/api/tests/services/test_credit_cap_budgets.py src/apps/api/tests/services/test_credit_deadline_compatibility.py src/apps/api/tests/services/test_generated_media_transport.py src/apps/api/tests/services/test_generated_media_service.py src/apps/api/tests/services/test_generated_media_admission.py src/apps/api/tests/routes/test_generated_media.py -q
```

The actual PostgreSQL admission check needs a dedicated disposable scratch database. Set `NOVA_ADMISSION_TEST_DATABASE_URL` to that database before running this command. Never point it at production or a shared development database. A skip is missing coverage.

```sh
python -m pytest -c src/apps/api/pyproject.toml src/apps/api/tests/services/test_generated_media_admission_postgres.py -q
```

Recorded dedicated PostgreSQL databases were `nova_generated_test` and `admission_test`. Native simulator evidence is `.dev/generated-media-complete-tests.xcresult`, currently nine tests (seven wire and two UI); additional image and relaunch checks are pending. Use the repository iOS verification workflow for the selected simulator and preserve its result bundle.

The retained `offline-preflight.json` is an aggregate estimate for a synthetic packet, not spending or model-quality evidence. Its 0.384148 USD estimate uses the earlier price table. Regenerate packet preflight after prompt, schema, pricing or runtime changes. Expected report values remain `live_authorized: false`, `quality_verdict: unassessed`, and `actual_export: unassessed`.

Live calls, billing verification, fresh eligible holdouts and inspected physical-phone exports remain unverified. Synthetic audio matching the transcript does not prove pronunciation or audible fidelity. Generated images still require human inspection for unwanted lettering.
