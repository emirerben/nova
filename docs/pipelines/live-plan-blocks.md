# Live plan blocks (KRI-443, contract v2 KRI-439)

After the creator taps Create on a v2 creation thread, the render pipeline reports what it
decided, section by section, as `plan_block` events in `CreationThreadEvent`. Clients read them
through the existing `GET /creation-threads/{id}/delta` poll, and a cold client reads the reduced
state from `GET /creation-threads/{id}/plan`. v2 runtime only. `pipeline_trace` is never exposed.

Dark behind `LIVE_PLAN_REVIEW_ENABLED` (`settings.live_plan_review_enabled`, default `false`).
Off means: no events, no new fields, no extra query, capability `false`, `cancel-render` and
`GET /plan` return 404 `live_plan_review_unavailable`, and the revision check is the strict
original one (byte-identical to before).
Kill switch: `fly secrets set LIVE_PLAN_REVIEW_ENABLED=false --app nova-video` + restart api and
worker.

Wire models live in `app/kria/plan_contract.py` (frozen; the Swift mirror is
`Kria/Core/PlanReviewModels.swift`). Payload builders: `app/kria/plan_payloads.py`. Event writer
and revision logic: `app/kria/plan_blocks.py`. Snapshot reducer: `app/kria/plan_snapshot.py`.

## Sections

`SECTION_ORDER = title, clips, captions, music, sfx, overlays, look, post_caption` (8).
`post_caption` is last and display-only in v2 (cannot be flagged or edited). Old clients drop the
unknown id (their total stays 7). `SCOPABLE_SECTIONS` is the first seven.

## Event contract

`event_type = "plan_block"`, `role = "system"`, `content = null`.

```json
{
  "turn_id": "uuid | null",
  "job_id": "uuid",
  "scope": ["captions"],
  "previous_job_id": "uuid",
  "blocks": [
    {
      "section_id": "title|clips|captions|music|sfx|overlays|look|post_caption",
      "state": "waiting|deciding|decided",
      "summary": "<=120 chars | null",
      "detail": "<=400 chars | null",
      "intent": false,
      "skipped": false,
      "decided_at": "iso8601 | null",
      "revision": 1,
      "changed": false,
      "payload": { "...": "see below" },
      "previous": { "revision": 1, "job_id": "uuid", "summary": "", "payload": {}, "skipped": false }
    }
  ]
}
```

- `scope` (non-null for a scoped update render) and `previous_job_id` are optional top-level keys;
  they ride on every event of the job, not only the first.
- `summary` and `detail` are still filled exactly as before, so old clients and the decode
  fallback keep working. `payload` is present only for `decided` + not `skipped`, and may be
  absent when it could not be built.
- One event may carry several blocks. `intent: true` means only a mode is known. `skipped: true`
  (always `decided`) means "Not used". `decided_at` is set only for `decided`.
- Signed URLs are never stored: `clips[].thumbnail_url` and `music.art_url` are filled by
  `GET /plan` only.
- A new `job_id` (a re-render) resets the feed on the client.

### Revision, `changed`, `previous`

Computed in ONE place, `plan_blocks._emit` (via `annotate_blocks`), after the thread row lock is
taken. Emit sites never compute them.

- The previous value of a section is its latest `decided` block from a different `job_id` in this
  thread's events. A decided block written before v2 has no `revision` and counts as 1.
- First decided value in a thread: `revision=1`, `changed=false`, no `previous`.
- Equal to the previous value (canonical JSON of the payload; if either payload is null, compare
  `summary` and `skipped`): same revision, `changed=false`.
- Different: `revision = previous.revision + 1`, `changed=true`, `previous` set.
- `waiting` / `deciding` blocks carry the revision of the value they will replace (0 if none).
- A re-emit within the same job never lowers a revision already written for it (the cloud path
  emits a summary first and the enriched payload at finalize).

### Reducer rule (server and client)

Per `job_id`, per section: a higher `revision` replaces the whole block; an equal revision only
moves forward (waiting -> deciding -> decided) and fills in, never blanks, `summary`, `detail`
and `payload`; a lower revision is ignored.

### Scoped updates never leak

A render path that re-runs the whole pipeline may re-report a section outside the scope.
`plan_blocks.freeze_out_of_scope` (in `_emit`, under the thread lock) drops any block for a
section that is outside the job's `scope` and was already `decided` by the dispatch copy, so the
copied value never turns into a fake "Updated" (log `plan_block_out_of_scope_ignored`). A section
with no previous value is still free to be filled. `scope` accepts `SCOPABLE_SECTIONS` only
(`post_caption` is dropped). The post caption is not generated alongside a scoped job.

