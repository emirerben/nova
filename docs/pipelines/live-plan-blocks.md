# Live plan blocks (KRI-443)

After the creator taps Create on a v2 creation thread, the render pipeline reports what it
decided, section by section, as `plan_block` events in `CreationThreadEvent`. Clients read them
through the existing `GET /creation-threads/{id}/delta` poll. v2 runtime only. `pipeline_trace`
is never exposed.

Dark behind `LIVE_PLAN_REVIEW_ENABLED` (`settings.live_plan_review_enabled`, default `false`).
Off means: no events, no extra query, capability `false`, `cancel-render` returns 404, and the
revision check is the strict original one (byte-identical to before).
Kill switch: `fly secrets set LIVE_PLAN_REVIEW_ENABLED=false --app nova-video` + restart api and
worker.

## Frozen event contract

`event_type = "plan_block"`, `role = "system"`, `content = null`.

```json
{
  "turn_id": "uuid | null",
  "job_id": "uuid",
  "blocks": [
    {
      "section_id": "title|clips|captions|music|sfx|overlays|look",
      "state": "waiting|deciding|decided",
      "summary": "short human string | null",
      "detail": "string | null",
      "intent": false,
      "skipped": false,
      "decided_at": "iso8601 | null"
    }
  ]
}
```

- One event may carry several blocks (fewer revision bumps).
- Display order: `title, clips, captions, music, sfx, overlays, look`. There is no post-caption
  section.
- `intent: true` means only a mode is known, not the final value.
- `skipped: true` (always with `state: "decided"`) means "Not used".
- `decided_at` is set only for `decided`.
- Client reducer rule: per `job_id`, a section's state only moves forward
  (waiting -> deciding -> decided), so duplicate or out-of-order events are harmless. A new
  `job_id` (a re-render) resets the feed.

## Producers

| When | Where | Event |
| --- | --- | --- |
| Approval dispatched | `_finish_approval_dispatch` (`tasks/kria_runtime.py`) | all 7 sections `waiting`, appended in the SAME transaction as `render_queued` (thread lock already held) |
| Phone-guided (device render) | `_guided_execution_plan` (`tasks/generative_build.py`) | `music` deciding before the matcher; then all 7 sections `decided` from the pinned plan (`blocks_from_guided_plan`; absent section = `decided` + `skipped`). The render is on the device, so the cloud decisions are the whole story |
| Phone narrated / voiceover montage / subtitled | `_run_phone_narrated_job`, `_run_phone_voiceover_montage_job`, `_run_phone_subtitled_job` (`tasks/generative_build.py`) | after the device request is pinned and committed (no lock held; early stale/cancelled returns emit nothing): all 7 sections `decided` from the pinned recipe via `plan_blocks.emit_phone_recipe_blocks` / `blocks_from_phone_recipe` (clip count + duration, caption cues, voiceover/track, `sfx`/overlay track clips, title and look only when the recipe carries text; the rest `Not used`). The phone unified montage hands its plan to the guided phone path, so it is covered above |
| Cloud guided / unified montage | `render_execution_plan(on_stage=...)` (`pipeline/guided_story.py`), reporter from `plan_blocks.make_stage_reporter` | paced to real stages: `clips` deciding at download, decided after assembly; `music` deciding/decided around the bed mix; `overlays` around the pretext lanes; `title`+`captions`+`look` around the text burn; `sfx` around the SFX pass. `music` is `deciding` before the matcher (in `_guided_execution_plan(emit_decided=False)`). Values come from the plan: catalog track, creator's own song (`user_song`), voiceover, caption/label counts |
| Cloud (non-guided) path | `orchestrate_generative_job` | after clip analysis: `clips` decided + `title`/`look`/`music` deciding; after the text/style/music join: `title`, `look`, `music` decided |
| Job finalize | `_finalize_job` and the cloud finalize call site | `emit_skipped_remainder`: every section never decided for the job is sent `decided` + `skipped:true` |

`app/kria/plan_blocks.py::emit_plan_blocks(job_id, blocks)` is the only writer outside the
approval transaction. It is sync and best-effort: it never raises (a feed problem must not fail
a render), no-ops when the flag is off, the job has no v2 thread (matched by
`CreationThread.active_plan_item_id == job.content_plan_item_id`) or the job is `cancelled`,
and opens its own short session that locks ONLY the `CreationThread` row.

Hazards:

- NEVER call it while holding Job/PlanItem/Plan locks (the `record_pipeline_event` lock trap;
  Thread is last in `app/db_locks.py::CANONICAL_LOCK_ORDER`). Call it after the surrounding
  `db.commit()`, from the main render thread (not ThreadPool futures).
- The turn id is resolved from the execution that targets the job, falling back to the thread's
  `executing`/`observing` turn. It can be `null` only if a render emits before the approval
  dispatch commits.

## Revision tolerance

Every event bumps `thread.revision`, so a client that has not polled the feed yet would 409 on
`submit_turn`, approve, and cancel. `conversation_revision_matches(db, thread, expected)`
(`services/creation_thread_titles.py`) keeps the original exact and +1-title tolerance, and also
passes when every event with `revision > expected` is a `plan_block` (flag on only). Any other
event in the gap is a real change and still 409s `thread_revision_stale`. Used at `submit_turn`,
approval decide, and `cancel_turn` in `app/kria/runtime.py`.

## Capability

`GET /creation-threads/capabilities` gains `live_plan_review_enabled: bool`.

## Cancel render

`POST /creation-threads/{thread_id}/turns/{turn_id}/cancel-render`
Body: `{"expected_thread_revision": int}` (same as `TurnCancelBody`). Response: `TurnCancelled`.

- Flag off: 404 `live_plan_review_unavailable`.
- 409 `turn_not_cancellable` when the turn is not `observing`/`executing`, the execution has no
  job, the job is not the thread's current one, the job status is not cancellable, or the job
  has no Celery task (a device render: iOS hides Stop for those).
- Cancels through `app/services/job_cancel.py` (extracted from admin `cancel_job`, which now
  calls it too): lock the Job, terminalize speech, set `cancelled`, commit, THEN revoke the
  Celery task and enqueue cleanup. Then, in a second transaction (Session -> Turn -> Execution
  -> Thread), the turn and execution become `cancelled`, the session returns to
  `awaiting_feedback`, and a `role=system` event `render_cancelled` `{turn_id, job_id}` is
  appended.
- Operation id `cancelCreationRender` in `app/cli/kria_contracts.py` (and the generated mobile
  OpenAPI subset).

## Guards

`tests/kria/test_plan_blocks.py` (contract shape, flag-off, never-raises, same-transaction
waiting blocks, revision tolerance, cancel-render, capability), `tests/routes/test_lock_order.py`.
