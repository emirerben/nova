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

- `item` — core fields: `item_status` (derived via `derive_item_status`, same as the
  live app), `edit_format`, `content_mode`, `montage_preset`, `current_job_id`,
  `has_voiceover` + `voiceover_gcs_path`, `scheduled_date`.
- `clip_gcs_paths` — count + full paths.
- `clip_assignments` — one summary per assignment: media id, gcs path, kind,
  duration/aspect, generation, and whether analysis has run (`has_analysis`,
  `analysis_version`).
- `pool_assets` — every `PlanItemAsset` row for the item (status, error_code,
  error_detail, analysis_attempt_count, etc).
- `jobs` — every `Job` row linked to the item via `content_plan_item_id`.
- `edit_proposal` — the reviewable guided-edit envelope (`status`, `proposal_version`,
  `brief`, `failure` with full detail, a `last_approved` summary, a `draft` summary).

## Conversation is redacted by design

`edit_proposal.conversation` (the briefing chat between the creator and the edit-guide
agent) is **never** returned verbatim. Each turn collapses to
`{role, phase, length, has_suggestions}` — the actual typed content is stripped before
serialization. Same treatment for `draft`/`last_approved`: only structural counts
(`beat_count`, `media_count`, `duration_s`) survive, not the creator's title/goal text.
This is intentional — this endpoint is admin-only but still must never put a creator's
own words in front of an operator, a screenshot, or a support ticket. If you need the
actual conversation content for debugging a specific proposal-generation bug, that
requires a separate, explicitly-scoped tool — don't extend this endpoint to include it.
