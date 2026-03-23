# Frontend + GTM Workstream — Cofounder Handoff

> **Owner:** ybyesilyurt
> **Reviewer:** emirerben
> **Created:** 2026-03-23
> **Status:** Ready to start
> **Branch:** Create `dev-frontend-gtm` from `main`

## TL;DR

Build the frontend for template-mode jobs + GTM infrastructure (UTM capture, confirmation emails). Emil is working on template output quality in parallel — zero file overlap between workstreams.

**Total effort with CC+gstack: ~3.5 hours.** Without AI: ~2 weeks.

## What You're Building

### Week 1 Deliverables
1. **Batch presigned upload endpoint** — `POST /presigned-urls`
2. **Template list endpoint** — `GET /templates`
3. **Template playback URL endpoint** — `GET /templates/:id/playback-url`
4. **Template Gallery Page** — `/template` route with visual cards
5. **Clip Upload Flow** — multi-file picker → parallel GCS upload → job creation
6. **Job Status Polling + Basic Result Viewer** — `/template-jobs/[id]`
7. **UTM Capture** — 3 columns on waitlist_signups + Alembic migration
8. **Confirmation Email** — Resend email task (fire-and-forget)

### Week 2 Deliverables
9. **Slot-Aware Video Preview Player** — timeline visualization below video
10. **Side-by-Side Template Comparison** — original template vs generated output
11. **Re-roll Endpoint + Button** — `POST /template-jobs/:id/reroll`
12. **Job History QA Dashboard** — `GET /template-jobs` list + table view

---

## API Contracts

### 1. POST /presigned-urls (NEW)

```python
# Request
{ "files": [{ "filename": str, "content_type": str, "file_size_bytes": int }] }

# Response
{ "urls": [{ "upload_url": str, "gcs_path": str }] }
```

- Per-file validation: max 4GB, content_type must be `video/mp4` or `video/quicktime`
- No duration/aspect_ratio validation at presign time
- Existing `POST /uploads/presigned` stays for the non-template flow — don't touch it
- Add to a new file: `src/apps/api/app/routes/presigned.py`

### 2. GET /templates (NEW)

```python
# Response
[{
    "id": str,
    "name": str,
    "gcs_path": str,
    "analysis_status": str,
    "slot_count": int,        # len(recipe_cached["slots"])
    "total_duration_s": float, # recipe_cached["total_duration_s"]
    "copy_tone": str,          # recipe_cached["copy_tone"]
    "thumbnail_url": str | None  # null in v1
}]
```

- Only return templates with `analysis_status = "ready"`
- Derive `slot_count`, `total_duration_s`, `copy_tone` from `recipe_cached` JSONB
- No auth required (public endpoint)
- If `recipe_cached` is None or corrupt, skip that template

### 3. GET /templates/:id/playback-url (NEW)

```python
# Response
{ "url": str, "expires_in_s": int }
```

- Returns a time-limited signed GCS URL for the template video
- Expires in 3600s (1 hour)
- Used by the side-by-side comparison view

### 4. POST /template-jobs/:id/reroll (NEW)

```python
# Request: {} (no body)
# Response: { "job_id": str, "status": "queued", "template_id": str }
```

- **Guard:** Return 409 if original job status ≠ `template_ready`
- Return 404 if job not found or not `job_type = "template"`
- Create new Job with new UUID, copy `all_candidates.clip_paths` and `template_id` from original
- Re-queue via Celery: `orchestrate_template_job.delay(new_job_id)`
- Seed is ephemeral — no DB change needed. The matcher naturally produces different results on re-run due to ThreadPoolExecutor ordering + moment tiebreakers

### 5. GET /template-jobs (NEW)

```python
# GET /template-jobs?limit=50&offset=0
# Response: { "jobs": [...], "total": int }
```

- Scoped to synthetic user_id `00000000-0000-0000-0000-000000000001`
- Ordered by `created_at DESC`
- Limit defaults to 50, max 100
- Note: existing `GET /template-jobs/:id/status` does NOT filter by user_id — that's intentional

---

## Assembly Plan Schema (READ THIS)

When you poll `GET /template-jobs/:id/status` and `status = "template_ready"`, the `assembly_plan` field looks like this:

