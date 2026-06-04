# Admin job-debug view — runbook

## Purpose

Surfaces every agent's full I/O + every non-LLM pipeline decision per job.

- `/admin/jobs` — list
- `/admin/jobs/{id}` — detail. Use to answer: "is this bad output from an agent, the
  agent's parameters, or assembly?"

## Storage layers

### agent_run table

One row per agent invocation — written automatically by
`app/agents/_runtime._log_outcome` via `app/agents/_persistence.persist_agent_run`.
Captures input/output Pydantic dicts, full raw LLM response, outcome, tokens, cost,
latency. Best-effort: DB failure never breaks an in-flight job. Skips non-UUID job_ids
(e.g. `"track:<id>"` track-level analyses).

### Job.pipeline_trace JSONB column

Appended by `app/services/pipeline_trace.record_pipeline_event(stage, event, data)`.
Reads the current job_id from a contextvar set by `pipeline_trace_for(job_id)`. Capped
at 500 events/job.

## Adding pipeline events

```python
record_pipeline_event("<stage>", "<event_name>", {"...": ...})
```

Call from inside any `app/pipeline/*` module at any decision point. Stage buckets:
`interstitial`, `transition`, `overlay`, `beat_snap`, `reframe`, `audio_mix`,
`assembly`, `orientation`.

The `orientation` stage emits five events: `skipped`, `flag_stripped_no_rotation`,
`flag_stripped_no_rotation_180`, `normalized`, `disabled_by_env`.

## Template-scoped sibling

`/admin/templates/{id}` has a "Debug" tab backed by `GET /admin/templates/{id}/debug`
that surfaces the same agent_runs (`template_recipe`, `creative_direction`, etc.) but
scoped to one template — usable before any job has referenced it. Uses the shared
`AgentSection` component (`src/apps/web/src/app/admin/_shared/`). Cap: 100 runs, DESC
(newest first).

## Eval harness opt-out

The eval RunContext sets `extra={"skip_agent_run_persist": True}` so replay-mode evals
don't pollute the prod `agent_run` table. Don't drop this flag.
