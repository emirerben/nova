# Removing the dedicated `visuals` process group (KRI-1240 revert, #2183)

#2148 added an always-on Fly process group, `visuals` (`-Q visuals-analysis`, concurrency 6). #2183
removes it for cost. The unified `worker` (`--concurrency=1`) has drained `visuals-analysis` the
whole time, so Visuals keep working, one at a time.

Two facts make the removal unsafe as a plain merge:

1. **Old attempts carry old deadlines.** Each Visuals attempt stores a durable deadline
   (`plan_item_assets.analysis_deadline_at`) and its broker message carries the same `expires`.
   Before the lane-sized deadline change shipped, attempts were budgeted for two slots with no other
   work on the lane and a 60-min cap. Nothing extends a deadline: `claim_deadline` only shortens it,
   and the reaper fails an expired `queued` row without asking the broker. So an attempt dispatched
   under the old budget can time out if it has to wait on the single worker lane.
2. **Removal is force destruction.** The pinned flyctl (v0.4.94) destroys the machines of a group that
   left `fly.toml` with `Kill: true` before it updates the retained groups: no stop signal, no grace.
   A killed `analyze_pool_asset` leaves its row `analyzing`. The late-ack redelivery is refused (only
   `uploaded`/`queued` rows are claimable), so the row only ends as a retryable `analysis_timed_out`
   at its claim deadline (claim + 6 min for a photo, + 12 min for a video).

So capacity is removed only after (a) the lane-sized deadline code is live and every old-budget
attempt has ended, and (b) the visuals machines were stopped gracefully, with every attempt they
could have touched reconciled by id and token. Every step below is a production action that needs
its own go-ahead. Queries run against the production database (read-only).

## Preconditions

- **P1 – lane-sized deadlines are live.** The PR that sizes `pool_analysis_deadline` to the worker's
  one slot is merged, and its Fly Deploy run has **completed**. Write down that completion time as
  `T_A` (UTC). From `T_A` on, every api, worker and light machine stamps the new budget.
- **P2 – no deploy can touch the fleet.** Freeze merges to `main` until step 6 is done. Right before
  step 1, and again right before step 5, this must print `[]`:

  ```bash
  gh run list --repo emirerben/nova --workflow fly-deploy.yml --limit 30 --json databaseId,status --jq '[.[] | select(.status != "completed")]'
  ```

  The deploy workflow serializes deploys, but the manual stop/scale below does not take its
  guard. A queued or running deploy would restart or recreate the visuals machines mid-procedure.

## Steps

### 1. Wait until the old-budget cohort has ended

Run this until it returns no rows. An old-budget attempt lives at most 60 min (claim) + 12 min
(run) after its dispatch, and the visuals machines are still running, so this normally empties in
minutes:

```sql
-- cutover:old-budget-cohort
SELECT id, plan_item_id, kind, status, analysis_attempt_token, analysis_last_dispatched_at, analysis_deadline_at
FROM plan_item_assets
WHERE status IN ('queued', 'analyzing')
  AND (analysis_last_dispatched_at IS NULL OR analysis_last_dispatched_at < TIMESTAMPTZ '<T_A>')
ORDER BY analysis_last_dispatched_at;
```

Do not stop anything while it returns rows: those attempts need the dedicated capacity to finish
inside their old deadlines.

### 2. Record `T_start`, then inventory every attempt in flight

Write down the current UTC time as `T_start`. Then save the full output of this query (ids **and**
tokens) as **the inventory**:

```sql
-- cutover:inventory
SELECT id, plan_item_id, user_id, kind, status, analysis_attempt_token, analysis_last_dispatched_at, analysis_started_at, analysis_deadline_at
FROM plan_item_assets
WHERE status IN ('queued', 'analyzing')
ORDER BY analysis_last_dispatched_at;
```

### 3. Stop each visuals machine gracefully, one at a time

```bash
fly machine stop <id> --app nova-video --signal SIGTERM --timeout 300 --wait-timeout 6m
```

At v2145 the ids were `862102ae21e2d8` and `8edd63df731668`; confirm them with
`fly status --app nova-video`.

The signal contract, stated once:

- **Signal:** `SIGTERM`, named explicitly. It is also `kill_signal` in `fly.toml`, but plain
  `machine stop` documents SIGINT as its default, so the flag is passed. The app sets
  `REMAP_SIGTERM=SIGQUIT`, so Celery treats it as a **soft shutdown**, not a warm one.
- **Grace:** the worker waits up to 240 s (`worker_soft_shutdown_timeout`) for the tasks in flight
  at the signal. With the early-exit hook it stops waiting as soon as they finish. After that, any
  unfinished late-acknowledged message is restored. Fly sends SIGKILL at `--timeout 300`.
- **Wait:** `--wait-timeout 6m` makes the command return only once the machine is actually
  `stopped`. Without it (default 0) success means only that the API accepted the request.
