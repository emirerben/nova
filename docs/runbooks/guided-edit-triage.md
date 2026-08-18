# Guided-edit plan-item triage — runbook

## Purpose

`GET /admin/plan-items/{item_id}/debug` gives a durable, read-only snapshot of one
plan item's state so support/triage never needs raw SQL over SSH. Built after plan
item `85d1de16-ba11-4533-9290-927a45819cd3` was wedged with `edit_proposal.status ==
"failed"` and had to be hand-queried on the DB directly.

## Symptom: "user can't generate on a plan item"

**Step 1 — pull the snapshot.**

```bash
python scripts/admin.py --prod GET plan-items/<item_id>/debug
```

(swap `--prod` for local against a dev DB). Returns 404 for both an unknown id and a
malformed one — treat both the same, there's no separate "bad request" case.

**Step 2 — branch on what you see.**

- **Clips present + pool empty + `edit_proposal.status == "failed"`** — the guided-edit
  proposal build errored (see `edit_proposal.failure.code` / `.message` for why).
  Retry via the auto-design lane on Generate once the guided-auto-design lane is
  deployed; until then this needs a manual re-trigger of the proposal build.
- **Pool-only media (no `clip_assignments`, `pool_assets` non-empty)** — the user has
  uploaded footage to the pool but never ran it through the guided flow or promoted it
  with "Use in edit". Point them at either path; the endpoint's `clip_assignments` will
  stay empty until one of those happens.
- **A job stuck non-terminal (`jobs[].status` not `*_ready` / `*_failed` / `done` /
  `cancelled`)** — cross-check with the jobs list and cancel it:

  ```bash
  python scripts/admin.py --prod GET "jobs?content_plan_item_id=<item_id>"
  python scripts/admin.py --prod POST jobs/<job_id>/cancel
  ```

  The same `content_plan_item_id` linkage predicate powers both endpoints
  (`app/routes/admin_jobs.py::list_jobs` and `app/routes/admin_plan_items.py`), so the
  `jobs` array in the debug payload and the `/admin/jobs?content_plan_item_id=` list
  will always agree on which jobs belong to the item.

## What's in the payload

- `item` — core fields: `item_status` (derived the same way `derive_item_status` does
  live, cross-checked against the `jobs` array below rather than a separate full-Job
  fetch), `edit_format`, `content_mode`, `montage_preset`, `current_job_id`,
  `has_voiceover` + `voiceover_gcs_path`, `scheduled_date`.
- `clip_gcs_paths` — count + full paths.
- `clip_assignments` — one summary per assignment: media id, gcs path, kind,
  duration/aspect, generation, and whether analysis has run (`has_analysis`,
  `analysis_version`). `user_note` (creator-authored free text about the clip) is
  deliberately omitted.
- `pool_assets` — every `PlanItemAsset` row for the item (status, error_code,
  error_detail, analysis_attempt_count, etc). Analysis JSONB is not fetched.
- `jobs` — every `Job` row linked to the item via `content_plan_item_id`. Heavy JSONB
  columns (`assembly_plan`, `transcript`, `pipeline_trace`, etc.) are deferred — use
  `/admin/jobs/{id}/debug` for those.
- `edit_proposal` — the reviewable guided-edit envelope: `status`, `proposal_version`,
  `brief` (direction/pace/duration_s + `goal_length` — NOT the goal text itself),
  `failure` with full detail, a `conversation_attempt` presence flag (no token), a
  `last_approved` summary, a `draft` summary.
- `edit_proposal_unparseable` + `edit_proposal_raw_keys` — set when `PlanItem.edit_proposal`
  is a non-empty dict that fails schema validation (corrupted/legacy JSONB). Tells you an
  envelope exists and which top-level keys it has, with no values — treat this as "go look
  at the row directly if you need the content," not as a payload bug.

## Redaction is by design

Nothing that carries the creator's own typed words, or an internal write-fence secret,
survives into this response verbatim:

- `edit_proposal.conversation` (the briefing chat between the creator and the edit-guide
  agent) collapses each turn to `{role, phase, length, has_suggestions}`.
- `edit_proposal.brief.goal`, `draft`, and `last_approved` reduce to structural counts
  (`goal_length`, `beat_count`, `media_count`, `duration_s`) — never the title/goal text.
- `clip_assignments[*].user_note` is omitted entirely.
- `edit_proposal.conversation_attempt.token` (an internal write fence, see
  `app/schemas/edit_proposal.py`) is never returned — only `has_conversation_attempt`
  plus its `started_at`/version numbers.

This is intentional — this endpoint is admin-only but still must never put a creator's
own words, or an internal secret, in front of an operator, a screenshot, or a support
ticket. If you need the actual conversation content for debugging a specific
proposal-generation bug, that requires a separate, explicitly-scoped tool — don't extend
this endpoint to include it.

Filenames and GCS object paths (`clip_gcs_paths`, `clip_assignments[*].gcs_path`,
pool-asset `source_filename`) are the one exception and stay in the payload — they're
operationally necessary to actually locate the media during triage.
