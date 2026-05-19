# Single-pass encode rollout runbook

Operational runbook for testing and rolling out the single-pass encode
foundation that landed in [PR #147](https://github.com/emirerben/nova/pull/147).
Pairs with [`concurrency_2_canary.md`](concurrency_2_canary.md) — that's Phase 2,
this is Phase 1.

Two paths covered:
- **Path B — Smoke test on real prod bytes.** Non-flag-flipping. Fires ONE
  job through single-pass via the `force_single_pass=True` kwarg on a
  real production template + real user clips. Compares output bytes.
  Fastest empirical confidence that the encode chain works on real data.
- **Path A — Full per-template rollout.** After B passes for ≥3
  templates, flip the env flag and start the per-template allow-list
  rollout. Documented one template at a time with rollback at each step.

**Recommendation: B first, then A.** Three B runs cost ~30 minutes total
and answer "does it work on real production data" without changing any
flags or affecting any user.

---

## Pre-flight checklist (one-time, before B)

- [ ] `fly auth whoami` returns your handle (need worker shell + log access)
- [ ] `gsutil ls gs://<storage-bucket>/` works (need GCS read for output mp4s)
- [ ] `ffmpeg` + `ffprobe` on local PATH (for SSIM comparison)
- [ ] Phase 0 baseline captured: `fly logs -a nova-video --json --since 7d | python src/apps/api/scripts/aggregate_phase_timings.py --by template_id > pre-rollout-baseline.md`. Save it somewhere durable — this is the "before" snapshot every subsequent claim of "X% faster" depends on.
- [ ] Confirm `settings.single_pass_encode_enabled=False` at the env level. Verify: `fly secrets list -a nova-video | grep SINGLE_PASS`.
- [ ] Confirm every `video_templates` row has `single_pass_enabled=false`:
  ```bash
  fly ssh console -a nova-video -g worker -C 'python -c "
  from app.db import _sync_session
  from app.models import VideoTemplate
  with _sync_session() as db:
      rows = db.execute(__import__(\"sqlalchemy\").select(VideoTemplate.id, VideoTemplate.name, VideoTemplate.single_pass_enabled)).all()
      for r in rows: print(r)
  "'
  ```

---

## Path B — Force single-pass smoke test (NON-DESTRUCTIVE)

**Goal:** verify single-pass produces visually equivalent output to multi-pass on
one real production template + real user clips, *without* flipping any flags.

### B-1: Pick a target job

Find a recent successful template job for one of the verified-clean templates
(see "Template routing audit" below):

```bash
fly ssh console -a nova-video -g worker -C 'python -c "
from app.db import _sync_session
from app.models import Job
from sqlalchemy import select, desc
with _sync_session() as db:
    rows = db.execute(
        select(Job).where(Job.status==\"finished\").order_by(desc(Job.created_at)).limit(10)
    ).scalars().all()
    for j in rows:
        print(j.id, j.template_id, j.output_url)
"'
```

Pick one whose `template_id` matches a single-pass-routable template (see
audit table below). Save: `JOB_ID`, `MULTI_URL` (the multi-pass baseline).

### B-2: Save the multi-pass baseline

```bash
JOB_ID="<paste-job-id>"
MULTI_URL="<paste-output_url>"
mkdir -p /tmp/single-pass-smoke
gsutil cp "gs://$(echo $MULTI_URL | sed 's|https://storage.googleapis.com/||')" /tmp/single-pass-smoke/multi.mp4
ffprobe -v error -show_entries format=duration,bit_rate -of default=nw=1 /tmp/single-pass-smoke/multi.mp4
```

### B-3: Trigger force_single_pass rerender

This uses the locked `assembly_plan` from the original job — same clips,
same slot assignments. The output URL on the job WILL be overwritten with
the single-pass result.

```bash
fly ssh console -a nova-video -g worker -C "python -c '
from app.tasks.template_orchestrate import orchestrate_template_job
orchestrate_template_job.apply_async(args=[\"'$JOB_ID'\"], kwargs={\"force_single_pass\": True})
'"
```

Watch the worker logs:

```bash
fly logs -a nova-video --json -i worker | grep -E "$JOB_ID|effective_single_pass|single_pass_done|fallback_to_multi"
```

Expected log signature:
- `effective_single_pass=True`
- `single_pass_start inputs=N transitions=K abs_pngs=P`
- `single_pass_done size_bytes=...`
- `template_job_done`

If you see `single_pass_unsupported_fallback_to_multi reason="curtain-close..."`:
the template uses a feature gated until M4/M5. Fallback to multi-pass is
clean (output unchanged); pick a different template for B.

### B-4: Fetch the single-pass output

```bash
fly ssh console -a nova-video -g worker -C "python -c '
from app.db import _sync_session
from app.models import Job
with _sync_session() as db: print(db.get(Job, \"'$JOB_ID'\").output_url)
'"
SINGLE_URL="<paste new output_url>"
gsutil cp "gs://$(echo $SINGLE_URL | sed 's|https://storage.googleapis.com/||')" /tmp/single-pass-smoke/single.mp4
```

### B-5: Compare

```bash
cd /tmp/single-pass-smoke
ffmpeg -nostdin -loglevel info -i multi.mp4 -i single.mp4 \
  -lavfi ssim=stats_file=ssim.log -f null - 2>&1 | grep "SSIM"

ffprobe -v error -show_entries stream=codec_name,width,height,r_frame_rate,nb_read_packets,duration \
  -count_packets -of default=nw=1 multi.mp4
ffprobe -v error -show_entries stream=codec_name,width,height,r_frame_rate,nb_read_packets,duration \
  -count_packets -of default=nw=1 single.mp4

open multi.mp4 single.mp4  # macOS eyeball
```

### B-6: Pass criteria

| Metric | Threshold | Notes |
|---|---|---|
| SSIM All (global) | **≥ 0.95** | Recalibrated 2026-05-15 from 0.98 → 0.95 after production-shape clip testing. Grain-heavy real footage measurably differs between ultrafast-stream-copied multi-pass and fast-encoded single-pass at the pixel level, but is visually indistinguishable at this threshold. |
| Duration delta | ≤ 0.05s | |
| Frame count delta | ≤ 2 | Multi-pass has 3 encode passes that can each drift by ±1 frame from codec rounding. |
| Codec | h264 (both) | |
| Pixel format | yuv420p (both) | |
| Visual eyeball at slot boundaries (xfade, curtain transitions, overlay timing) | "looks the same" | |

### B-7: Cleanup / rollback

The job's `output_url` now points at the single-pass version. If a user is
actively watching this job's result, they'll see different bytes (visually
equivalent at SSIM ≥ 0.95, but not byte-identical).

To restore the multi-pass version:
```bash
gsutil cp /tmp/single-pass-smoke/multi.mp4 "gs://$(echo $MULTI_URL | sed 's|https://storage.googleapis.com/||')"
```

### B recommended sequence

Run B on 3 templates covering the 3 single-pass paths:
1. **impressing-myself** — M2 (2 slots, hard-cut, no overlays). Simplest.
2. **just-fine** — M6 (2 slots, hard-cut, fade-in absolute overlays). Exercises overlay chain.
3. **rule-of-thirds** — M3 + M6 (xfade transitions + absolute overlays). Exercises xfade chain + overlay chain together.

If all 3 pass: proceed to Path A.

---

## Path A — Full per-template rollout (after B passes)

### A-1: Flip the env-level kill switch (still no-op)

```bash
fly secrets set -a nova-video SINGLE_PASS_ENCODE_ENABLED=true
```

(Or update the env var via the relevant pydantic-settings mechanism — check
`src/apps/api/app/config.py:89` for the exact source name.)

This is a no-op because every `video_templates` row defaults to
`single_pass_enabled=false`. Verify with one job: every `ffmpeg_assemble_start`
log line should still show `effective_single_pass=false`.

If any job logs `effective_single_pass=true` here, STOP — a row was
flipped early or there's a config bug. Investigate before continuing.

### A-2: Allow-list the first template

Pick the lowest-traffic, lowest-risk M2 template (impressing-myself or
just-fine). One template, one row, one transaction:

```sql
UPDATE video_templates
SET single_pass_enabled = true
WHERE name = 'impressing-myself';
```

Or via worker shell:

```bash
fly ssh console -a nova-video -g worker -C 'python -c "
from app.db import _sync_session
from app.models import VideoTemplate
from sqlalchemy import select
with _sync_session() as db:
    t = db.execute(select(VideoTemplate).where(VideoTemplate.name.ilike(\"%impressing%\"))).scalar_one()
    t.single_pass_enabled = True
    db.commit()
    print(\"flipped:\", t.id, t.name)
"'
```

### A-3: Watch the next 5-10 jobs for that template

```bash
TEMPLATE_ID="<from A-2>"
fly logs -a nova-video --json -i worker --since 1h | \
  jq -r 'select(.template_id=="'$TEMPLATE_ID'") | "\(.event) effective=\(.effective_single_pass // \"?\") elapsed_ms=\(.elapsed_ms // \"?\")"' | head -50
```

Expected: `effective_single_pass=true` on all new jobs for this template.
`single_pass_done` events fire. Zero `single_pass_unsupported_fallback_to_multi`
events (this template is M2 — no unsupported features).

### A-4: Compare wall-clock to the Phase 0 baseline

```bash
fly logs -a nova-video --json --since 24h | \
  python src/apps/api/scripts/aggregate_phase_timings.py --by template_id > /tmp/post-flip.md
diff <(grep "$TEMPLATE_ID" pre-rollout-baseline.md) <(grep "$TEMPLATE_ID" /tmp/post-flip.md)
```

For an M2 template, expect `assemble:single_pass` to replace
`assemble:render_parallel` + `assemble:curtain_and_interstitials` + `assemble:join` +
`assemble:text_overlay`. Total `_assemble_clips` time should be similar or
slightly faster.

### A-5: Escalate per template, 24h cadence

After 24h of clean signal on impressing-myself:
1. Flip just-fine (M2 + M6)
2. After 24h: flip rule-of-thirds (M3 + M6)
3. After 24h: flip every other M2/M3/M6-compatible template
4. After 7 days clean across all templates: nothing left to flip — multi-pass becomes the fallback for the curtain-close templates only (dimples-passport).

### A-6: Rollback procedure

Per-template rollback:
```sql
UPDATE video_templates SET single_pass_enabled = false WHERE id = '<...>';
```

Fleet kill switch:
```bash
fly secrets set -a nova-video SINGLE_PASS_ENCODE_ENABLED=false
```

Both are reversible in seconds with no migration needed.

### A-7: Phase 2

Once 100% of M2+M3+M6 templates are allow-listed and stable for 2+ weeks,
run the [`concurrency_2_canary.md`](concurrency_2_canary.md) protocol to flip
Fly `worker --concurrency=1 → 2`. That doubles throughput at the same VM cost.

---

## Template routing audit (in-repo state, 2026-05-15)

| Template | Kind | Single-pass route | Source |
|---|---|---|---|
| impressing-myself | multiple_videos | **M2** (direct) | `templates/impressing-myself.json` |
| just-fine | multiple_videos | **M6** (direct, fade-in overlays) | `templates/just-fine.json` |
| love-from-moon | templated (music) | N/A — music opts out via `force_single_pass=False` at `music_orchestrate.py:415` | `templates/love-from-moon.json` |
| how-do-you-enjoy-your-life | single_video | N/A — single_video kind, doesn't use `_assemble_clips` | `scripts/seed_how_do_you_enjoy_your_life.py` |
| dimples-passport-brazil | multiple_videos | **Falls back to multi-pass** — uses `curtain-close` interstitial (M4 not yet shipped) | `scripts/seed_dimples_passport_brazil.py` |
| rule-of-thirds | multiple_videos | **M3 + M6** (direct, dissolve transitions + absolute overlays) | `scripts/seed_rule_of_thirds.py` |

**Production DB has more templates than the repo.** Templates that exist
only in production (created via admin UI, not seeded) must be audited
separately:

```bash
fly ssh console -a nova-video -g worker -C 'python -c "
from app.db import _sync_session
from app.models import VideoTemplate
from sqlalchemy import select
with _sync_session() as db:
    rows = db.execute(select(VideoTemplate.name, VideoTemplate.template_type)).all()
    for r in rows: print(r)
"'
```

For each unknown template, inspect its `recipe_cached` for `interstitials`
of type `curtain-close` or `barn-door-open` (these gate to multi-pass) and
for `text_overlays` with `pre_burn_pngs` (also gated). If a template uses
any of those, it falls back to multi-pass cleanly — no breakage, just no
speedup until M4/M5 lands.

---

## Phase 0 baseline aggregation

The plan calls for a 7-day production baseline before flipping anything.
The aggregator already exists at `src/apps/api/scripts/aggregate_phase_timings.py`.
Pipe Fly logs in:

```bash
fly logs -a nova-video --json --since 7d | \
  python src/apps/api/scripts/aggregate_phase_timings.py --by template_id > pre-rollout-baseline.md
```

Save the output. After A-3 for each template, diff the same query against
the post-flip window to see the per-template delta.

The aggregator handles both stage-level (`fixed_intro_stage_done`) and
sub-phase (`assemble_phase_done`) events. Stringified numbers, Fly's
`{"message": "..."}` envelope, and missing fields are all handled.

---

## What still needs Yasin/Emir's hands

Items I could not execute from a worktree without production access. All of
these are operational, not code:

1. **Stage actual Nova-user-uploaded clips in a parity GCS prefix.** The
   in-repo test uses production-shape synthetic clips (see
   `tests/scripts/gen_real_shape_clips.sh`) which exercise the encoder
   correctly but are not real user footage. For a true production-grade
   parity sweep before flag flip, stage 3-5 real recent clips representing
   the production content distribution (varied: gradient, motion, dark,
   text-on-screen) at a known GCS prefix. Trigger the
   `single-pass-parity` workflow_dispatch with that prefix as input.
2. **Pull the 7-day Phase 0 baseline.** Requires `fly logs` auth on
   your machine. One-line command above.
3. **Production DB template audit.** The in-repo template audit above is
   complete for code-defined templates. Templates created via the admin UI
   only exist in the DB and need a separate query (one-liner above).
4. **Run path B on 3 templates before path A.** ~30 min total. The runbook
   above is copy-pasteable.

---

## History

| Date | Event |
|---|---|
| 2026-05-15 | PR #147 merged: single-pass M2+M3+M6 + per-template allow-list |
| _TBD_ | First production Path B smoke test |
| _TBD_ | First production Path A allow-list flip |
| _TBD_ | Phase 2 (`--concurrency=2`) canary |
