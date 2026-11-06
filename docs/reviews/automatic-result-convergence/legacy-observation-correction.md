# Legacy AI observation correction (local work in progress)

Predecessor evidence remains frozen at 51249def55de4ffc3b857c462d5fba2516da8606. This separate correction addresses the compatibility review finding; it does not retroactively change that evidence or authorize rollout.

Acceptance/failure modes, recorded before implementation:

- Missing original-envelope/claim evidence must not classify a legacy PENDING row as definitively unexecuted, rejected or cancelled. Reads expose unresolved uncertainty; no reconstruction, replacement UUID or paid redispatch.
- Existing legitimate old execution can persist running/token and later complete under that original token. Observation must recover that exact result without a second provider call. A missing-token outcome cannot gain an invented claim or accept an unauthenticated arbitrary result.
- Stop may halt client observation but cannot assert a server-side cancellation of unknown work. Resume observes only the same original ID; newer local target/account/environment/request/source must not inherit that authority or apply stale output.
- Unsupported rows are excluded before the bounded outbox selection, not merely skipped after limit. More than 50 old rows cannot starve an eligible new request, and no hot loop increments/publishes unsupported rows.
- Cancellation, admission replay and receipt reads preserve owner/item/project identity. Source replacement and newer instructions cannot cause old output to overwrite new state.

Evidence levels: focused real PostgreSQL/persistence and fresh-process controls with fake external providers are permitted; native source controls are written but cannot execute under the shared heavy hold. Actual production backlog provenance, rolling old binaries, broker/Beat, deployment and physical-device behavior remain unverified. No migration, remote write, flag or private-project mutation is authorized for this correction.

Initial executed red control: the new PostgreSQL legacy-control file produced 11 expected failures in 8.16s against predecessor production behavior (`/tmp/slide-ai-legacy-red.log`). It exposed false terminal rejection/cancellation, overwriting a persisted old claim, refusal of token-bound late publication, and eligible-work starvation behind the first 50 legacy rows. This is real scratch PostgreSQL with fake provider/broker, not a native or production test. Additional adversarial controls and corrected-source reruns are recorded below; this paragraph preserves the initial reproduction target.

Native source selection audit found the 51249 manifest omitted 41 existing AI test methods across extensions and SlidePostAISessionRecoveryTests. The replacement manifest includes them plus five new legacy controls: 723 app candidates in 17 classes; package39/UI6 unchanged. These are static candidates only. No new or old native case executed. The source-binding guard compares slide ID/asset ID/kind, allowing reorder and preserving independent-field merge; same-ID media byte replacement cannot be established from this client baseline and remains a rollout/source-generation compatibility limit.

## Corrected-source checks

Focused database selection: 23 passed, 44 warnings, 14.78s (`/tmp/slide-ai-legacy-final-pg.log`). Offline impacted six-file selection: 125 passed, 44 warnings, 2.48s; exact command and final receipt follow below. Scoped Ruff check and format-check of all six changed Python files passed (6 files already formatted). Native source-selection hashes were checked against disk; this is not compilation or execution.

Two intermediate typed-version controls initially failed because SQLAlchemy compared JSON 1/true/1.0 as equal and skipped the fault-injection UPDATE. The fixture now forces persistence and confirms the reloaded JSON type. Those two failures are fixture corrections, not independent production red proof. The original 11-failure reproduction and missing-manifest/digest failures remain separate evidence.

Intermediate runtime/control source hashes (superseded by final manifest below):

