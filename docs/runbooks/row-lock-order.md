# Row-lock order (deadlock prevention)

## Why this exists

On 2026-09-07 production returned 500s on `POST /creation-threads/{id}/media`:

```
asyncpg.exceptions.DeadlockDetectedError: deadlock detected
DETAIL:  Process 18773 waits for ShareLock on transaction 44786; blocked by process 18871.
Process 18871 waits for ShareLock on transaction 44785; blocked by process 18773.
```

Two request paths took the same two row locks in opposite order:

| Path | Order |
| --- | --- |
| `_reject_input_mutation_while_rendering` (media attach guard) | `PlanItem` → `Job` |
| `_prepare_partial_variant_retry` (retry-partial) | `Job` → `PlanItem` |

Concurrent requests each held one row and waited for the other. PostgreSQL
detected the cycle and aborted one transaction. Both sites date to `1f102edd`
(v0.59.4.0, #956) — pre-existing, not from the speech-cleanup work.

The deeper cause was that the codebase carried **two contradictory documented
orders**: `app/tasks/kria_runtime.py` declared "Global mutation order: Plan ->
PlanItem -> Job -> Session -> ...", while `app/routes/creator_agent.py`
declared "the route-wide lock order: CreatorAgentSession -> Job".

## The canonical order

One order now governs every `SELECT ... FOR UPDATE` on the creation graph. It
lives in `app/db_locks.py` as `CANONICAL_LOCK_ORDER`:

```
User → Persona → ContentPlan → PlanItem → PlanItemAsset → Job
     → CreatorAgentSession → CreatorAgentTurn → CreatorEditDraft
     → CreatorAgentApproval → CreatorAgentExecution → CreationThread
```

It follows ownership: parents before children, with the `CreationThread`
projection last because it is derived state that trails the rows it points at.
This is the Kria runtime's order, extended at the front with the ownership rows
and with `PlanItemAsset` between `PlanItem` and `Job`.

Rules:

- Acquire strictly in this order **within one transaction**.
- Re-locking a row the transaction already holds is a no-op — always allowed.
- Locking a subset is fine; skipping ranks never inverts the order.
- `commit()` / `rollback()` releases everything, so ordering restarts after one.

## How to take several locks

Use the helper rather than hand-ordering `db.get(...)` calls — it sorts
internally, so the call site cannot be typed wrong:

```python
from app.db_locks import acquire_locked_rows

locked = await acquire_locked_rows(
    db, {PlanItem: item_id, Job: job_id}   # any argument order is fine
)
item, job = locked[PlanItem], locked[Job]
```

`acquire_locked_rows_sync` is the Celery/synchronous-session equivalent.

## The guard

`tests/routes/test_lock_order.py` walks the AST of every module under `app/`
and reconstructs each function's lock sequence, following calls into
module-local helpers (the production inversion spanned two functions) and
splitting at `commit()`/`rollback()` boundaries. It understands
`acquire_locked_rows` calls, so wrapping a site in the helper does not hide it.

Two tests:

- `test_row_locks_follow_the_canonical_order` — fails on any inversion.
- `test_known_inversions_are_all_real` — fails when a `KNOWN_INVERSIONS` entry
  no longer inverts, so the debt list cannot outlive the debt.

`KNOWN_INVERSIONS` is a closed debt list of pre-existing sites, each with a
rationale. Adding to it should be a deliberate, reviewed decision.

## Remaining known inversions

Each needs its own restructuring; none is on the media-attach path.

| Site | Inversion | Why it wasn't fixed here |
| --- | --- | --- |
| `routes/creation_threads.py::delete_thread` | `CreationThread` first | The delete cascade is discovered *through* the thread row (`content_plan_id`, `active_plan_item_id`). Fixing it means re-reading the thread unlocked, locking children canonically, then re-verifying under the thread lock — a restructure of a destructive path. |
| `routes/creator_agent.py::_resolve_creator_overlay_asset` | `PlanItemAsset` after `Job` | Asset identities are only known after the agent's commands are parsed, which happens under the `Job` lock. |
| `tasks/generative_build.py::_lock_owned_entry_job` | `Job` before `ContentPlan`/`PlanItem` | The render orchestrator's ownership fence; 7 entrypoints depend on it. |
| `tasks/edit_proposal_build.py::_bind_creator_job_after_auto_design` | `CreationThread` before the plan/item/job graph | — |
| `routes/me.py::_provision_editor_plan` | `ContentPlan` before `Persona` | The `Persona` lock is on a conditional provisioning branch; hoisting it adds a lock to the early-return path. |
| `services/speech_cleanup_preflight.py::prepare_snapshot_mismatch_reanalysis` | `ContentPlan` before `Persona` | — |
| `routes/creator_workspace.py::decide_relevance_proposal` | `Job` before `PlanItem` | — |

`PlanItemAsset`'s placement relative to `Job` is genuinely split in the
codebase — measuring both placements app-wide gave 18 violating windows either
way. It sits before `Job` to match the ownership hierarchy.

## When a deadlock still happens

`app/main.py::transient_db_conflict_handler` maps SQLSTATE `40P01`
(deadlock_detected) and `40001` (serialization_failure) to a **409** with
`Retry-After: 1` and `"code": "concurrent_update"`, instead of a 500. The
driver's message is not echoed to the client. Every such response logs
`transient_db_conflict` with the SQLSTATE at warning level — a sustained rate
means an inversion slipped past the static guard.
