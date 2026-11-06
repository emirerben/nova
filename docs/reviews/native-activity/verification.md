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

## Native preview recovery and current-main integration

The preview repair commit `620664a00` drains an already-queued seek after an older frame request fails and discards superseded successful frames. A one-shot image request forwards Swift cancellation to AVFoundation, ignores duplicate and late callbacks, and cancels abandoned interaction work. It preserves composition parameters and final export behavior. No retry budget, provider call, or server renderer was added.

Controlled red runs reproduced the lost queued seek and missing native cancellation, including deselection and session release. The repaired candidate passed 13 unit tests, including the unchanged scrub-playback integration test and generation-rebase test. The unchanged text-creation UI test passed after retaining its existing diagnostic suffix contract. Local evidence is retained in the isolated `native-preview-repair/.dev` directory, including `preview-final.xcresult`, `preview-ui-corrected.xcresult`, and source hashes. Independent review returned PASS with notes. These local runs use iOS 26.5. They do not prove the mechanism caused the retained hosted iOS 26.2 failures.

Current main `9ec04c50c` already owns migration 0127 for generated media admission. The integration preserves that migration and assigns native Activity migration 0128 with parent 0127. Both model classes, API protocol additions, and journey registrations are preserved. A fresh scratch database migrated from empty through both migrations. Independent integration review found no blocker. The focused backend integration passed 301 tests. One opt-in generated-media test skipped because its separate `NOVA_ADMISSION_TEST_DATABASE_URL` was absent. That test drops and recreates its dedicated table, so the shared admission database was not substituted. The actual new worktree database migrated through both tables successfully. The selector suite passed 41 tests and 183 subtests. The merged native source passed 22 unit tests and four UI tests with no failures or skips. Those cases include the original scrub-playback and text-creation failures, generation rebase, generated-media decoding, and all three Activity navigation journeys. The retained result is `current-main-0128-generated-native.xcresult`. The first integration build used an outdated ignored Xcode project and failed before tests; regenerating the project included main's new files. Preship validation runs after committing the resolved merge and setting its exact-head description.

Run `bash scripts/check-api.sh test tests/routes/test_native_activity_postgres.py tests/routes/test_device_render.py tests/tasks/test_device_render_reaper.py tests/routes/test_background_export_verification.py tests/integration/test_actual_export_verification.py tests/routes/test_actual_export_publication.py tests/test_content_plan_schema.py tests/services/test_generated_media_admission_postgres.py -q` against a worktree-only scratch database to reproduce the focused backend integration checks.

The single-job hosted retry did not execute tests. Its attempt-scoped fixture artifact was missing because the producer had run only in the original attempt. That failure is retained. The next source publication requires a complete workflow with its fixture producer. No artifacts were renamed or provenance checks relaxed.


## Account-boundary follow-up

A later local notification integration review found two existing client races relevant to the Activity inbox. A delayed refresh could overwrite or restore an account after sign-in or logout. An Activity acknowledgement could also refresh and retry with a different account before the view's final owner check. These fixes are isolated from push and continued-background preparation.

Keychain reads and mutations now share one process-wide lock, including conditional replacement and deletion of an exact session snapshot. Delayed expiry events preserve a newer account and fail closed if storage cannot be read. Activity opening binds the fresh Activity fetch, project fetch, and successful read acknowledgement to one captured owner before selecting the project. The shared refresh key remains the failed access token, preserving single-use refresh deduplication. This does not claim cross-process Keychain atomicity.

Controlled negative tests reproduced old-session restoration, account overwrite, and acknowledgement replay as the wrong account before the fixes. Independent source review found no remaining blocker in the extracted first-release patch. The isolated `.dev/account-boundary.xcresult` reports 71 passed, zero failed, and one existing unsigned-Keychain entitlement skip across 72 tests, including all eight new regression cases and all three Activity UI journeys. Both original scrub-playback and generation-rebase tests passed separately in `.dev/account-preview-regression.xcresult`. The selector contract passed 41 tests and 183 subtests. Recorded core source hashes matched after execution. The broader local runtime review passed 105 tests, with one existing unsigned-Keychain entitlement skip. That skipped test does not establish physical-device Keychain behavior.

No APNs client, notification permission prompt, background capability, backend delivery model, or live sender is added by this follow-up. DEBUG Activity fixtures use synthetic identities and an injected fake profile API.


## Export availability policy integration

Current-main integration includes `e07172d071f20de2ed7eca4814a240be50bfb163` and its completed-export advisory policy from #1954. The founder explicitly chose the inbox title “Export available” without a warning. Quality findings remain in verification reports and audit events. They do not withhold a completed export's Activity entry.

Availability still requires measured byte size and SHA-256 for the reserved storage generation, current owner and generation, unchanged source policy and upload reservation, and the current published artifact pointer. Publication now persists a separate versioned proof only after these checks. The unmeasured-quality fallback also checks actual bytes. Unreadable bytes remain pending; wrong size or checksum is refused. Historical records qualify only through a complete measured verification report, not the no-policy exemption.

The retained `.dev/activity-proof-green.log` reports 220 passed and zero skipped. Red controls reproduced quality-advisory false negatives, absent or mutated publication proof, mismatched or missing stored bytes, and policy/reservation races. Independent read-only review inspected the four backend source hashes and retained evidence and returned PASS with no blocking finding. These are local PostgreSQL and synthetic-storage results, not production or physical-phone delivery proof.

The updated native build passed all 41 selected simulator tests, with zero failures and zero skips, in `.dev/export-available.xcresult`. The independently extracted summary is `.dev/export-available-summary.json`. This includes DeviceRenderSessionTests, account-boundary regression tests, and all three Activity UI journeys using “Export available”. It does not establish APNs or physical-device background execution.

Additional export integration checks produced 89 passes, one sandbox-blocked loopback security test, and one sandbox-skipped PostgreSQL case. Both blocked cases passed separately with local access in `.dev/export-available-playlist.log` and `.dev/export-available-background.log`. The initial result remains in `.dev/export-available-integration.log`; it is not represented as an uninterrupted green run. No assertion or security boundary was changed to obtain these results.


## Named-test evidence repair and current-main refresh

Hosted run `41049397468` finished with creation and screens passing. Its unit log records 2,160 executed tests, 26 existing skips and zero failures, including both required auth-refresh methods passing. Four selected UI tests also passed. The final named-test gate nevertheless failed because the result parser treated a class named `KriaTests` as an already-present bundle prefix. Actual xcresult data uses `KriaTests/testMethod()` for that class, while the required selector is `KriaTests/KriaTests/testMethod`.

The parser now distinguishes two-component class/method identifiers from three-component bundle/class/method identifiers. It still requires exact identifiers and a Passed result. Regression controls reject another class, a foreign bundle, skipped and failed cases. The original control failed before the fix. The corrected code passed 118 orchestration tests, seven journey-gate tests and an actual saved-xcresult replay covering all 13 account-boundary selectors. Independent read-only review returned PASS without finding gate weakening. No local UI automation was needed for this repair.

The branch then integrated current main `a04d6963a82b0345c3aa4ec4831d3c8ad935ad69`. The only manual conflict was the journey registry. Every incoming main entry remains unchanged, and the two Activity entries are retained. The current merged source passed 212 focused backend tests without skips and 41 selector tests with 184 subtests. Earlier native results remain labeled evidence for their earlier source; fresh published-head CI must validate this current integration before merge. No failed assertion was waived and no test selector was removed.
