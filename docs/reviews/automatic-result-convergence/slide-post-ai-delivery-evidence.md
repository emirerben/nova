# Original SlidePost AI pending delivery — component evidence

Base: `789789214046faf15c5e944c2d85cc5badc35666`. This is a local recovery fix, not native-output or production verification. No live provider, broker, private media or native tool was used.

## Observable contract

A server-accepted, never-claimed request retains its original execution envelope and is delivered without another user click. Duplicate HTTP/worker delivery can claim it only once. Running or unknown outcomes never receive another paid execution. GET is observation-only. An authenticated cancel can win only before that permanent claim. A new instruction is required after a definitive rejection; uncertain outcomes remain attached to the original ID.

Preparation freezes agent input, original draft/binding, composer ordering/kinds/manual edits/fallback/aspect, attribution and exact postprocessing inputs before committing pending. Execution reads the INSERT winner. A compatibility manifest pins executor code, rendered prompt/skills, model/spec/thinking and parser flags. Canonical owner/plan epoch/project/active-item/draft/source pins are checked under locks before claim; thread and source contention use NOWAIT and preserve pending. Changed or unsupported inputs reject before paid work. Existing pending rows without the full original envelope are safely rejected rather than reconstructed.

The AI row is its outbox. A sweep reserves the next deadline before every publication, takes at most 50 rows, isolates publication errors and backs off 30–300 seconds with no finite publication cap. Paid claims are permanent. Existing recovery/slides/chat/richtext rollback switches pause applicable pending execution while receipts remain readable.

## Executed checks

All PostgreSQL commands used `/tmp/nova-ai-db-safe-python`, exact database `nova_auto_convergence_ai_20261106_test`, loopback port 5432 only, pool cap 2, disabled dotenv and denied external sockets. Tests replace the provider with counting/parser fakes. No shared or production database was used.

- Initial pending-envelope regression: 1 expected failure, 9 deselected, 2.90s. A busy claim persisted no original effective input.
- Legacy same-version draft drift regression: 1 expected failure, 10 deselected, 2.73s. Retry actually called the fake provider against changed content.
- Rollback-switch regression: 4 expected failures, 24 deselected, 3.68s; each erroneously completed instead of pausing. `/tmp/slide-ai-delivery-rollback-red.log`.
- Migration: guarded `-m alembic upgrade head`, success `0137 -> 0138` in the isolated scratch DB only.
- Final PostgreSQL: `-m pytest -q tests/routes/test_slide_post_ai_requests_postgres.py --tb=short`, **31 passed in 29.02s**. `/tmp/slide-ai-delivery-pg-final.log`.
- Final offline: `src/apps/api/.venv/bin/python /tmp/slide-ai-offline-tests.py -q src/apps/api/tests/services/test_slide_post_compose.py src/apps/api/tests/services/test_slide_post_chat_edit.py src/apps/api/tests/routes/test_slide_post_ai_requests.py src/apps/api/tests/services/test_slide_post_ai_prepared.py src/apps/api/tests/services/test_slide_post_ai_requests.py src/apps/api/tests/tasks/test_task_time_limits.py --tb=short`, **125 passed in 5.31s**. `/tmp/slide-ai-delivery-offline-final.log`.
- Guarded `-m app.cli.kria_contracts --write` then `--check`: passed. `/tmp/slide-ai-delivery-contract-{write,check}.log`.
- Scoped Ruff check and format check: passed, 17 Python files already formatted. `/tmp/slide-ai-delivery-ruff-final.log`. `git diff --check`: passed.

Final process controls use a fresh interpreter (`tests.slide_post_ai_kill_worker`) with its own exact scratch-DB/socket guard, disabled dotenv, fresh NullPool connections and explicit fake provider. Parent SIGKILLs at a signaled boundary: after pending commit before enqueue, before claim, after committed claim, after fake-provider invocation, and after durable completion. Duplicate fresh workers during a claimed operation do not invoke the fake provider. Earlier fork-based results were provisional and superseded.

Other PG controls cover lost publication acknowledgement, persisted backoff, bounded/fair batches beyond attempt 100, actual concurrent distinct-envelope INSERT contention, source-lock contention, cancellation/claim race, changed source/config/project/draft/ownership, late original-token completion, exact uppercase UUID identity and pure GET observation.

## Limits and rollout