```json
{
  "src/apps/api/app/services/slide_post_ai_requests.py": "1fe888e851d6939416b2f1dc28e8d742d049c59ed58a8e9f5cf78b605707daea",
  "src/apps/api/app/services/slide_post_ai_delivery.py": "54fd2af23fe019caa3c04bbb27d7a2836762e9ade767bcb169454ae3a3f7ed79",
  "src/apps/api/app/routes/slide_post_ai_requests.py": "b2a0303ed3e08b2920642ee76a4766c30912667c2f9245fd2bf20b1afa134243",
  "src/apps/api/tests/routes/test_slide_post_ai_legacy_pending_postgres.py": "224b61bfacaab71d39b6360530df6f4976e3d5a671a27d457117da6806276338",
  "src/apps/api/tests/routes/test_slide_post_ai_requests_postgres.py": "feee0e62c9eb12fb2ef6920527e12b58247f1cc7233fff73c6a81ce11731fcb9",
  "src/apps/api/tests/slide_post_ai_kill_worker.py": "2b8ffe8f552b029c62c1adcc017eab7b0782a25caec859a75da90977e1969b90",
  "src/apps/ios/Kria/Core/SlidePost.swift": "6247fb92f224933707346ccb4031f5ea01011852fa8545cbaacba98f11c7ed04",
  "src/apps/ios/Tests/KriaTests/SlidePostAIRecoveryTests.swift": "7db3ac3c360289700bf899bbadae34ff1bf11ef42dcf1eeaa268880d9599ac20"
}
```

## Final focused execution receipt

From `src/apps/api`:

```sh
/tmp/nova-ai-db-safe-python -m pytest -q tests/routes/test_slide_post_ai_legacy_pending_postgres.py tests/routes/test_slide_post_ai_requests_postgres.py::test_legacy_pending_cannot_reconstruct_from_new_metadata tests/routes/test_slide_post_ai_requests_postgres.py::test_pending_receipt_resumes_once_and_unknown_never_reexecutes tests/routes/test_slide_post_ai_requests_postgres.py::test_busy_thread_keeps_pending_and_releases_claim_locks_without_deadlock tests/routes/test_slide_post_ai_requests_postgres.py::test_pending_cancel_is_per_original_intent_and_get_never_dispatches tests/routes/test_slide_post_ai_requests_postgres.py::test_concurrent_same_uuid_distinct_preparations_both_use_insert_winner tests/routes/test_slide_post_ai_requests_postgres.py::test_publication_batch_is_bounded_fair_and_has_no_attempt_cap --tb=short
/tmp/nova-ai-db-safe-python -m pytest -q tests/routes/test_slide_post_ai_requests_postgres.py --tb=short
```

Results: 23 passed/44 warnings/14.78s (17 new parameterized cases +6 existing); full existing file31 passed/44 warnings/17.16s. Six cases overlap: do not call this54 unique cases. Logs `/tmp/slide-ai-legacy-final-23-pg.log` and `/tmp/slide-ai-legacy-final-existing31-pg.log`. Both used existing guarded scratch schema; no migration ran. The started-provider control actually executes one counting fake, kills the process, fault-injects a legacy missing-envelope representation, verifies fresh-worker observation cannot repeat it, then publishes using the exact original persisted token in another process. This is fault injection, not execution of an old production binary or proof about a live backlog.

Offline command from repository root (same six-file impacted selection as predecessor):

```sh
src/apps/api/.venv/bin/python /tmp/slide-ai-offline-tests.py -q src/apps/api/tests/services/test_slide_post_compose.py src/apps/api/tests/services/test_slide_post_chat_edit.py src/apps/api/tests/routes/test_slide_post_ai_requests.py src/apps/api/tests/services/test_slide_post_ai_prepared.py src/apps/api/tests/services/test_slide_post_ai_requests.py src/apps/api/tests/tasks/test_task_time_limits.py --tb=short
```

125 passed/44 warnings/2.48s; fake-provider/offline scope only. Six changed Python files pass `ruff check` and `ruff format --check`; `git diff --check` passes. Contract/schema shapes were not changed by this correction; no generated schema or migration modified. The archived083/512 evidence remains historically bound, not silently promoted.

The new five client source controls cover unknown0→late completed0, slow pacing, Stop/reentry, unresolved409, conflicting local draft/replaced source, newer instruction and account switch. Native source guard retains paid output as an explicit proposal on changed source bindings; native tests have not compiled or executed. Current source selection723 app/39 package/6 UI, final-source build/model/aggregate/Linux/hosted/physical and rollout gates remain pending. Parent retains shared-model ownership; no formal or coverage file changed. Model correspondence must account for observer-only legacy pending and exact-token late publication; no model proof is claimed.

