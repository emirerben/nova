# Controlled main integration

## Checkpoints

| Snapshot | Commit | Tree |
| --- | --- | --- |
| Reviewed catch-up, AI and song work | `62ac28e0909f69fb2531dc4101a7be6081ef7cee` | `0f518899dfd20b726b7a455500da6c4390d18561` |
| Independently reviewed Device rearm | `86e1afb04c2b1104ad52eee4ab9d5d78a0c2885c` | `2aafa93d10cf5bc70a451984ff0e290395dceca2` |
| Independently reviewed Review rearm | `8fc83e2a395f367cca6207630f133274451c5e14` | `8c48dfb3d98a5cf2ed6964f247f7da64c6c6c256` |

No native compilation or tests ran for these read-rearm snapshots. Device has nine new written regression methods; Review has ten. Their source approval does not establish runtime or model proof.

## Verified incoming main

`git fetch origin main` observed `c078f13b9ad6f0f60d4a5e94f4cd2afac551a76b`. It is a release-metadata-only child of Cunda merge `70bff5f3738ca8f6873e3a85ddd9fbad374ada49`, whose exact tree was verified as `684c329c20e001ce307ed2a217d9051e9177658d`. It inherits #2109 and the previously coordinated `e090eb668124fee58cfc74c34e662ac78e268342` source-replacement/caption changes. Cunda was inherited through main, not replayed from its old branch. Speed and split remain separate incoming dependencies.

A clean pre-merge working tree was merged with `git merge --no-commit --no-ff c078f13b9ad6f0f60d4a5e94f4cd2afac551a76b`. Twenty-two textual conflicts occurred, all in NativeEditorSession.swift. Other shared files merged textually and still received semantic review.

## Session reconciliation

The session author preserved canonical Cunda editorLoadEpoch/EditorOperationIdentity and nested loadEpoch propagation. beginEditorLoad is the single reset point for latest-job/merge/song fallback and Save-journal invalidation. The outer activeSaveAttemptID transaction remains authoritative; the bounded Save-history journal composes beneath it. Response/error/defer mutation requires current epoch, target and journal ownership.

Review identified an additional composition race: isSaving becomes false between a first 409 and the outer guarded reload/retry. Latest-job draining now requires activeSaveAttemptID to be nil at entry and after every asynchronous read/adoption gate. The owning outer Save defer clears its attempt before draining. Three-way needsResolution remains distinct from Cunda superseded, and prior render consent cannot silently apply to a newer generation.

Written, unexecuted regression: `NativeEditorSessionTests.testLatestJobWaitsForWholeConflictSaveAttemptThenRechecksAutomatically`. Existing incoming tests were retained. No conflict markers remain; scoped source diff checks passed. Native compilation/execution remains a required gate.

## Backend semantic reconciliation

Main's source replacement retains item.current_job_id while marking the old Job superseded in thread state. The old unknown-dispatch observer could therefore announce a replaced take as ready without a newer turn. A deterministic fake-backed regression reproduced this: `/tmp/slide-source-replacement-red.log` records one failed replaced-source case and one passing unrelated-marker control in 4.07 seconds. The correction checks source_replacement_supersedes before accepting recovered output; it performs observation, not dispatch.

Retain strict AI's owner/item-to-Thread NOWAIT/savepoint locking: incoming source replacement still has Thread-to-Item paths. Incoming suggestion state updates use Thread SKIP LOCKED, and editor-save suggestion refresh occurs after commit. Main Talking-caption processing and candidate SlidePost immutable render publication occupy separate paths and both remain present. Combined contract verification must retain main media-replace/suggestion registrations and candidate AI/timeline registrations; no generated file may be selected wholesale from one parent.

Executed merged-source guarded offline command:

```sh
src/apps/api/.venv/bin/python /tmp/slide-ai-offline-tests.py -q \
  src/apps/api/tests/kria/test_unknown_dispatch_recovery.py \
  src/apps/api/tests/routes/test_saved_user_song_state.py \
  src/apps/api/tests/routes/test_slide_post_ai_requests.py \
  src/apps/api/tests/services/test_slide_post_ai_requests.py \
  src/apps/api/tests/services/test_suggestion_serve_wiring.py \
  src/apps/api/tests/test_mobile_auth_contract.py
```

Result: **205 passed**, 44 warnings, 5.01 seconds; `/tmp/slide-main-integration-offline.log`. This is a component selection, not the aggregate suite. Scoped Ruff check/format and diff checks passed. The source-replacement fix was independently reviewed by root.

From src/apps/api, guarded `/tmp/nova-ai-db-safe-python -m app.cli.kria_contracts --write` and `--check` both exited zero; `/tmp/nova-auto-main-contract-write.log` and `/tmp/nova-auto-main-contract-check.log`. The checker reported matching tool, mobile-fixture and mobile-OpenAPI artifacts.

The first merged-source AI PostgreSQL attempt exited five with one module skipped (database not reachable from the default sandbox). It is **not passing PostgreSQL evidence**. An escalated guarded scratch-database attempt was requested separately; its result is pending and must be recorded before attributing concurrency proof to this source.

## Readiness limits

The three additional P1 observation gaps in read-recovery-evidence.md remain explicit: native latest-job/watch exhaustion, strict AI observation exhaustion, and generation-receipt observation exhaustion. Integration is a checkpoint, not broad automatic-convergence or publication readiness. Required gates include their correction/review, meaningful final-source native execution, affected backend and PostgreSQL checks, supported Linux decoder checks, aggregate checks, shared model correspondence/verification, incoming speed/split integration and hosted CI. No paid provider, private project, physical device or production data was exercised.
