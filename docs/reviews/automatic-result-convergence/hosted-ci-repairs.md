# Hosted API failure corrections after integrated source

Parent source61477ca3c6dc99a061edef143b2f6ae953def8db (tree49baea00a6ac1204c1585a7f3f5398a6a2ec8364) integrates mapping60437739121c70166ff855af700af83b9197c93e and main4bfa30f34fe450d4b5ea8dd15a1fe1796b021d13. This is a separate correction; it does not reattribute old59 PostgreSQL passes to integrated source.

Old hosted3d run42257775770: api(0) had97 failures/21493passes; api(1)25failures/22869passes. Logs were read, not rerun. Many failures shared the new fence reading FakeJob.id even when no keyed claim was active. Process controls rejected the real disposable CI database because their helper allowed only the exact local scratch name. Additional concrete failures were an outdated render-entry parameter pin, editor public-method parity, Alembic sole-head expectation, and an asynchronous sticker fixture. Downstream aggregate failures do not establish a native test failure or execution.

Corrections independently reviewed without removing production guards:

- Both unkeyed write checks return before inspecting job.id when active_job is None. Matching activeUUID still requires the running receipt, and the upsert generation fence remains unchanged. The regression first failed1/6; real keyed/stale/missing receipt controls remain.
- Fake-provider process helper retains its exact private database default. Only trusted CI=true, GITHUB_ACTIONS=true and GITHUB_REPOSITORY=emirerben/nova together allow nova_test/nova_journey_test. Initial and subsequent URL DSNs reject wrong schemes, non-loopback/non5432, query/fragment and other databases; all connects must match the selected exact DB. External socket denial, disabled dotenv and fake provider remain. Six trusted-CI cases failed before the fix;39 focused controls pass after query/fragment cases were added. This is offline guard proof, not actual CI process/DB proof.
- Nine ordinary-job task methods (11 parameterizations) explicitly opt into a fake unkeyed/legacy lookup decision; the production wrapper still executes. No global/autouse override, claim reset or retry-assertion relaxation. The18-second speech wait passes unchanged; the former60 seconds came from the outer retry on an unintended real DB lookup. One ordinary-job typed-decline unit uses the same explicit seam. Real keyed/persistence tests are untouched.
- Sticker asset-row fixture now exposes AsyncSession.run_sync as AsyncMock(False), modeling absence of a keyed generation receipt, rather than failing on awaiting MagicMock. Real revision staging is exercised by separate PostgreSQL controls.
- Render-entry pin explicitly adds artifact_generation and strict_inputs: runner derives them only from its server-owned claim UUID and strict asset validation. No asked-value override, entry/recipe-path move or lane_targets red suppression. Full61 route replay/negative-control cases pass.
- Parity classifies resolveMergeConflicts as requiring explicit creator choice, and setObserversVisible as lifecycle observation; no fake agent op was invented. Structural migration guard preserves exact linear0133→0138 links and sole0138 head; no migration is executed by that test.

Executed evidence:

1. Existing five metadata/sticker failures reproduced5red in4.98s; corrected5green3.38s. Expanded final boundary run:153 passed/29warnings/4.73s, `/tmp/nova-auto-ci-boundaries-final-fixed.log`. It includes39 guard cases; do not sum those again. First expanded run had152pass/1 unintended ordinary-job DB lookup, preserved in `/tmp/nova-auto-ci-boundaries-final.log` before the explicit fixture correction.
2. Full six task files:493 passed/1 skipped/44warnings/8.05s, `/tmp/slide-unkeyed-fixture-focused.log`. Files: test_generative_build, test_cloud_render_receipts, test_orchestrator_db_resilience, test_render_generation_guard, test_speech_cut_rerender, test_visual_blocks_autoplan. Exact skip: tests/tasks/test_render_generation_guard.py::test_lock_serialization_requires_real_concurrent_db_sessions, an explicit source marker requiring the real concurrent integration DB harness. It is not passing concurrency evidence.
3. Full tests/kria/test_route_replays.py:61 passed/2warnings/26.77s, `/tmp/nova-auto-route-replays-final.log`. No model/network/database or production media used.
4. All11 changed Python files pass Ruff/check/format; diffcheck passes. Final AI/saved-song offline rerun is recorded in the publication handoff. Existing168 CI selector contracts remain on unchanged policy source from the reviewed integration; current candidate diagnostics find zero unmapped paths.