API and worker must run the same compatible executor/prompt configuration; incompatibility rejects pending work before claim. Apply migration 0138 before enabling admission. The existing recovery flag remains default-off; enabling it requires deployed API, worker and beat registration. Delivery assumes the database, broker, scheduled sweep and compatible worker eventually become available. Unknown paid outcomes deliberately remain non-replayable. This does not assert real provider success, native rendering/export, deployment or user exposure.

Native compatibility changes and six new test methods are written but **uncompiled/unexecuted** under the coordinator's native hold. Backend approval does not approve those client changes. Client GET checks contain no POST fallback; the separate first-admission path requires an atomically never-attempted journal record. Optional receipt delivery version preserves completed legacy-result reads; new admission requires the new delivery capability.

## Frozen backend fingerprints

Sorted JSON manifest digest: `132c11409a7bc0af0ccfcc7757f506c2b7d2bf0a1da8d2c42e5db371807df206`. Reproduce with `sha256(json.dumps(manifest, sort_keys=True).encode("utf-8"))`, including Python's default separator spaces. Root verified all 19 file hashes against the frozen source.

```json
{
  "src/apps/api/app/models.py": "8b5d3de95f2b50751803e0eb88db38e82c7310a60505c327f97a31a57ed9f511",
  "src/apps/api/app/routes/plan_items.py": "4b86344762de15ec121547a627ce6b22a470f80d66263f3cf5de778cb07b91f9",
  "src/apps/api/app/routes/slide_post_ai_requests.py": "e363404eeae19ab2154af1eb11eddca63c3aac867f87a63623722212ac0b063c",
  "src/apps/api/app/services/slide_post_ai_requests.py": "a2a422ee5f8436a2d61ea4a238946d75c0b7523f88645a7bfce57d6a3ebfab8c",
  "src/apps/api/app/services/slide_post_ai_prepared.py": "8e5410aae08752c7b3f90f37bf62649c30142beefd8039bef5260d89b00f8fe0",
  "src/apps/api/app/services/slide_post_ai_authority.py": "a5f0212040c7d796c03b80fa9c8ce4b2cb38a041022c49cfbd9f6c289d896ce7",
  "src/apps/api/app/services/slide_post_ai_delivery.py": "88e57f4b34b0200041686601ea0af8e1e50c03ff314d9a676273d78e11b9ea2c",
  "src/apps/api/app/services/slide_post_chat_edit.py": "968c7ebabfb83ead270992cf46db0f2763fdecefd0ccbfab6b8b9ea015e0ebed",
  "src/apps/api/app/services/slide_post_compose.py": "e9f157e42d3043336a5491be45f98118a700463e997fa2587ce5f2af3e73d315",
  "src/apps/api/app/tasks/slide_post_ai_delivery.py": "62b1a53a7ade0ec2f159d6b6eb489735909efaf343267ab20f500184f46a03f7",
  "src/apps/api/app/worker.py": "e6648e208696adde8547c6bd088ad65bc44c2944419ccb773c6b211c8c86a04f",
  "src/apps/api/app/migrations/versions/0138_slide_post_ai_delivery.py": "606b7f2d153f2b28c2e0731a4e89eb25271afb42637031a33d38685ba8a2fcec",
  "src/apps/api/app/cli/kria_contracts.py": "cafabd77732f4a39e60f835b647992c4928d78ac91ede27712a6927e413610e6",
  "src/apps/api/tests/routes/test_slide_post_ai_requests.py": "0fdc863ae8bf5094145119c10b47bd203a33654c311f26069a97bd0f3d521e56",
  "src/apps/api/tests/routes/test_slide_post_ai_requests_postgres.py": "f8744f8233c6f022a204b14fdfaa746dcfab5c5b408dae2c4a5d00044f6880c8",
  "src/apps/api/tests/services/test_slide_post_ai_prepared.py": "6a1ea77337e13fa372a6e5d427990ebf842591e0e2a07da824bc03d24d9cb4c8",
  "src/apps/api/tests/slide_post_ai_kill_worker.py": "cccbdc4b90243cc1ba945b6c7518179fcf94779e46a16aa2a1ecf0ad39e57806",
  "src/apps/api/tests/fixtures/kria_mobile.json": "b71b4f4f6e4f452fa4b9f2f158716355e33eaf2bbb1e18d64d71e77e98649da6",
  "src/apps/ios/Kria/Generated/openapi.yaml": "1960a31fa6175f29eadbbdf0676f3e458c70c817f1ebcfb3261cbd6fe15fd98d"
}
```