The dispatch event is built inside a savepoint: if `dispatch_payload` raises, the dispatch
transaction falls back to the plain all-`waiting` event (log `plan_dispatch_payload_failed`)
instead of failing a render that is already queued.

### Scoped turns re-render the same Job (KRI-441, KRI-442)

A turn with `scope` is planned only as editor operations (`kria/plan_review.py`: scoped op
families + prompt line, op filter, post-compile lane repair) and commits through the editor
path, so it re-renders the SAME Job; there is no "previous job" to compare or restore from.
Two consequences, both handled in `plan_review.py`:

- **Feed:** `emit_scoped_update_feed` runs at dispatch (inside the dispatch savepoint, thread
  lock held) for a scoped editor commit or a section undo. Sections in scope whose value moved
  get a `decided` block with `revision + 1`, `changed=true`, `previous` (what they replaced),
  plus the `plan_update_summary`. Unmoved or out-of-scope sections are never written.
- **Undo:** the pre-update value of every changed lane is captured at compile time
  (`CompiledEditorDraft.before`) and stored on the draft's execution
  (`result["plan_review_before"]`); `POST .../plan/sections/{id}/undo` and `DraftUndoBody.render`
  restore from it and mint a render-only successor turn. The restore turn's source event carries
  `scope`, so the same feed path toggles the block back (revision + 1 again).

Known limits: blocks reflect the committed draft at dispatch, not the finished render (a failed
render leaves the changed block and the undo guard `variant.render_status == "ready"` blocks Undo
until a render succeeds); a later scoped update does not clear `changed` on sections it did not
touch; Undo only reaches the LATEST update's lanes.

### `plan_update_summary`

`role="system"`, `content=null`, payload `{turn_id, job_id, text, changed_sections}`. Appended
once at finalize (`emit_skipped_remainder`) for a job that has a `scope` and at least one
`changed` block. `text` is deterministic and localized with `say(en=..., tr=...)`, for example
"Updated captions and music." Best-effort. `turn_id` may be an empty string on the snapshot when
the event has none. The revision-tolerance check treats it like `plan_block`.

## Payloads (`PAYLOAD_MODELS[section_id]`)

Dumped with `model_dump(mode="json", exclude_none=True)`, at most 16 KiB. Over the cap the lists
are truncated (clips and caption lines to 40, sfx and overlays to 30; captions set
`truncated=true`, `count` keeps the real total), and if still too big the payload is dropped and
the block keeps its summary. Building a payload never raises.