Product request context: the creator asked for automatic visible updates without refresh/reopen or a click to show the new version. Verified predecessor PR1817 (KRI-935) records the same owner rule; PR1816 (KRI-934) records the bounded failure-screen/model work. This local correction preserves that outcome without misrepresenting unknown paid execution as failure. Those historical issue references are verified context, not a claim that a new tracking assignment was created.

## Draft publication checkpoint (correction excluded)

After explicit new publication authorization and read-only confirmation that neither branch nor matching PR existed, reviewed commit51249def55de4ffc3b857c462d5fba2516da8606 was pushed and draft PR2137 opened: https://github.com/emirerben/nova/pull/2137. Initial check rollup:11 queued, no completed checks; drafttrue, head51249. The draft explicitly lists the uncommitted legacy correction, nested snapshot/authority must-fix, source proposal guard and selection correction as not included. No rollout/merge occurred. GitHub reported base main c0caeddd1a1f13d988ad4908e60995ab3b40ec94, newer than this branch's verified integrateddbb260e; a controlled integration remains required. The push hook's five nonblocking medium matches were inspected: four variable assignments/parameter annotations and one local fixture expression, not credential values. No hook bypass was used.

## Subsequent independent review finding — earlier green is not final readiness

Independent review found a copied valid manifest plus original agent_input can still pass supported() while baseline/context/postprocess inputs are missing. Missing baseline can consume the provider call before failing. Nonempty authority alone also fails to establish the complete original snapshot. The earlier23/31/125 receipts above are a real intermediate checkpoint, not evidence for the forthcoming nested-completeness correction. Before committing the correction, kind-specific original execution/postprocess and canonical authority completeness must be checked before claim and before bounded outbox selection; incomplete evidence stays unresolved. New red controls, final rerun and review are required. No rollout or merge is allowed by these intermediate passes.

Nested-completeness red controls:8 expected failures,17 deselected,44 warnings,3.95s (`/tmp/slide-ai-legacy-effective-input-red.log`). These retain a valid copied manifest while removing chat baseline/context/server_version/binding, invalidating baseline, removing compose fallback/previous_version, or dropping canonical authority owner. They check real durable claim/sweep/provider behavior; missing nested evidence must neither publish nor call the counting fake. This is the independent review's substantiated correction, not a general schema/scheduler rewrite.

Draft-body metadata validation (trusted unchanged validator, integrateddbb260e→published51249): `python3 scripts/ci/pr-evidence.py --repo . --base dbb260e47ef640b8c0908808df640723e0f8c1a2 --head 51249def55de4ffc3b857c462d5fba2516da8606 --body-file /tmp/nova-automatic-update-pr-2137-body.md` failed with `local_fix requires at least one passed implementation check with executed evidence`. The draft intentionally declares the broad final-source checks pending; no passed declaration was fabricated. After the correction is committed, a scoped actually executed backend receipt can be declared without claiming native/request fulfillment or rollout. This local validation failure remains a draft readiness gate, not a product test result or approval bypass.

## Final bounded independent source review

Reviewer reread the frozen correction and found both substantiated must-fixes resolved: kind-specific postprocessing/context/typed inputs are checked before paid claim; canonical authority and structural input checks precede the scanner limit. Fresh Python eligibility in cancel/reject prevents typed-invalid unknown rows being terminalized, while atomic SQL claim-evidence predicates remain. No remaining scoped source blocker was identified. SQL checks structure rather than recreating all Pydantic validation; typed-invalid rows cannot dispatch/cancel/reject, and persisted pacing prevents permanent starvation. The source-binding guard is statically approved, with native execution and same-ID source-byte limitations explicit. Review ran no tests or remote operations; final execution receipt below remains required before the separate commit.

Combined intermediate59 run:58 passed/1 failed/44 warnings/29.38s. The pre-existing batch-fairness fixture minted50 new row UUIDs while copying the original execution context/request UUID. The new immutable-identity guard correctly excluded those rows (one eligible row instead of50). The fixture must build each distinct original identity consistently (context, request input and digest); the guard is not relaxed. Intact chat and compose positives passed. This is recorded as a failed intermediate run, not hidden or relabelled final green.

## Final correction receipt — supersedes intermediate greens above

