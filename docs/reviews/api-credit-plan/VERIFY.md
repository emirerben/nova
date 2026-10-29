# Verify API credit capabilities without live calls

Historical first-increment evidence is 53 tests and 24 subtests. Intermediate integrated evidence is 245 tests and 24 subtests across 16 selected backend suites, then 13 boundary/concurrency tests. Judge runs reported 107 and 74 tests in overlapping selections. Do not sum these counts. These are historical intermediate runs. At code HEAD `8d13cc470`, 60 focused API tests and 286 mapped reliability tests passed. Final independent review found no blocking findings.

From the repository root with the API environment active, run the fixture-backed checks below. They require no live-provider submission.

```sh
python -m pytest -c src/apps/api/pyproject.toml src/apps/api/tests/agents/test_credit_plan_contracts.py src/apps/api/tests/agents/test_comparison_workflow.py src/apps/api/tests/agents/test_text_provider_contracts.py src/apps/api/tests/agents/test_sealed_comparison.py src/apps/api/tests/agents/test_online_eval.py src/apps/api/tests/services/test_credit_cap_budgets.py src/apps/api/tests/services/test_credit_deadline_compatibility.py src/apps/api/tests/services/test_generated_media_transport.py src/apps/api/tests/services/test_generated_media_service.py src/apps/api/tests/services/test_generated_media_admission.py src/apps/api/tests/routes/test_generated_media.py -q
```

The actual PostgreSQL admission check needs a dedicated disposable scratch database. Set `NOVA_ADMISSION_TEST_DATABASE_URL` to that database before running this command. Never point it at production or a shared development database. A skip is missing coverage.

```sh
python -m pytest -c src/apps/api/pyproject.toml src/apps/api/tests/services/test_generated_media_admission_postgres.py -q
```

Recorded dedicated PostgreSQL databases were `nova_generated_test` and `nova_admission_test`. Final native simulator evidence is `.dev/generated-media-final-journeys.xcresult`, with 14 tests (nine unit and five UI), zero failures and zero skips. Native code remained unchanged from the tested revision through the main merge. Use the repository iOS verification workflow for the selected simulator and preserve its result bundle.

The broad API run stopped with three failures, 16,450 passes, 29 skips, 13 expected failures and 24 subtests in 762 seconds. One failure was mobile OpenAPI snapshot drift; regeneration added 21 lines to `Kria/Generated/openapi.yaml`. Two failures came from an unavailable FFmpeg `drawtext` filter. Both FFmpeg tests passed separately using the already installed `/opt/homebrew/opt/ffmpeg-full/bin` build (two passes, no installation). The independent contract check and drift test passed after regeneration. A new full API run explicitly deselects those two separately verified tests and is still running. Its outcome is pending; the broad suite is not yet verified green. See [verification.json](verification.json) for the safe retained summary.

The refreshed `offline-preflight.json` estimates 0.384148 USD for the repository montage development fixture, frozen against the current runtime and existing price table. It is an offline planning estimate, not spending or model-quality evidence. The packet remains outside Git; no fresh real holdout is claimed. Regenerate packet preflight after prompt, schema, pricing or runtime changes. Expected report values remain `live_authorized: false`, `quality_verdict: unassessed`, and `actual_export: unassessed`.

Live calls, billing verification, fresh eligible holdouts and inspected physical-phone exports remain unverified. Synthetic audio matching the transcript does not prove pronunciation or audible fidelity. Generated images still require human inspection for unwanted lettering.
