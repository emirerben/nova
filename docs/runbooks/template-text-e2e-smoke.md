# E2E smoke test — `nova.compose.template_text` on canary job `1dc849ab`

_Runbook version: 2026-05-17. Covers verification of PR #188 (v0.4.26.0)._

---

## Goal

Verify that the `nova.compose.template_text` agent — which runs as a parallel
second pass alongside `template_recipe` in the agentic-template build path — is
correctly wired in production after the v0.4.26.0 deploy. We reuse prod job
`1dc849ab-4ba8-41af-aa66-c065463bec90` as the canary because it was the
real-world failing case that motivated the agent: its template had visible text
overlays (lower-thirds, sticker text, watermarks) that `template_recipe` missed,
producing a rendered output with absent or wrong text. The new agent addresses
this by doing one focused extraction pass and replacing every slot's
`text_overlays` list with its output.

Two things must hold after the deploy:

1. **Agent run recorded**: the admin Debug tab at `/admin/templates/[id]`
   surfaces an `agent_run` row with `agent_name = "nova.compose.template_text"`,
   `outcome = "ok"`, and a non-empty `output_json.overlays` list.
2. **No render regression**: rendering the same template against the same
   (or representative) user clips produces output whose text positions are
   identical to the v0.4.25.0 baseline. Today the renderer still uses the
   named-position fallback (`top`/`center`/`bottom` derived from
   `text_extraction.py:_bbox_to_named_position`); the captured `text_bbox` is
   recorded for future use but does not yet drive render decisions. The bar is
   "no regression," not "improved positions."

---

## Prereqs

### Environment

| Variable | Value |
|---|---|
| `DATABASE_URL` | Prod Postgres URL (or a DB snapshot that contains the canary template row). |
| `GEMINI_API_KEY` | Required — the agentic build re-uploads the template video to the Gemini File API. |
| `STORAGE_BUCKET` + `STORAGE_PROVIDER` | Must point at the prod GCS bucket so the worker can download the template's `gcs_path`. |
| `REDIS_URL` | Must point at the prod (or staging) Redis instance so Celery tasks are enqueued and dequeued correctly. |
| `ADMIN_API_KEY` | Static admin token accepted in `X-Admin-Token` header — see `fly secrets list -a nova-video`. |

### Confirm the correct deploy is running

```bash
fly status --app nova-video
```

The output should show:
- `Image` digest corresponding to v0.4.26.0 or later (version ≥ 0.4.26.0 in
  `src/apps/api/VERSION`).
- Both the `api` and `worker` process groups showing `running` instances.

If the deploy is still in progress, wait until both groups are `running` before
continuing — the Celery worker must be on the new image or it will run the
pre-PR code even after you enqueue a task.

To confirm the worker is on the new image:

```bash
fly ssh console -a nova-video -g worker -C \
  'python -c "from app.agents.template_text import TemplateTextAgent; print(TemplateTextAgent.spec.prompt_version)"'
```

Expected output: `2026-05-17`. If the import fails or returns an older string,
the worker has not picked up the new image yet.

---

## Step 1 — Identify the source template

The canary job ID is `1dc849ab-4ba8-41af-aa66-c065463bec90`.

The `jobs.template_id` column (type `Text`, FK → `video_templates.id`) maps a
job to its template. Retrieve it via the Fly worker shell:

```bash
fly ssh console -a nova-video -g worker -C 'python -c "
from app.database import sync_session as _sync_session
from app.models import Job
import uuid

JOB_ID = \"1dc849ab-4ba8-41af-aa66-c065463bec90\"

with _sync_session() as db:
    job = db.get(Job, uuid.UUID(JOB_ID))
    if job is None:
        print(\"JOB NOT FOUND\")
    else:
        print(\"template_id:\", job.template_id)
        print(\"job_type:\",    job.job_type)
        print(\"status:\",      job.status)
        print(\"clip_paths:\",  (job.all_candidates or {}).get(\"clip_paths\", []))
"'
```

Save the printed `template_id` as `TEMPLATE_ID` for all subsequent commands.

Also note `clip_paths` — you'll need them in Step 4 to re-render.

---

## Step 2 — Trigger a re-build of the template via the admin API

The correct endpoint for agentic templates is:

```
POST /admin/templates/{template_id}/reanalyze-agentic
```

This endpoint (defined at `src/apps/api/app/routes/admin.py:1066`) validates
that `is_agentic=True`, resets `analysis_status → "analyzing"`, clears the
Redis requeue-guard counter (`analyze_attempts:{template_id}`), and enqueues
`agentic_template_build_task`. Do **not** use `/reanalyze` — that routes to
the manual path, which skips both `text_designer` and `extract_template_text_overlays`.

