# Instrumentation integration checkpoint

The continuation source at `23f8a3296866bc67c40e4a34e355af0d7c3710ce`, tree `36006df3eefcaaf898c30e126db598e6014bd6a3`, is combined with verified main `dbb260e47ef640b8c0908808df640723e0f8c1a2`. This main adds #2120 instrumentation (`5b21ad6bd99cee559824aef48f607d0dda4d3582`) and release metadata after `c078f13b9ad6f0f60d4a5e94f4cd2afac551a76b`.

The earlier controlled integration anchor is `c0ab9630a3abdaf59c3892ed6bc9ed90ce8c4c74`, tree `126815c7e426c8cace46ef48a09c9e5cce9f1fca`. Its second parent is full main anchor `c078f13b9ad6f0f60d4a5e94f4cd2afac551a76b`, the metadata-only child of Cunda merge `70bff5f3738ca8f6873e3a85ddd9fbad374ada49` (tree `684c329c20e001ce307ed2a217d9051e9177658d`). No Cunda replay or unmerged speed/split stacking occurred.

## Conflict and source review

Only DeviceRenderCoordinator.swift conflicted. Resolution retains measured asset resolution, export, fingerprint and publication calls. `continuationStage = exportStarted` is durably persisted before entering the instrumented exporter invocation. `outputReady` is recorded after fingerprinting and before localReady persistence. The safe-continuation guards, original attempt identity and unknown-output restrictions remain unchanged.

Independent Services/instrumentation review found its auto-merge limited to an editorCommit timing wrapper and optional diagnostic fields. Captured-owner Review/Slide/AI request-to-refresh/send validation remains intact. Perf.measureAsync preserves actor isolation, invokes its body once, rethrows and closes telemetry in defer; it adds no retry or detached execution. Background upload/device publication instrumentation preserves existing owner/identity ordering. NativeSession/Slide/Chat continuation source has no incoming main delta.

Root independently reviewed the coordinator composition. Source diff checks passed. This is static review, not native compilation or runtime proof. No native/package/model/simulator/device or paid execution was launched.

Integration committed at `789789214046faf15c5e944c2d85cc5badc35666`, tree `d21b82e26b5c8d0529f35ba4588dd8e42b6ac2fb`, parents `23f8a3296866bc67c40e4a34e355af0d7c3710ce` and `dbb260e47ef640b8c0908808df640723e0f8c1a2`.

## Component checks executed on 789789214

Guarded offline selection: **205 passed**, 44 warnings, 3.80 seconds; `/tmp/nova-auto-789-integration-offline.log`. From repository root:

```sh
src/apps/api/.venv/bin/python /tmp/slide-ai-offline-tests.py -q \
  src/apps/api/tests/kria/test_unknown_dispatch_recovery.py \
  src/apps/api/tests/routes/test_saved_user_song_state.py \
  src/apps/api/tests/routes/test_slide_post_ai_requests.py \
  src/apps/api/tests/services/test_slide_post_ai_requests.py \
  src/apps/api/tests/services/test_suggestion_serve_wiring.py \
  src/apps/api/tests/test_mobile_auth_contract.py
```

Guarded isolated AI PostgreSQL selection: **10 passed**, 44 warnings, 3.88 seconds; `/tmp/nova-auto-789-ai-pg.log`. From src/apps/api: `/tmp/nova-ai-db-safe-python -m pytest -q -rs tests/routes/test_slide_post_ai_requests_postgres.py`. This runner restricts SQL to the private synthetic AI test database and denies external provider connections.

Generated contracts: `/tmp/nova-ai-db-safe-python -m app.cli.kria_contracts --check` exited zero and reported matching tool, mobile fixture and mobile OpenAPI artifacts; `/tmp/nova-auto-789-contract-check.log`. These are component checks, not aggregate, decoder, native, package, UI, device or model proof. Future AI delivery edits require their own source-bound checks.

## AI admission follow-up

Existing pending AI rows retain original request_input, but resolved_input is first written with the permanent running claim. Pending proposal requests may omit baseline draft and asset ordering/descriptions; chat preparation also resolves bindings, dimensions and feature settings later. Therefore those legacy pending rows lack a complete original execution envelope and must not be reconstructed from current data.

The bounded delivery correction must freeze a versioned original envelope before committing a new pending admission, retain immutable request identity and payload, and deliver only never-claimed work through a durable outbox/queue. GET remains pure observation. Claimed/unknown outcomes are never re-executed. Current account/project/source and Stop authority must be checked before the committed unique claim. Legacy missing-envelope handling remains explicit. Database/process/fault-boundary proof and independent review are required before readiness.