| Section | Shape |
| --- | --- |
| `title` | `{text, highlight_word?, bar_id?}`. `bar_id` is the text element id (manual-edit target) |
| `clips` | `{total_duration_s, clips:[{index, media_id?, kind, role?, label?, start_s, end_s, source_start_s?, source_end_s?, transition?, transition_duration_s?, thumbnail_url?}]}` on the OUTPUT timeline. `transition` LEAVES the clip (null for the last clip). `role` is the guided moment `topic` (<=40). `label` is the overlapping `clip-label-*` / `montage-text-*` text |
| `captions` | `{count, lines:[{id, kind:"cue"\|"bar", text, start_s, end_s}], truncated}`. `cue` ids are `caption_cues[].id`, or `cue-<index>` when the cue has none; `bar` ids are text-element ids (narration/context labels, `caption_cue` bars) |
| `music` | `{source:"catalog"\|"user_song"\|"voiceover", mode?, track_id?, title?, artist?, bpm?, start_s?, art_url?, mix?}`. `artist`/`bpm` come from `MusicTrack` by `track_id`, best effort (`bpm = round(60 / median beat gap)`). `mix` has `music_level`, `original_level` (the footage's own sound; defaults follow the renderer: `editor_original_level`, else 0 under a creator song, else `editor_audio_level`), `music_gain_db` |
| `sfx` | `{count, items:[{id, label?, at_s, gain?}]}` |
| `overlays` | `{count, items:[{id, kind:"image"\|"video"\|"text_card"\|"motion"\|"visual", label?, start_s, end_s, display_mode?}]}` from `media_overlays`, `visual_blocks`, `motion_scenes` (frames / 30) |
| `look` | `{chips:[<=6 x <=24 chars], style_id?, look_preset?}`. Chips are derived from ids and enums only (style id, title font family, per-moment look presets), never from model text |
| `post_caption` | `{text, hashtags (no "#"), platform:"tiktok"}` |

### Transition vocabulary (KRI-447)

Display vocabulary: `cut | dissolve | whip | fade`, mapped from the editor and recipe wire value
with `plan_contract.TRANSITION_DISPLAY` (crossfade -> dissolve, dip_to_black -> fade, flash ->
whip, ...). `plan_payloads.display_transition` also maps the recipe-only `fade_black` and
`fade_white` to `fade`. Anything else (for example `wipe_left`) is `null`, never a guess. A guided
moment's `transition_after` wins; when it is unset the plan's `transition_policy` applies (`none`
is `cut`). The editor ops and the editor wire keep their own vocabulary
(`cut|crossfade|dip_to_black|flash`); the display vocabulary is read-only on the wire.

### Post caption (KRI-448)

`platform_copy` exists only on the template and music paths. For generative jobs:

1. Use `variant["post_caption"]` (or the job/plan-level one) when present.
2. Otherwise run `PlatformCopyAgent` in a background thread (`hook_text` = the decided title, else
   the thread intent; `has_transcript=False`), started when the title block is emitted so it runs
   alongside the render, with an 8 s wait at finalize. The result is persisted on every variant as
   `{text, hashtags, source:"platform_copy"}` (Job row lock only) and reported as `decided`.
3. Failure or timeout: `decided` + `skipped`. The agent's template-copy fallback is deliberately
   not used.

Metric `plan_post_caption{outcome: generated|from_variant|skipped|timeout}`.

## Producers

| When | Where | Event |
| --- | --- | --- |
| Approval dispatched | `_finish_approval_dispatch` (`tasks/kria_runtime.py`) via `plan_blocks.dispatch_payload` | all 8 sections, appended in the SAME transaction as `render_queued` (thread lock already held). Unscoped: all `waiting` (revision of the value they replace). Scoped (`payload.scope` on the turn's `user_message` event): sections outside the scope are `decided`, copied from the previous job (`changed=false`), sections in scope are `deciding`; the event carries `scope` and `previous_job_id` |
| Phone-guided (device render) | `_guided_execution_plan` (`tasks/generative_build.py`) | `music` deciding before the matcher; then all sections from the pinned plan (`blocks_from_guided_plan`; absent section = `decided` + `skipped`; `post_caption` is `deciding`). `_run_phone_guided_job` calls `emit_post_caption` once the request is committed |
| Phone narrated / voiceover montage / subtitled | `_run_phone_narrated_job`, `_run_phone_voiceover_montage_job`, `_run_phone_subtitled_job` | after the device request is pinned and committed (no lock held): all sections via `emit_phone_recipe_blocks` / `blocks_from_phone_recipe` (clip list with transitions from the recipe, caption cues, voiceover/track, `sfx`/overlay clips, title and look only when the recipe carries text), then `emit_post_caption` |
| Cloud guided / unified montage | `render_execution_plan(on_stage=...)` (`pipeline/guided_story.py`), reporter from `plan_blocks.make_stage_reporter` | paced to real stages: `clips` deciding at download, decided after assembly; `music` around the bed mix; `overlays` around the pretext lanes; `title`+`captions`+`look` around the text burn; `sfx` around the SFX pass. Payloads come from the pinned plan |
| Cloud (non-guided) path | `orchestrate_generative_job` | after clip analysis: `clips` decided (count summary only) + `title`/`look`/`music` deciding; after the text/style/music join: `cloud_decision_blocks` (title, look, music with payloads). At finalize `_enrich_from_variant` fills `clips` (from `ai_timeline`/`user_timeline` slots), `captions`, `sfx`, `overlays` from the first rendered variant, at the same revision |
| Job finalize | `_finalize_job` and the cloud finalize call site | `emit_skipped_remainder`: enrich from the variant, `emit_post_caption`, every section never decided is sent `decided` + `skipped:true`, then the `plan_update_summary` of a scoped update that changed something |

`plan_blocks.emit_plan_blocks(job_id, blocks)` is the only writer outside the approval
transaction. It is sync and best-effort: it never raises (a feed problem must not fail a render),
no-ops when the flag is off, the job has no v2 thread (matched by
`CreationThread.active_plan_item_id == job.content_plan_item_id`) or the job is `cancelled`, and
opens its own short session that locks ONLY the `CreationThread` row.

Hazards:

- NEVER call it while holding Job/PlanItem/Plan locks (the `record_pipeline_event` lock trap;
  Thread is last in `app/db_locks.py::CANONICAL_LOCK_ORDER`). Call it after the surrounding
  `db.commit()`, from the main render thread (not ThreadPool futures).
- The post caption persists with the Job row lock only, released before any thread-locking emit.
- The turn id is resolved from the execution that targets the job, falling back to the thread's
  `executing`/`observing` turn. It can be `null` only if a render emits before the approval
  dispatch commits.

## `GET /creation-threads/{thread_id}/plan`

Operation id `getCreationPlan`. Router `routes/kria_runtime.py`, no rate limit, read-only (no
locks, never bootstraps a draft), no admission check. Returns `PlanSnapshotOut`:

- It reduces the thread's `plan_block` events for the LATEST `job_id` (newest 600 events at most)
  with the reducer rule above. `blocks` is always all 8 sections once any event exists; sections
  with no event are `waiting`. A job that began before `post_caption` existed and has everything
  else decided shows `post_caption` as `Not used`.
- `editable` is true for a scopable section that is not skipped, when `status == "ready"` and the
  thread has an editable rendered variant (`drafts._target` resolves).
- `status`: `empty` (no `plan_block` events; `blocks` is `[]`), `planning`, `ready` (all 8
  decided), `updating` (latest job has a `scope` and is not fully decided), `cancelled` (a
  `render_cancelled` event names the job).
- `scope` is the latest job's scope while `updating`. `previous_job_id` is the previous job in the
  thread. `update_summary` is the latest `plan_update_summary` of the latest job.
- `draft` is `{draft_id, draft_revision, etag, can_undo}` of the head `CreatorEditDraft`, or null.
- `clips[].thumbnail_url` is signed for still images (found by `media_id` in the job's plan);
  video thumbnails come from the iOS media ledger. `music.art_url` is `MusicTrack.thumbnail_url`.
- `next_after_sequence` is the highest read event sequence (resume `/delta` from it).
- Errors: 404 `live_plan_review_unavailable` (flag off), 404 `kria_runtime_unavailable`, 404
  `thread_not_found`.
- Metric: `plan_snapshot_read{thread_id, status}`.

## Revision tolerance

Every event bumps `thread.revision`, so a client that has not polled the feed yet would 409 on
`submit_turn`, approve, and cancel. `conversation_revision_matches(db, thread, expected)`
(`services/creation_thread_titles.py`) keeps the original exact and +1-title tolerance, and also
passes when every event with `revision > expected` is a `plan_block` or `plan_update_summary`
(flag on only). Any other event in the gap is a real change and still 409s
`thread_revision_stale`. Used at `submit_turn`, approval decide, and `cancel_turn` in
`app/kria/runtime.py`.

## Capability

`GET /creation-threads/capabilities` carries `live_plan_review_enabled: bool` and
`live_plan_review_version: int` (2 for this contract with the flag on; 1 = feed only, which is
also what the flag-off response reports). iOS shows Review only when enabled and version >= 2.

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

## Metrics (KRI-453, structlog, no new infra)

`plan_block_emitted{job_id, sections, states, changed_sections}` (every append),
`plan_snapshot_read{thread_id, status}`, `plan_post_caption{outcome}`. The scoped-turn lane adds
`plan_review_scope_applied`, `plan_review_scope_repaired` and `plan_section_undone`. Message and
caption text is never logged.

## Guards

`tests/kria/test_plan_blocks.py` (contract shape, payloads and transitions, caps, revision
across two jobs, flag-off byte-identical blocks, reducer, update summary, cancel-render,
capability), `tests/kria/test_plan_snapshot_postgres.py` (real SQL: first render, scoped update
copy + changed/previous + undo toggle + update summary, signed URL fill, ownership and flag
errors, post caption generated/variant/failure/timeout, finalize enrichment, metrics),
`tests/tasks/test_phone_plan_blocks.py` (the three phone writers emit payloads),
`tests/kria/test_plan_review.py` + `tests/kria/test_plan_review_feed_postgres.py` (scope enforcement,
restore record, same-Job feed + undo toggle against real SQL),
`tests/routes/test_lock_order.py`.