```bash
TEMPLATE_ID="<from Step 1>"
ADMIN_TOKEN="<your X-Admin-Token value>"
API_HOST="https://nova-video.fly.dev"

curl -s -X POST "${API_HOST}/admin/templates/${TEMPLATE_ID}/reanalyze-agentic" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" | python3 -m json.tool
```

Expected response: a `TemplateResponse` JSON with `"analysis_status": "analyzing"`.
If you see `"analysis_status": "failed"` with `error_detail` saying "non-agentic
template", the template row was created with `is_agentic=false`. In that case:

```bash
# Flip is_agentic=true first (raw SQL via worker shell):
fly ssh console -a nova-video -g worker -C 'python -c "
from app.database import sync_session as _sync_session
from app.models import VideoTemplate

TEMPLATE_ID = \"<paste template_id>\"
with _sync_session() as db:
    t = db.get(VideoTemplate, TEMPLATE_ID)
    t.is_agentic = True
    db.commit()
    print(\"is_agentic flipped:\", t.is_agentic)
"'
```

Then retry the `reanalyze-agentic` curl.

### Watch the build

```bash
fly logs -a nova-video --json -i worker | \
  grep -E "agentic_template_build|template_text_overlays_merged|template_text_extraction|agentic_recipe_version_created"
```

Expected happy-path log sequence (one line per event):

| Log event | What it means |
|---|---|
| `agentic_template_build_start` | Worker picked up the task. |
| `agentic_template_recipe_cache_hit` OR `gemini_upload_and_wait` events | Cache hit skips re-upload; cache miss uploads the source video. |
| `template_text_overlays_merged overlay_count=N agent_returned=M` | `extract_template_text_overlays` completed; N overlays merged into recipe slots. |
| `agentic_text_designer_baked overlays_styled=K` | `text_designer` ran on the label-like overlays. |
| `agentic_recipe_version_created trigger=reanalysis` | A new `TemplateRecipeVersion` row was written and `recipe_cached` updated. |
| `agentic_template_build_done` | Task finished cleanly. |

The full build takes 5–20 min depending on video length and whether Gemini's
File API is in the Redis cache (key namespace: `recipe+text` — distinct from
the `recipe-only` namespace used by manual templates).

---

## Step 3 — Verify in the admin Debug tab

Navigate to:

```
https://nova-video.vercel.app/admin/templates/<TEMPLATE_ID>
```

Select the **Debug** tab.

### What to check

1. **`template_agent_runs` list**: look for a row where `agent_name` =
   `"nova.compose.template_text"`. The list is sorted newest-first; the run
   from the re-build you triggered should appear at the top.

2. **`outcome` field on that row**: must be `"ok"`. Any other value
   (`"error"`, `"parse_error"`, `"terminal_error"`) means the agent ran but
   failed; check `error_message` for details.

3. **`output_json.overlays` list**: expand it via the JSON tree. The count
   must be > 0. For the canary template, the agent should surface the
   lower-thirds / sticker text / watermark overlays that `template_recipe`
   missed. Pay specific attention to any overlay whose `sample_text` matches
   the worst offenders from the original failing job (text that was visually
   prominent in the reference video but absent from the pre-PR rendered output).

4. **Each overlay in `output_json.overlays`** should include:
   - `role` — e.g. `"hook"`, `"label"`, `"cta"`.
   - `sample_text` — a non-empty string.
   - `start_s` / `end_s` — slot-relative floats (both ≥ 0, `end_s > start_s`).
   - `text_bbox` — object with `x_norm`, `y_norm`, `w_norm`, `h_norm`,
     `sample_frame_t` (all floats in [0,1] except `sample_frame_t` which is
     slot-relative seconds).
   - `font_color_hex` — six-digit hex string matching `^#[0-9A-Fa-f]{6}$`.
   - `_extracted_by` — must be `"nova.compose.template_text"`.

5. **Cached recipe JSON** (expandable at the bottom of the Debug tab):
   inspect `slots[*].text_overlays` — the entries written by the text agent
   will have `_extracted_by: "nova.compose.template_text"` and a populated
   `text_bbox` object. Entries that lack `_extracted_by` or have `text_bbox: null`
   were written by `template_recipe`, not the text agent.

6. **Overlay count on the template**: the `template_agent_runs` header shows
   total run count. Confirm the total across all slots matches the
   `overlay_count` from the `template_text_overlays_merged` log line.

