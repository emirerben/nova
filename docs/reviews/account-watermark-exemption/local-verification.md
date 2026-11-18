# KRI-1533 local verification

Implementation tested at commit `4c140226ec0ebac644389ccaa4117519bf3e0d96` on 2026-11-18. This record adds no implementation changes.

## Executed checks

- API: `PYTHONPATH=src/apps/api .dev/testenv/bin/python -m pytest src/apps/api/tests/kria/test_watermark_exemption.py src/apps/api/tests/routes/test_device_render.py -k 'watermark or foreign_job_is_filtered' -q`: 4 passed, 134 deselected.
- Entitlement: `swift test --package-path src/apps/ios/Packages/KriaMediaEngine --filter WatermarkExemptionTests`: 2 passed.
- Synthetic native output: `swift test --package-path src/apps/ios/Packages/KriaMediaEngine --filter BrandingTests/testAccountExemptionRemovesWatermarkButPreservesOutro`: 1 passed. A generated black-photo recipe has no watermark at 0.1, 1.0, and 1.9 seconds. Its duration retains the original edit plus the shipped outro, and its tail matches the bright outro.
- iOS Simulator: `xcodebuild` with scheme `Kria`, Debug configuration, a dedicated empty iPhone simulator, and `-only-testing:KriaTests/NativeEditorExportTests` plus the four watermark/offline/saved-generation tests in `NativeEditorSessionTests`: 16 passed, 0 failures. This includes removing a temporary export when final validation throws, rejecting stale-owner responses, rejecting another saved generation, avoiding branded cache for an exempt owner, and retaining ordinary offline routing.
- Generic Simulator `build-for-testing`, scoped ruff, Python compilation and `git diff --check` passed.

## Limits

These are local synthetic and simulator checks, not a production replay or physical-device result. No customer data or footage was used. No account was enabled, production setting changed, server render started, deployment performed or published media overwritten. The paired physical iPhone was unavailable. Full account-transition integration and revocation policy remain review items. Missing original media or an unknown saved generation intentionally blocks export.
