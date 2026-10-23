# Worker-activity SQL fixture isolation — failure modes before repair

Fresh fullscreen head `54e287160e70faa4812bf16819e7c4f587e7a90b`, CI
39941425889, API shard job120373835040 failed the camera-roll approval case
`test_selection_prepares_common_draft_then_explicit_approval_freezes_execution[True-True-legacy]`.
PostgreSQL's deadlock report names the competing statement explicitly:
`UPDATE creator_agent_turns SET status = 'completed', updated_at = :old WHERE status IN (...)`.
It comes from `_quiesce_existing_activity` in the worker-activity SQL test,
not from the production guard. The same fixture first updates all active
creation_threads. The approval transaction then waits for its own thread while
the fixture waits for that transaction's turn row. Neither failure nor its
cause is dismissed as an unexplained scheduling flake.

Pre-repair failure modes and intended controls:

1. A fixture for a global query mutates or locks another test's committed rows.
   Keep an independently committed active thread, enter the activity fixture,
   and require another session's `FOR UPDATE NOWAIT` on that thread to succeed
   with its exact timestamp and status unchanged. The current fixture must fail.
2. Isolating data accidentally narrows the production query to one creator.
   Keep the production SQL byte-identical. Inside the isolated data set, activity
   belonging to a second creator must still make the global guard true.
3. The harness replaces PostgreSQL with a mock or loses date/status coverage.
   Keep the existing15real-PostgreSQL cases and every assertion unchanged. Use
   real PostgreSQL temporary tables copied from the current public definitions
   (columns, defaults, indexes and check constraints), not fabricated result rows.
   These tests cover the activity query, not foreign-key admission; the existing
   seed graph and the separate admission/Save tests remain.
4. Temporary data survives a case or contaminates a pooled connection.
   Create tables only in the case's connection/transaction and roll it back;
   temporary tables also use ON COMMIT DROP. No global UPDATE, public cleanup,
   timeout increase, assertion removal, new skip or blanket serialization.

No production source, Fly configuration, provider, phone or web-editor action is
part of this repair. Final checks and the exact new publication will be recorded
below after the red control and repair.

## Verified repair

The new cross-session control failed on the original helper with PostgreSQL
`LockNotAvailable: could not obtain lock on row in relation creation_threads`.
The repaired helper creates transaction-local PostgreSQL copies of the 12 tables
used by its seed graph. It no longer updates or locks another test's public
thread, turn, reservation, planning attempt or approval rows.

- All 15 original parameterized activity cases and their assertions are unchanged
  (the five test function ASTs match before/after).
- The 16 activity cases, including the new independent-session/global-second-owner
  control, plus all 8 camera-roll selection/approval cases pass: 24 passed, 0 skipped.
- The same 24 cases pass under two concurrent xdist workers: 24 passed, 0 skipped.
- The production activity SQL and application source are unchanged by this repair.
  Ruff, formatting and diff checks pass. There is no sleep, larger timeout,
  serialization, new skip or relaxed assertion.

Commands (task-owned migrated scratch PostgreSQL):

```sh
bash scripts/check-api.sh test tests/tasks/test_render_worker_activity_sql.py tests/integration/test_camera_roll_execution.py -q
bash scripts/check-api.sh test tests/tasks/test_render_worker_activity_sql.py tests/integration/test_camera_roll_execution.py -q -n 2
```

Final mandatory preship and fresh exact-head remote CI are still required after
this additive publication. Original N2/live-model/physical-output acceptance is
unchanged and remains open.