```json
{
  "steps": [
    {
      "slot": { "position": 1, "target_duration_s": 3.5, "priority": 8, "slot_type": "hook" },
      "clip_id": "files/abc123",
      "moment": { "start_s": 2.0, "end_s": 5.5, "energy": 7.2, "description": "energetic intro" }
    }
  ],
  "output_url": "https://storage.googleapis.com/.../template_output.mp4",
  "platform_copy": {
    "tiktok": { "hook": "...", "caption": "...", "hashtags": ["..."] },
    "instagram": { "hook": "...", "caption": "...", "hashtags": ["..."] },
    "youtube": { "title": "...", "description": "...", "tags": ["..."] }
  },
  "copy_status": "generated"
}
```

**Important:** `clip_id` is a Gemini Files API path (opaque string). Display it as "Clip 1", "Clip 2" etc. — it's not a URL you can resolve.

`steps` are sorted by `position` (temporal order for the video).

---

## Frontend Specs

### Template Gallery Page (`/template`)

- Visual cards in a grid
- Each card: template name, slot count, duration, tone
- When `thumbnail_url` is null (v1): render a gradient placeholder colored by `copy_tone`
  - casual → warm orange gradient
  - energetic → red/pink gradient
  - calm → blue/teal gradient
  - formal → dark gray gradient
- Click card → proceed to clip upload flow for that template

### Clip Upload Flow

1. Multi-file picker (accept `video/mp4`, `video/quicktime`)
2. Call `POST /presigned-urls` with file metadata
3. Parallel `PUT` to GCS signed URLs with per-file progress bars
4. **If any upload fails:** show error + "Retry" button. No partial job creation. No orphan cleanup needed.
5. On all success: `POST /template-jobs` with `clip_gcs_paths` from presigned response

### Slot-Aware Timeline Player (MVP Spec)

Render below the `<video>` element:
- N colored horizontal segments, each proportional to `steps[i].slot.target_duration_s`
- Color by `slot_type`: hook=blue, broll=gray, outro=green
- Label each segment with `slot_type`
- Thicker border = higher `priority`
- Click segment → seek video to that slot's cumulative start time
- Vertical scrubber line shows current playback position
- Below timeline: "Slot {position} · Clip {n} · {target_duration_s}s"

### Side-by-Side Comparison

- Two `<video>` elements side by side
- Left: original template (use `GET /templates/:id/playback-url`)
- Right: generated output (use `assembly_plan.output_url`)
- Sync play/pause buttons (play both simultaneously)

### Re-roll Button

- Collapsed disclosure below result: "These don't look right?"
- Click expands to show "Try different clips" button
- Calls `POST /template-jobs/:id/reroll`
- Navigates to new job's status page
- Max 2 re-rolls per original job (enforce in frontend, not backend)

### Job History QA Dashboard

- Table: job_id, status, template name, created_at, link to result
- Pagination controls
- Label as "QA Dashboard" — this is an internal tool

---

## GTM Infrastructure

### UTM Capture

- Alembic migration: add 3 nullable `Text` columns to `waitlist_signups`
  - `utm_source`, `utm_medium`, `utm_campaign`
- Frontend: extract UTM params from `window.location.search`, pass as query params on form submit
- Backend: read from request query params, store in DB. NULL when absent.

### Confirmation Email

- New env var: `RESEND_API_KEY` (add to `.env.example` with description)
- New file: `src/apps/api/app/tasks/email.py`
- Celery task: `send_waitlist_confirmation(email: str)`
- Fire-and-forget: log errors, don't retry
- Email: subject "You're on the Nova waitlist", body: value prop + "we'll reach out when your spot opens"
- Dispatch from `POST /api/waitlist` after successful DB insert

---

## Coordination Rules

1. **If you change the Job model or any API response schema, tell Emil immediately.** The pipeline writes `assembly_plan` — if you change how the frontend reads it, you need to align.
2. **Don't touch these files** (Emil's workstream):
   - `src/apps/api/app/pipeline/*`
   - `src/apps/api/app/tasks/template_orchestrate.py`
   - `src/apps/api/app/tasks/orchestrate.py`
3. **Test alongside each endpoint.** Emil prefers too many tests over too few. Write unit tests for every new route.

---

## Quick Start

```bash
git checkout -b dev-frontend-gtm
# Backend: start with the batch presigned endpoint
# Frontend: start with the template gallery page
# Use CC to generate the boilerplate — it's all standard FastAPI + Next.js patterns
```

Good luck. When you ship the basic result viewer (Week 1, Day 4), ping Emil — he'll start using it immediately for quality QA.