All PostgreSQL/process tests on combined source, native build/781 app/39 package/8UI, model/shared companion/aggregate/Linux and physical/rollout gates remain pending. Local loopback5432 is unavailable; no service was started or migration applied. Source-only CI URL acceptance is not new persistence evidence. Hosted fresh-head checks must run normally; old failed runs were not manually retried. Active tracking is unresolved with parent; archived KRI-935 is historical provenance only. No rollout/flag/private-project action occurred.

Source/control hashes binding this separate correction:

```json
{
  "src/apps/api/app/kria/editor_capability_parity.py": "193ba5d678a0e0c93aabcc178b06eee6dd52f9e460de1119d845213d9b94019a",
  "src/apps/api/app/services/slide_post_generation_delivery.py": "d6a8b8c5632a6e8799a698ad40f2b6ae7b2ed47b866462668c126e5ba0e7aade",
  "src/apps/api/app/tasks/generative_build.py": "442833de0f0d6e8725cb69b1a8522eb87574b52ced9b2d73081a24a78affe941",
  "src/apps/api/tests/fixtures/kria_replay/render_entries.json": "3cd90a5a053bf075ece4b352dd8c67848023182c52227246e397389899a4fcba",
  "src/apps/api/tests/routes/test_slide_post_sticker_routes.py": "65bef37c45d626fecce4cbc3d6276de330dd191883477620dd8baa12f2cb2986",
  "src/apps/api/tests/services/test_cloud_render_contract.py": "4f935866862ffeab3b433f68317d6c269d91deb28aa94ffc0ddd609d99af5861",
  "src/apps/api/tests/slide_post_ai_kill_worker.py": "de9b8e4bc7c80183ac9bc21a0dda74d92cd4276377402f5fd41da25f995177c1",
  "src/apps/api/tests/tasks/test_generative_build.py": "dcf8bfa969a779746a6e1955142613ce9b75265fc81a1e16268189feaadc2ae7",
  "src/apps/api/tests/test_content_plan_schema.py": "8eb2685a09a293c164e5b91fe6be3c53145aacfca44fcea896e9369040bdb5f4",
  "src/apps/api/tests/services/test_generation_delivery_write_guard.py": "7e4485d29c544d70c7c3488681a44e3c44c701e66e9dbef4d10fdfe36f563093",
  "src/apps/api/tests/slide_post_ai_worker_database_guard.py": "1c5e89eb64a991c0bea7c0ea318c19285e4bdcee146122070f60ea47650ac9d3",
  "src/apps/api/tests/test_slide_post_ai_worker_database_guard.py": "4f11f23d132d611e98669cbdb63b03a207111f9c34d95f7e250623e25d478f91"
}
```

Final offline source receipt:129 passed/44warnings/3.92s (`/tmp/nova-auto-final-offline.log`, JUnit `/tmp/nova-auto-final-offline.xml`) after all runtime/metadata corrections. The selected files are compose/chat-edit/strict-AI/prepared-envelope/request/task-limit and saved-song controls. This overlaps the earlier125+4; no sum is implied. Native source-selection hashes still match781app/39package/8UI candidates.

Executed command selections (repository root unless noted):

```sh
src/apps/api/.venv/bin/python /tmp/slide-ai-offline-tests.py -q src/apps/api/tests/services/test_generation_delivery_write_guard.py src/apps/api/tests/test_slide_post_ai_worker_database_guard.py --tb=short
src/apps/api/.venv/bin/python /tmp/slide-ai-offline-tests.py -q src/apps/api/tests/tasks/test_generative_build.py src/apps/api/tests/tasks/test_cloud_render_receipts.py src/apps/api/tests/tasks/test_orchestrator_db_resilience.py src/apps/api/tests/tasks/test_render_generation_guard.py src/apps/api/tests/tasks/test_speech_cut_rerender.py src/apps/api/tests/tasks/test_visual_blocks_autoplan.py --tb=short
```

From src/apps/api (existing guarded scratch runner; these selections do not require a live DB):

```sh
/tmp/nova-ai-db-safe-python -m pytest -q tests/kria/test_render_entry_pins.py tests/kria/test_editor_capability_parity.py tests/test_content_plan_schema.py tests/routes/test_slide_post_sticker_routes.py tests/services/test_cloud_render_contract.py tests/services/test_generation_delivery_write_guard.py tests/test_slide_post_ai_worker_database_guard.py --tb=short -rs
/tmp/nova-ai-db-safe-python -m pytest -q tests/kria/test_route_replays.py --tb=short -rs
```

Independent final repair review approved the exact source/fixture/guard changes; pending153 rerun then passed. These guards are test-specific database protection, not a production connection-policy change. No actual persistence/process evidence is inferred from offline URL acceptance.
