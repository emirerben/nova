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
