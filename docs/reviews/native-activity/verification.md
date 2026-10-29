# Native activity verification

Source base is 788f93c0453888a1ae86c25ea143d75956ec9131. Final source commit is the PR head referenced by its request-evidence record. This record reports local implementation evidence only.

The focused API command ran against worktree scratch database nova_native_activity_inbox_test, migrated from empty through the new activity migration. It passed 157 tests, with no skips. It includes real HTTP and PostgreSQL activity persistence, duplicate/rollback, target and owner fences, proof rejection, failure-report and Stop behavior, stale-device and verification-exhaustion hooks, and a media-checked actual publication endpoint with duplicate completion. External storage is an isolated transport double. No model calls run.

Command:

```
bash scripts/check-api.sh test tests/routes/test_native_activity_postgres.py tests/routes/test_device_render.py tests/tasks/test_device_render_reaper.py tests/routes/test_background_export_verification.py -q
```

Native build-for-testing passed. Three ActivityUITests initially passed, including read-acknowledgement failure. Independent review requested stronger exact-project selection. That expanded fixture initially failed because the first empty project was correctly discarded and the drawer-close transition was not awaited. The fixture now preserves both projects with a user event and waits for drawer closure. The corrected final run passed all three tests, including exact-project selection from a different open project, stale lookup, and failed acknowledgement. It ran on dedicated iPhone 17 Pro simulator A66888D1-F7C3-42EC-AFFE-91537B54749E with iOS 26.5, code signing disabled, and an offline-only URL transport. Retained screenshots were inspected. Earlier native results are not substituted for this final run.

Independent read-only architecture and implementation reviews selected the dedicated table, required all terminal writer hooks, and found a thread/item/Job binding omission. That omission was fixed and pinned by a real PostgreSQL negative control. Final independent static review found no remaining blocker. Suggested follow-ups include pagination cursor invalidation and account switch during native navigation. The implementation checks account identity after asynchronous calls, but no UI account-switch claim is made.

No production rollout, push delivery, physical-phone background execution or new native-render fidelity claim is made. These remain separate follow-ups under the broader notification design and KRI-1039 coordination context.

## Latest-main integration

Integrated main 1692d1751cb78d0695f16b4170362e9e4ae4f615, including provider deadlines, API cost work and Send recovery, without rewriting the feature branch. Four overlapping integration files merged cleanly. Independent comparison against both parents found Activity ownership/navigation and Send's project-specific recovery preserved; the UI test registry contains the union of both parents. Activity during a delayed send and account switching remain explicit coverage gaps.

The actual merged source passed 237 focused backend tests, including the native lifecycle, migration chain, provider deadlines and failure-code controls. Its dedicated local database migrated through 0127. The native app then built and passed all three Activity UI journeys and all eleven ChatSendReceiptTests on the same task-owned iOS26.5 simulator. The inspected result bundle reports 14 passed, zero failed and zero skipped. Retained artifacts are `.dev/latest-main-api.log`, `.dev/latest-main-native.log`, `.dev/latest-main-native.xcresult` and `.dev/latest-main-native-summary.json`.

The earlier detached integration snapshot at main fc0c8ff84 also passed 237 tests; its retained log was inspected after the executor reconnected. It is supporting historical evidence, not a substitute for the actual 1692d1751 merge above. No production deployment or push delivery is claimed. GitHub checks must bind to the new merge head.

## Conflict integration after the green run

CI run 40910715249 completed successfully for 706d62f32bd06eb85c4020d209db1636d1ce5f89. Its native unit log reports 2,120 tests with 26 skipped and zero failures, followed by four selected UI tests with zero failures. This remote UI subset is not substituted for the three independently executed Activity UI tests.

A separate verifier rebuilt that exact head and passed all three Activity UI journeys with zero failures or skips. The retained result is `.dev/independent-shipping-activity.xcresult`. The verifier inspected the result summary and exported screenshots and returned PASS+NOTES. Account switching, same-owner session replacement, pagination invalidation and navigation during delayed Send remain explicit gaps.

GitHub then reported a real conflict with main f32043bf3b33a6bf1bb5e0b593cc0b8d93ab3182. The single manual resolution preserves main's journey manifest and appends the unchanged Activity entry. Independent comparison verified the exact union of 110 unique registrations, all main entries unchanged, and all 41 selector tests passing. Every iPhone source and Activity/publication hook is unchanged from the independently tested head. Incoming export verification is unchanged from main.

The merged source passed 343 focused backend tests, zero skips, including `tests/integration/test_actual_export_verification.py` and `tests/routes/test_actual_export_publication.py` alongside the previous Activity, schema, deadline and failure suites. These added tests cover current v5 acceptance, older-report rejection and wrong-picture controls. The log is `.dev/main-f320-integration-api.log`. The Activity synthetic publication fixture alone has no export policy and does not establish universal semantic proof. Fresh exact-head CI is required after publishing this integration.