---

## Step 4 — Render and diff against the v0.4.25.0 baseline

### 4-a: Obtain the v0.4.25.0 baseline output

The baseline is the output from the original job `1dc849ab-4ba8-41af-aa66-c065463bec90`
rendered on v0.4.25.0 (before `nova.compose.template_text` ran). Its output is stored
in `job.assembly_plan["output_url"]` (a GCS URL).

```bash
fly ssh console -a nova-video -g worker -C 'python -c "
from app.database import sync_session as _sync_session
from app.models import Job
import uuid

JOB_ID = \"1dc849ab-4ba8-41af-aa66-c065463bec90\"
with _sync_session() as db:
    job = db.get(Job, uuid.UUID(JOB_ID))
    plan = job.assembly_plan or {}
    print(\"output_url:\", plan.get(\"output_url\"))
"'
```

Download the baseline:

```bash
BASELINE_URL="<paste output_url from above>"
mkdir -p /tmp/text-e2e-smoke
gsutil cp "gs://$(echo $BASELINE_URL | sed 's|https://storage.googleapis.com/||')" \
  /tmp/text-e2e-smoke/baseline-v0.4.25.0.mp4
```

### 4-b: Trigger a re-render against the same clips

Use the `test-job` endpoint with the clips from Step 1:

```bash
TEMPLATE_ID="<from Step 1>"
ADMIN_TOKEN="<your X-Admin-Token>"
API_HOST="https://nova-video.fly.dev"

# Build clip_gcs_paths array from Step 1 output.
# Example — replace with real paths:
CLIPS='["clips/user-abc/clip1.mp4","clips/user-abc/clip2.mp4"]'

curl -s -X POST "${API_HOST}/admin/templates/${TEMPLATE_ID}/test-job" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"clip_gcs_paths\": ${CLIPS}, \"preview_mode\": false}" | python3 -m json.tool
```

Save the returned `job_id` as `NEW_JOB_ID`.

Poll for completion:

```bash
NEW_JOB_ID="<from above>"
fly ssh console -a nova-video -g worker -C "python -c '
from app.database import sync_session as _sync_session
from app.models import Job
import uuid

with _sync_session() as db:
    j = db.get(Job, uuid.UUID(\"$NEW_JOB_ID\"))
    print(\"status:\", j.status)
    print(\"output_url:\", (j.assembly_plan or {}).get(\"output_url\"))
'"
```

Download once status is `template_ready`:

```bash
NEW_URL="<paste output_url>"
gsutil cp "gs://$(echo $NEW_URL | sed 's|https://storage.googleapis.com/||')" \
  /tmp/text-e2e-smoke/post-v0.4.26.0.mp4
```

### 4-c: Compare

```bash
cd /tmp/text-e2e-smoke

# Duration and frame count
ffprobe -v error -show_entries format=duration -of default=nw=1 baseline-v0.4.25.0.mp4
ffprobe -v error -show_entries format=duration -of default=nw=1 post-v0.4.26.0.mp4

# SSIM (global score should be ≥ 0.95 for visually equivalent outputs)
ffmpeg -nostdin -loglevel info \
  -i baseline-v0.4.25.0.mp4 -i post-v0.4.26.0.mp4 \
  -lavfi ssim=stats_file=ssim.log -f null - 2>&1 | grep "SSIM"

# Side-by-side eyeball (macOS)
open baseline-v0.4.25.0.mp4 post-v0.4.26.0.mp4
```

**What to look at specifically:** text overlay positions. Because the renderer
still uses `_bbox_to_named_position` (top/center/bottom buckets) rather than
the raw bbox for placement, positions should be unchanged. If you notice any
text overlay appearing at a different vertical position, that is a regression
signal and should be investigated before the next deploy.

---

## Pass criteria

- [ ] `analysis_status = "ready"` on the template after the re-build completes.
- [ ] An `agent_run` row in the Debug tab with `agent_name = "nova.compose.template_text"` and `outcome = "ok"`.
- [ ] `output_json.overlays` is a non-empty list (count > 0) — the agent found at least one text overlay on the canary template.
- [ ] Each overlay in `output_json.overlays` has a populated `text_bbox` (all four norm fields + `sample_frame_t`) and a valid `font_color_hex`.
- [ ] `slots[*].text_overlays[*]._extracted_by == "nova.compose.template_text"` in the cached recipe JSON.
- [ ] SSIM ≥ 0.95 between baseline and post-deploy render.
- [ ] Text overlay positions (top / center / bottom bucket) match between baseline and post-deploy render — no position regressions.

