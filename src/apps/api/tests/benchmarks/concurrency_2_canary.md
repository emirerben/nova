# Fly `--concurrency=2` canary protocol

Phase 2 of the multi-clip template speedup plan. This document is the
operational runbook for flipping `worker --concurrency=1 → --concurrency=2`
in `fly.toml:35` once Phase 1 is stable in production. It is **not** a test
file — it's a checklist that lives in-tree so the next person who touches the
worker concurrency setting has the incident history at their fingertips.

Plan: `~/.claude/plans/our-multi-clip-templates-tingly-pelican.md`, Phase 2.

---

## Background: why we're at concurrency=1 today

PR #103 (`fly.toml:29-34`) dropped Fly workers from `--concurrency=2` to
`--concurrency=1` after incident `d018d1c3` (May 2026). The symptom: a
curtain-close encode that took 14s locally ballooned to >600s in prod when two
jobs ran concurrently on the same worker. Root cause was `geq` filter
contention — the curtain animation rendered per-pixel via the FFmpeg `geq`
filter, and two `geq` processes on a 2-shared-CPU VM fought for cache lines
hard enough to wreck throughput.

PR #105 fixed the underlying cause at the *filter* level by replacing `geq`
with a pre-rendered PNG sequence composited via `overlay`. With single-pass
M2-M6 landed (commit `efc58cf` → `cf2cabb`), the *process* level is also
fixed: one ffmpeg process per job instead of 3-7 in multi-pass means two
concurrent jobs no longer trigger the same multi-process contention class.

So `--concurrency=2` is now structurally safer. The canary is to confirm it.

---

## Prerequisites (must all be true before starting the canary)

- [ ] Single-pass `settings.single_pass_encode_enabled=True` flipped at the env level for at least 2 weeks.
- [ ] All production templates have `video_templates.single_pass_enabled=true` and their parity gate has passed (SSIM ≥ 0.98 on the reference clip set).
- [ ] No `single_pass_unsupported_fallback_to_multi` warnings in the last 7 days of Fly logs.
- [ ] `scripts/aggregate_phase_timings.py` baseline run on the last 100 jobs captured (the "before" snapshot for comparison).
- [ ] Off-hours window: no scheduled product launches, no large traffic ramps queued.

---

## Pass / fail criteria (decided up-front, not after the fact)

Pin these BEFORE flipping the flag. If we don't commit to acceptance criteria
in writing, the bias toward "well, it kind of worked" wins every time.

### Hard fail — revert immediately
- Any single job's `_assemble_clips` median elapsed_ms > 2× the pre-canary
  baseline for the same template_id.
- Any single ffmpeg subprocess exits with `subprocess.TimeoutExpired` (the
  exact incident #d018d1c3 fingerprint).
- p95 of Celery queue depth grows monotonically over 4+ hours (jobs queueing
  faster than the worker drains).

### Soft fail — investigate, may proceed conditionally
- p95 `_assemble_clips` elapsed_ms ↑ 10–30% vs baseline. Could be normal
  variance on a low-sample-size canary; widen the window before deciding.
- `slot_render_done` elapsed_ms variance ↑ noticeably (Phase B's
  ThreadPoolExecutor inside one job is now competing with a sibling job).

### Pass
- p95 `_assemble_clips` elapsed_ms ≤ baseline + 10%.
- Throughput (jobs/hour per worker machine) up by at least 1.5× during peak.
  (Not strictly 2× — the second concurrent slot has overhead.)
- No new error patterns in `fly logs --app nova-video | grep -i error`.

---

## Step-by-step

### Step 1 — capture baseline (the "before")

```bash
# From a machine with fly CLI auth
fly logs --app nova-video --json | \
  python src/apps/api/scripts/aggregate_phase_timings.py --by template_id \
  > /tmp/concurrency-canary-baseline.md
```

Commit `/tmp/concurrency-canary-baseline.md` to a private Notion page or
shared doc with the timestamp and current `fly.toml` SHA. This is the
reference point for comparison after the flip.

### Step 2 — scale to one canary machine

By default Fly auto-scales workers; to get a single-machine canary we
explicitly pin one machine first.

```bash
# Count current worker machines
fly machine list --app nova-video --process-group worker

# If >1 machine exists, the canary takes only ONE of them
# Update fly.toml on a feature branch (do NOT merge to main yet)
```

Edit `fly.toml`:

```toml
[processes]
  worker = "celery -A app.worker worker --loglevel=info --concurrency=2"
```

Deploy to a single machine:

```bash
fly deploy --app nova-video --process-group worker \
  --strategy bluegreen --max-unavailable 1
```

If `fly deploy` doesn't support per-machine rollout in your version, the
alternative is to scale to 1 worker first, deploy the change, then watch
that lone worker. Restoring the count after the canary is a second
`fly scale count` command.

### Step 3 — monitor for 24 hours

Three things to watch, every 4 hours:

1. **Phase timings** — re-run the aggregator:
   ```bash
   fly logs --app nova-video --json --since 4h | \
     python src/apps/api/scripts/aggregate_phase_timings.py --by template_id
   ```
   Compare median + p95 against `/tmp/concurrency-canary-baseline.md`.

2. **Error grep** — any new patterns:
   ```bash
   fly logs --app nova-video --since 4h | grep -iE 'error|timeout|subprocess|killed' | sort -u
   ```

3. **Throughput** — jobs/hour per machine. Pull from the admin dashboard or:
   ```bash
   fly logs --app nova-video --since 4h | grep template_job_done | wc -l
   ```

### Step 4 — decide

After 24 hours: apply pass/fail criteria above. Three paths:

- **Pass** → roll fleet-wide. Merge the fly.toml change to main; redeploy all
  workers; document the result on the same Notion page; keep the rollback
  procedure in step 5 close at hand for the first 72 hours.
- **Soft fail** → extend canary to 72 hours, sample more data. If still
  ambiguous, revert (cost is one commit; safer to redo than to ship).
- **Hard fail** → revert immediately (step 5). Document the failure mode.
  The likely cause is a contention class single-pass didn't retire; capture
  enough log evidence to identify it before reverting.

### Step 5 — rollback procedure

```bash
# Revert fly.toml to --concurrency=1
git revert <commit-sha>
fly deploy --app nova-video --process-group worker
```

If a job is mid-render when the rollback fires, Fly waits up to the
kill_timeout (30m) for it to finish before SIGKILL. The orchestrator's
1800s Celery hard timeout already bounds runaway encodes, so worst case a
job is force-killed and the user gets a "processing" status that times out
on its own.

---

## What this canary does NOT cover

- **Memory pressure under concurrency=2.** Two ffmpeg processes on a worker
  with `cpus=2 memory=2048MB` (per `fly.toml`) is tight. We have no
  instrumentation for peak RSS today. If OOM kill becomes visible during
  the canary, add `tests/benchmarks` RSS tracking as a follow-up.
- **Per-template traffic mix shifts.** If the canary lands on a quiet day
  and the production mix at flip time differs, the canary's "1.5× throughput"
  number may not generalize. Watch the first 72 hours of fleet-wide rollout
  to confirm.
- **The interaction with autoscaling.** Fly autoscales worker count by queue
  depth. With concurrency=2, the autoscaler may keep fewer machines around
  for the same queue, changing cold-start latencies. Track first-job-after-
  scale-up timings during the rollout, not just the canary.

---

## History (append rows as runs happen)

| Date | Outcome | Notes |
|------|---------|-------|
| _TBD_ | _pending_ | First canary run |
