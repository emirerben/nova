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

The `reframe` stage also carries the heavy-source downscale guard's events
(`app/pipeline/source_guard.py`, v0.12.2.0): `source_guard_downscaled`,
`source_guard_downscale_failed`, `source_guard_budget_exhausted`.

The `silence_cut` stage (emitted from the tasks layer, `generative_build.py`)
carries the speech-cleanup engine's decisions: `silence_cut_config`,
`silence_cut_plan`, `silence_cut_bailout`, `silence_cut_clamped`,
`silence_cut_analysis_failed`, `silence_cut_apply_failed`,
`silence_cut_required_failed`, `silence_cut_skipped_disabled`,
`silence_cut_skipped_no_audio`, `silence_cut_rule2_disabled`,
`retake_detector_failed`. `silence_cut_clamped` (v0.59.2.0) records the
explicit-consent budget clamp: proposed vs delivered removal seconds plus the
budget (`proposed_removed_s` / `time_saved_s` / `clamp_budget_s`). Since
2026-09-08 that budget has no fraction ceiling (`MAX_REMOVAL_FRAC_REQUIRED`
went `0.55 → 1.0`), so a `silence_cut_clamped` event now means the
`MIN_OUTPUT_S` 3.0 s output floor bound the plan — the removals the detector
proposed would have left under 3 s of video. Triage a "cleanup left a pause"
report from this event first: a span present in `proposed_removals` but absent
from `removed` was found by the detector and declined by the floor. The
auto/legacy `MAX_REMOVAL_FRAC` 0.4 bailout (`silence_cut_bailout`) is a
different rail and is unchanged.

## Template-scoped sibling

`/admin/templates/{id}` has a "Debug" tab backed by `GET /admin/templates/{id}/debug`
that surfaces the same agent_runs (`template_recipe`, `creative_direction`, etc.) but
scoped to one template — usable before any job has referenced it. Uses the shared
`AgentSection` component (`src/apps/web/src/app/admin/_shared/`). Cap: 100 runs, DESC
(newest first).

## Proposal runs before a render job exists

`AgentRun.plan_item_id` is a nullable owner for proposal model calls (migration
0107). `RunContext(plan_item_id=...)` persists accepted and rejected raw model
output without inventing a render Job. Existing job/template/track/session owners
remain valid.

`GET /admin/plan-items/{id}/proposal-trace` requires admin authentication and returns
the ten latest runs plus `agent_runs_has_more`, direction, prompt version, and
private scheduling diagnostics. Use
`python scripts/admin.py GET plan-items/<id>/proposal-trace` locally or the same
command with `--prod`. Diagnostics include requested/effective frame counts,
semantic plan, selected windows, repairs and failure reason. They are absent from
ordinary plan-item responses. The existing redacted `/debug` endpoint stays small.

## Creation-thread transcript and turns (read-only)

Chat-first jobs are owned by a creation thread. Three additive admin surfaces let an
operator read what the creator actually said and what the runtime did, without guessing:

- `GET /admin/creation-threads/{thread_id}/events?limit=100&cursor=` — transcript events in
  `sequence` order (`limit` 1-500, default 100). Each event carries `sequence`, `kind`
  (`event_type`), `actor` (`role`), `created_at`, `revision`, `client_event_id`, `content`
  (the creator's message text), `payload` (chips, receipts, ...). An event that started a
  runtime-v2 turn also carries `turn_id` and the `brief_version` that turn produced.
  `next_cursor` is the last `sequence` returned; pass it back as `cursor`; `null` on the last page.
- `GET /admin/creation-threads/{thread_id}/turns?limit=50&cursor=` — runtime-v2 turns, oldest
  first (`limit` 1-200). Each turn returns the persisted `KriaTurnPlan` (`plan`), `status`,
  `error`, `brief_version`, its `executions` (tool receipts: `tool_name`, `status`, `result`,
  `error`, target job/variant/draft ids) and the de-duplicated `job_ids` they touched. A
  runtime-v1 thread has no turn rows and returns `turns: []`. `next_cursor` is an opaque
  `(created_at, id)` keyset.
- `thread_id` + `runtime_version` are added to `GET /admin/plan-items/{id}/debug` and
  `GET /admin/jobs/{id}/debug` (null when no thread owns the item/job), so a job traces to
  its thread directly (`services/kria_trace.find_thread_link`).

Same admin auth as `/integrity`. These routes return creator text, so treat output as
sensitive; signed storage URLs inside payloads/receipts are replaced by
`[redacted-signed-url]`. Examples:

```bash
python scripts/admin.py --prod GET creation-threads/<thread_id>/events
python scripts/admin.py --prod GET "creation-threads/<thread_id>/turns?limit=10"
```

## Eval harness opt-out

The eval RunContext sets `extra={"skip_agent_run_persist": True}` so replay-mode evals
don't pollute the prod `agent_run` table. Don't drop this flag.