---

## Failure modes and what to do

### No `nova.compose.template_text` agent run row in the Debug tab

The template was built before v0.4.26.0 deployed, or the agentic build task
ran on a worker that was still on the old image. Re-trigger:

```bash
curl -s -X POST "${API_HOST}/admin/templates/${TEMPLATE_ID}/reanalyze-agentic" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" | python3 -m json.tool
```

Confirm the worker is on the new image (Step 0 / Prereqs) before retrying.

### `outcome = "error"` or `outcome = "parse_error"` on the agent run row

The agent ran but failed to produce valid output. Check `error_message` on the
`AgentRunPayload`. Common causes:

- `TerminalError` wrapping a Gemini safety refusal — the template video contains
  content that triggers Gemini's moderation. The task catches this and returns
  `success=False, count=0`; the recipe keeps `template_recipe`'s overlays under
  the pre-text-agent behavior. This is not a regression — it is a known
  limitation noted in `template_text_extraction.py`. File a Gemini escalation
  ticket if the template is expected to work.
- JSON parse failure — Gemini returned malformed JSON that the 11 error branches
  in `TemplateTextAgent.parse()` could not salvage. Check the raw Langfuse trace
  for the `job_id = "template:{template_id}:agentic"` session to see what Gemini
  actually returned.

### `output_json.overlays` is present but empty (count = 0)

Two possibilities:

1. **Agent returned 0 overlays**: the template is genuinely text-free, or Gemini
   hallucinated a blank overlay list. Cross-check the template source video by
   scrubbing through it manually.
2. **Agent returned overlays but `_merge_overlays_into_slots` dropped them all**:
   check the Langfuse trace for `template_text_overlay_invalid` and
   `template_text_overlays_dropped` warning events under the same
   `job_id = "template:{template_id}:agentic"` session. High drop counts usually
   mean timing salvage failed — the agent returned `start_s > end_s` on every
   overlay. Open a bug; include the `raw_text` from the `AgentRunPayload`.

### Text overlay positions regressed (different top/center/bottom bucket vs baseline)

`_bbox_to_named_position` (in `template_text_extraction.py:100`) derives the
named position from `y_norm` using fixed thresholds (< 0.33 → top, > 0.67 →
bottom, else center). If positions differ from the baseline, something downstream
is consuming `text_bbox` as the primary positioning signal — meaning the renderer
follow-up PR shipped unintentionally or a different code path is active.

**Immediate action**: do not deploy further changes until the regression is
understood. Run:

```bash
grep -rn "text_bbox\|x_norm\|y_norm" src/apps/api/app/pipeline/ --include="*.py"
```

and confirm that no renderer code reads `text_bbox.y_norm` for positioning. If
a consumer is found that was not there on v0.4.25.0, revert it before the next
deploy.

### Template `is_agentic` is `false`

The canary job's template was created before `is_agentic` was introduced, or was
created via `POST /admin/templates` without `is_agentic: true`. Flip the flag
(one-liner in Step 2 above) and re-trigger. Note: flipping `is_agentic` on a
previously-manual template has no effect on existing cached recipes; the next
`reanalyze-agentic` call writes a new `recipe+text` namespace cache entry and a
new `TemplateRecipeVersion` row without touching the old one.

---

## Appendix: key file locations

| Purpose | Path |
|---|---|
| Agentic build task (trigger, flow) | `src/apps/api/app/tasks/agentic_template_build.py` |
| Text extraction + slot merge | `src/apps/api/app/tasks/template_text_extraction.py` |
| Agent class + prompt_version | `src/apps/api/app/agents/template_text.py` |
| Admin reanalyze-agentic endpoint | `src/apps/api/app/routes/admin.py:1066` |
| Admin debug endpoint | `src/apps/api/app/routes/admin.py:878` |
| Debug tab frontend | `src/apps/web/src/app/admin/templates/[id]/components/DebugTab.tsx` |
| `AgentRunPayload` schema | `src/apps/api/app/routes/_admin_schemas.py` |
| `Job` model (template_id, all_candidates, assembly_plan) | `src/apps/api/app/models.py:233` |
| `AgentRun` model (agent_name, outcome, output_json, template_id) | `src/apps/api/app/models.py:358` |
| Named-position fallback (`_bbox_to_named_position`) | `src/apps/api/app/tasks/template_text_extraction.py:100` |

---

_Last updated: 2026-05-17_