Independent runtime and final fixture review: approved, no remaining scoped source blocker. Final source parent is51249def55de4ffc3b857c462d5fba2516da8606; the separate correction commit is reported in the final handoff. No unrelated runtime, migration, flag, formal, generated-contract or main integration changes are included.

Executed from `src/apps/api`:

```sh
/tmp/nova-ai-db-safe-python -m pytest -q tests/routes/test_slide_post_ai_legacy_pending_postgres.py tests/routes/test_slide_post_ai_requests_postgres.py --tb=short
```

**59 passed,44 warnings,29.38s** (28 new parameterized controls plus31 existing), `/tmp/slide-ai-legacy-complete-final59-corrected-pg.log`. This replaces the58+1 fixture-mismatch run; no guard was weakened. The six-file offline command recorded above was rerun after the final guards: **125 passed,44 warnings,2.86s**, `/tmp/slide-ai-legacy-complete-final125-offline.log`. Seven changed Python files pass scoped Ruff/format (`/tmp/slide-ai-legacy-complete-final-lint.log`), and diffcheck passes. No active test process remains.

New source behavior: incomplete legacy rows remain durable pending, projected unknown, not dispatchable/rejectable/cancellable as unstarted. Full captured context, kind-specific execution/postprocessing inputs, typed baseline/agent inputs/binding and canonical authority fields are required before claim. SQL structural predicates filter incomplete rows beforeLIMIT; Python typed checks additionally fence mutations and execution. An exact persisted nonnull token can publish a late original result once; null/wrong/duplicate/cancelled controls cannot. No claimed work is reset or recreated. Valid chat and composition intents still publish, execute once and complete.55 unsupported older rows do not prevent eligible new work. Stop/new owner/request/source controls retain the original identity and prevent stale application. Tokenless old outcomes may remain unresolved indefinitely; no generic unverified late-result import is implemented.

Native changes are independently static-reviewed only: five new recovery controls and a source-binding guard that stages the paid result for review when media identities differ.723app/39package/6UI candidates remain uncompiled/unexecuted. Same-ID changed media bytes are not represented in the saved client baseline, so that case remains an explicit source-generation/rollout verification limit.

Final seven-file backend manifest canonical digest is6b97dfd082e393c1e788b7e4c28f1053cd2929e244d164d7b51eb3e1708ea3e6; nine source/control hashes below include the two native files. These bind the correction commit to the exact tested/static-reviewed source without rewriting archived51249/083 evidence.

```json
{
  "src/apps/api/app/routes/slide_post_ai_requests.py": "b2a0303ed3e08b2920642ee76a4766c30912667c2f9245fd2bf20b1afa134243",
  "src/apps/api/app/services/slide_post_ai_requests.py": "cb2204e2fa1d00d9dc26ff6aefa36cb370e1da35e48383ce59ede437e37211c6",
  "src/apps/api/app/services/slide_post_ai_delivery.py": "54fd2af23fe019caa3c04bbb27d7a2836762e9ade767bcb169454ae3a3f7ed79",
  "src/apps/api/tests/routes/test_slide_post_ai_legacy_pending_postgres.py": "c3ee793d2bfa93762084af4dfc9ad6f9c4eb6ecbee40c5e58caeb1ed175600b9",
  "src/apps/api/tests/routes/test_slide_post_ai_requests_postgres.py": "8c49ec41bf5b3177fc0e05e9034f089531fe37018b8f68af53b0330a739d2fe6",
  "src/apps/api/tests/routes/test_slide_post_ai_requests.py": "0cf25867f3bcb73611fcf2df082c59ca64970e2843dfc95dd0d04a1bd60b6138",
  "src/apps/api/tests/slide_post_ai_kill_worker.py": "2b8ffe8f552b029c62c1adcc017eab7b0782a25caec859a75da90977e1969b90",
  "src/apps/ios/Kria/Core/SlidePost.swift": "6247fb92f224933707346ccb4031f5ea01011852fa8545cbaacba98f11c7ed04",
  "src/apps/ios/Tests/KriaTests/SlidePostAIRecoveryTests.swift": "7db3ac3c360289700bf899bbadae34ff1bf11ef42dcf1eeaa268880d9599ac20"
}
```
