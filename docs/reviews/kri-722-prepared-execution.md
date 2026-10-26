# KRI-722: prepared execution snapshot identity

Written before implementation (repo testing rule). Each row names how the code
handles the failure and the boundary test that proves it.

Before: an approval named a draft id and revision. After approve, the Job got a
fresh `creator_generation_id` and a brief binding, but nothing tied the Job or
its published output back to the approval. Dispatch never rechecked source
generations, original/proxy lineage or capabilities. Receipts for a unified
montage read the LATEST brief, so later chat could change what an accepted
attempt reported. A retry had no shared authority id with the attempt it
retried.

After: `decide_approval` mints one immutable `creator_prepared_executions` row
(schema version 1) in the same transaction that approves. Dispatch claims,
device publication, receipts, prune and retry all reference it by id and verify
it against live rows. Each dispatch or retry gets its own attempt id under the
same snapshot id. Unstamped (historical or flag-off) work is byte-identical.

## Failure matrix

| # | Failure | Handling | Test |
|---|---|---|---|
| F1 | Approve -> Job -> artifact not traceable | Snapshot id on the approval row (FK), on `execution.result["prepared_execution"]`, and on the Job (`all_candidates` + `assembly_plan["creator_prepared_execution"]`) with approval/draft/attempt ids. Device records copy the stamp at pin time | positive control walks approval -> snapshot -> execution -> Job |
| F2 | Brief/chat edited after approval | Receipts read the brief from the Job's binding after checking it against the snapshot's binding digest, never `load_latest_brief_sync` | edit brief after approve; receipts unchanged |
| F3 | Draft edited after approval (new head) | existing `draft_stale` 409 at approve; at claim the snapshot draft hash must equal the live draft row | new head -> HTTP 409; tampered draft hash -> `prepared_execution_stale` |
| F4 | Source replaced at the same path (new generation) | `sources[].generation` recorded from PlanItemAsset / MediaAssetSource / clip row; compared at claim and retry | bump `PlanItemAsset.gcs_generation` after approve -> stale, no Job |
| F5 | Analysis-only proxy promoted to original / lineage mismatch | `sources[].role` recorded (`original`/`analysis_proxy`) and re-derived at claim | flip a clip's upload purpose -> `prepared_execution_source_lineage` |
| F6 | Capability/compiler change between approve and dispatch | `versions.worker_capabilities_digest` recomputed at claim; a schema or compiler version above the worker's constant -> `prepared_execution_unsupported` | change verified phone features -> stale; bump row schema -> unsupported |
| F7 | Duplicate approve/dispatch | approval row lock + pending check (409); UNIQUE(approval_id); claim idempotent | second POST -> 409, one snapshot; second claim -> ignored, one Job |
| F8 | Exact retry | retry approval references the SAME snapshot; claim mints a new attempt id; no planner | retry: snapshot id equal, attempt id differs, planner sentinel unused |
| F9 | Late completion of an old attempt | device publish guard: a stamped Job's record must carry the job's current snapshot + attempt; cloud renders are fenced by `creator_generation_id`, which the attempt stamp records | stale record attempt -> publish refused, last-good kept |
| F10 | Unknown submit outcome | `outcome_unknown` keeps the accepted execution and its attempt stamp; reconciler redelivers the same claim without re-fencing | `test_accepted_redelivery_is_not_cancelled_by_a_capability_change` (attempt unchanged, snapshot not invalidated) |
| F11 | Concurrent devices approve different revisions | head + revision checks at approve; only the head draft mints | approve rev N after rev N+1 exists -> 409, no snapshot |
| F12 | Foreign thread/source injection | snapshot built only from server rows; BriefBinding.resolve(thread) + creator id + epoch checks unchanged; snapshot owner = approval creator | foreign creator approve -> 404 |
| F13 | Replay makes a new decision | builder is a pure function of stored rows; no model call | build twice from the same rows -> identical digests |
| F14 | Legacy rows change | absent stamp => legacy branch; contract model untouched | unstamped Job keeps its assembly plan; contract digest unchanged |
| F15 | Model value recorded as creator origin | per-decision `origin` from provenance only: strategy groups and Kria editor drafts are `model_default`; `creator` only for the creator's own unchanged editor state, `creator_answer` for approval-card answers | every strategy group of the positive control -> `model_default` |
| F16 | Clearing one instruction invalidates all | `decisions` is a per-key digest map; `changed_decisions()` names only changed keys | removing the title -> only `text` changes |
| F17 | Secrets / signed URLs leak | validator rejects `http(s)://`, `X-Goog-Signature`, `token=` anywhere in the snapshot | signed URL in a clip path -> ValueError |
| F18 | Retention prunes a referenced draft | `prune_kria_drafts` keeps drafts referenced by a live snapshot | prune leaves the referenced draft body |
| F19 | Editor/speech_cut/device kinds | mint is kind-agnostic at approve; claim stamps the target Job for editor kinds too | editor approval -> stamp on the target Job |
| F20 | Flag off | `KRIA_PREPARED_EXECUTION_ENABLED=false` at approve: no row, no stamp | flag off -> no row |

## Scope limits

- Concrete montage/speech/text/audio decisions still resolve inside the render
  (KRI-723/724/725). This ticket records per-decision digests and refuses drift;
  it does not move the compiler into the snapshot.
- Cloud exact retry (no saved device recipe) stays KRI-723.
- `origin` is recorded here; enforcing creator-over-model precedence is KRI-824.
- Rolling deploy: an old worker ignores the stamp (legacy behaviour, the same
  exposure as KRI-470). A new worker refuses an unknown schema/compiler version.

## Evidence

Repeatable command (real HTTP + Postgres; provider transport and broker substituted):

```bash
cd src/apps/api && KRI722_TRACE=1 pytest tests/kria/test_prepared_execution_postgres.py -v \
  --junitxml=../../../.dev/kri-722-e2e.xml
```

It writes `.dev/kri-722-e2e.xml` and `.dev/kri-722-trace.json` (approval -> snapshot ->
execution -> Job -> attempt ids for the positive control and the retry). Red control:
with `prepared_execution.verify` stubbed to a no-op, the source-generation,
proxy-promotion, draft-mutation, capability and retry-source cases all fail, so the
fence is what refuses them.
