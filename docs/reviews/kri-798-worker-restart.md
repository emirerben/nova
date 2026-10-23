# KRI-798 render-worker restart guard

Parent incident: KRI-829. This is a bounded lifecycle reliability fix, not a
new render feature or a production rollout.

## Recorded failure and boundary

Merope T1 job `0701fcc9-748b-4ac9-a640-130c125c0c17` recorded a worker stop at
`13:25:23.121`, render dispatch at `.956`, a Fly start/412 at `13:25:24.031`, and
the periodic backstop only at `13:27:24.8`. A render can therefore arrive while
the worker is stopping. The boundary is dispatch or active creator work → Fly
Machine state transition → render-worker availability. It does not prove the
video's requested picture, audio, correction, Save/reopen, or device export.

The implementation reuses saved, uncommitted September 23 lifecycle changes from
`nova-render-worker-lifecycle-race`, whose worktree was based on `c6ad208d7`;
that commit is not the lifecycle fix itself. It preserves the current
stop signal and merged Celery soft-shutdown behavior. The Fly wait request uses
the documented [`instance_id` and `timeout` parameters](https://docs.fly.io/machines/api/machines-resource/)
while a machine is stopping.

## Guarded behavior

`RENDER_AUTOSTOP_ENABLED` remains `false` by default; no production flag action
was performed. If that existing lifecycle rollout is authorized later,
`RENDER_START_WAIT_S=300` bounds the start retry window. The independently
default-true `RENDER_AUTOSTOP_ACTIVITY_GUARD_ENABLED` postpones an otherwise due
idle stop after a recent render dispatch or active creator work, including a
runtime-v2 `awaiting_approval` execution.

The activity check is intentionally global and bounded: it may leave the worker
running longer for unrelated recent creator activity, but cannot turn uncertain
activity into a stop decision. That cost tradeoff avoids the stop/first-render
race. The guard does not change the old soft-shutdown signal, start a machine by
itself, or authorize autostop.

## Focused regression coverage

- `tests/services/test_fly_machines.py`: one worker resolution, stopping-state
  wait, `instance_id`, bounded timeout, 412 retry, and fail-soft API behavior.
- `tests/tasks/test_render_worker_lifecycle.py`: disabled lifecycle, grace,
  dispatch and active-approval deferral, backstop start, and preserved stop
  behavior.
- `tests/tasks/test_render_worker_activity_sql.py`: the production activity SQL
  against PostgreSQL distinguishes recent work from stale work.
- `tests/test_render_worker_wake.py`: dispatch wake filtering and debounce keep
  a stopped worker from missing a first render.

The focused suite passed **78** cases: **63** isolated unit cases and **15**
real-PostgreSQL activity-SQL cases, with no skips. A baseline/current replay ran
the same three incidents against `a33c4b21` and the candidate: the baseline
failed all three; the candidate passed all three. The replay uses mocked Fly HTTP
state transitions and Redis fixtures, never a Fly API call.

Actual production idle-stop and first-render observations remain pending; no live
Fly action, physical device test, deployment, or original Merope acceptance is
claimed.