- **Expected final state:** `fly machine status <id> --app nova-video` shows `stopped`. Its last
  exit event shows `requested_stop=true` and `oom_killed=false`.
- **Expected log** (`fly logs --app nova-video --machine <id>`): exactly one of
  - `Soft shutdown: no task in flight, skipping the 240.0s wait`, or
  - `Soft shutdown: in-flight task(s) finished after …s, ending the wait early`,

  followed by `worker: Cold shutdown (MainProcess)`.
- **Not clean:** `Soft shutdown: 240.0s elapsed, restoring unfinished task(s)`, any
  `WorkerLostError`, or a SIGKILL. The procedure still continues, but the affected attempt will show
  up as `needs_retry` in step 4.

A clean log proves the worker drained; it does not prove every attempt succeeded. That is what
step 4 checks.

A second in-flight snapshot after the stops is **not** enough: an attempt dispatched and
claimed during the stop window, killed, and timed out by the reaper before the snapshot is already
`failed`. The sequential stops can last longer than the 6-min photo claim deadline. So step 4
captures the whole window by dispatch time instead of by state.

### 4. Reconcile every inventoried attempt, 20 min after the last stop

20 min = the 12-min video claim deadline plus one 5-min reaper sweep plus slack. First list every
attempt dispatched since `T_start`, whatever its state now (a Retry re-dispatches, so it shows up
here too):

```sql
-- cutover:interval
SELECT id, plan_item_id, user_id, kind, status, error_code, analysis_attempt_token, analysis_last_dispatched_at
FROM plan_item_assets
WHERE analysis_last_dispatched_at >= TIMESTAMPTZ '<T_start>'
ORDER BY analysis_last_dispatched_at;
```

Together, the inventory (in flight at `T_start`) and the interval (dispatched since) cover every
attempt a visuals machine could have claimed during the cutover. Put every
`(id, analysis_attempt_token)` pair into the `VALUES` list: the inventory's token for its ids, and
the interval's current token for ids that are only in the interval:

```sql
-- cutover:reconcile
WITH inventory(id, token) AS (VALUES <(id, token) pairs, e.g. ('…'::uuid, '…')>)
SELECT i.id, a.plan_item_id, a.user_id, a.status, a.error_code,
       CASE
         WHEN a.id IS NULL OR a.status = 'cleanup_pending' THEN 'removed'
         WHEN a.status = 'failed' AND a.error_code = 'analysis_timed_out' THEN 'needs_retry'
         WHEN a.status = 'failed' THEN 'failed_other'
         WHEN a.status IN ('queued', 'analyzing') AND a.analysis_attempt_token = i.token THEN 'still_in_flight'
         WHEN a.status IN ('queued', 'analyzing') THEN 'new_attempt'
         ELSE 'done'
       END AS outcome
FROM inventory i
LEFT JOIN plan_item_assets a ON a.id = i.id
ORDER BY outcome, i.id;
```

| Outcome | Meaning | Action |
|---|---|---|
| `done` | analysed (`ready`, or a refresh/dedupe state) | none |
| `removed` | the creator deleted the Visual | none |
| `new_attempt` | the creator already tapped Retry | none; it runs on the worker with the new budget |
| `failed_other` | an ordinary analysis failure, not caused by the removal | none |
| `still_in_flight` | still `queued`, or `analyzing`, with the same token | a `queued` row waits on the worker under the new budget: none. An `analyzing` row 20 min after the last stop means the reaper did not run: check the `light` machine, then run this query again |
| `needs_retry` | timed out; possibly stranded by a stop | **recovery:** the Visual shows Retry in the app. Tell the creator (`user_id`, `plan_item_id`) to tap Retry or re-add the file. Retry mints a fresh attempt on the worker under the new budget. Nothing retries it automatically |

### 5. Remove the stopped machines

Run the P2 check again (`[]`), then:

```bash
fly scale count visuals=0 --app nova-video --yes
```

This destroys only the stopped machines, so there is nothing in flight on them. Afterwards,
`fly status --app nova-video` shows `api`, `worker` and `light` only.

### 6. Merge #2183

Its Fly Deploy finds no `visuals` machine to destroy. After it completes, `fly status` still shows
only `api`, `worker` and `light`. Then lift the merge freeze.

## Guards

`tests/integration/test_visuals_cutover_postgres.py` runs the four queries above, verbatim from this
file, against real PostgreSQL with the real upload route, task body and reaper:

- an old-budget queued burst times out if capacity shrinks before the cohort ends (red);
- the same burst, drained on dedicated capacity while the cohort query lists it, ends `ready`, and
  only then does a lane-sized burst on the single worker also end `ready` (control);
- reconcile classifies a hard-killed claim (`needs_retry`), a finished attempt (`done`), a
  re-tried one (`new_attempt`), a deleted one (`removed`) and a still-queued one
  (`still_in_flight`);
- an attempt dispatched AND claimed during the stops, killed, and timed out by the reaper before
  the stops end is missed by any in-flight snapshot but caught by the interval query
  (`needs_retry`).
