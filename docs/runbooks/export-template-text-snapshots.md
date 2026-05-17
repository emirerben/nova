# Runbook: Export template_text Eval Fixtures from Prod DB

**Context:** PR #188 (v0.4.26.0, merged 2026-05-17) shipped `TemplateTextAgent` live in
`agentic_template_build_task`. Its output is merged into
`VideoTemplate.recipe_cached.slots[*].text_overlays` with two markers:
`_extracted_by: "nova.compose.template_text"` and a `text_bbox` dict per overlay.
PR #193 added `_build_template_text_fixture()` to `scripts/export_eval_fixtures.py` and
the test file `tests/evals/test_template_text_evals.py`.

The eval gate is currently **dormant**: it is skipped at test time with the message
`"no fixtures under tests/fixtures/agent_evals/template_text/"`. This runbook explains
how to unlock it.

---

## Why the local dev DB can't help

The local dev DB (docker-compose `nova-db-1`, `postgresql://postgres:postgres@localhost:5432/nova`)
has 15 templates, all with `recipe_cached` populated, and all were last analyzed on or
before 2026-05-07 — before PR #188 shipped on 2026-05-17. None of their
`text_overlays` carry the `_extracted_by` marker that `_build_template_text_fixture()`
requires. Running the exporter locally therefore produces 0 fixtures.

The only eligible templates are in the **Fly.io prod database** (`nova-db` app), where
`agentic_template_build_task` has been running the new agent since deploy at ~13:55 UTC
on 2026-05-17.

---

## What the export script does (no AI keys required)

`scripts/export_eval_fixtures.py --only template_text` does the following:

1. Connects to `DATABASE_URL` (sync-compatible; uses `AsyncSessionLocal` internally).
2. Loads all `VideoTemplate` rows with `analysis_status = 'ready'`.
3. For each template, walks `recipe_cached.slots[*].text_overlays` and collects overlays
   where `_extracted_by == "nova.compose.template_text"` AND `text_bbox` is a dict.
4. Re-emits those overlays as a flat list, converting slot-relative timings back to
   global timings (reverses `_merge_overlays_into_slots`).
5. Runs structural validation via `run_eval(CassetteModelClient(raw_text), ...)`.
6. Writes passing fixtures to
   `src/apps/api/tests/fixtures/agent_evals/template_text/prod_snapshots/<slug>.json`.

The script requires only `DATABASE_URL` — no `GEMINI_API_KEY` or `ANTHROPIC_API_KEY`.

---

## Step-by-step: run against prod DB

### 1. Proxy the prod DB to localhost

The Fly.io postgres app is named **`nova-db`** (not `nova-video-postgres`). Use
`fly proxy` to forward a local port to it:

```bash
# Terminal 1 — keep this running while you export
fly proxy 15432:5432 -a nova-db
```

The proxy binds `localhost:15432` → prod postgres on port 5432. Leave it open.

### 2. Find the prod DATABASE_URL

```bash
fly secrets list -a nova-video | grep DATABASE_URL
# Then reveal it:
fly ssh console -a nova-video --command "printenv DATABASE_URL"
```

It will look like:
`postgresql://postgres:<password>@nova-db.flycast:5432/nova?sslmode=disable`

Swap the host to `localhost:15432` for the proxy:
`postgresql://postgres:<password>@localhost:15432/nova?sslmode=disable`

### 3. Create a throwaway worktree (never commit prod creds)

```bash
git worktree add -b docs/template-text-fixtures ../nova-tt-fixtures origin/main
cd ../nova-tt-fixtures
```

### 4. Run the export (dry-run first)

```bash
cd src/apps/api
DATABASE_URL="postgresql://postgres:<password>@localhost:15432/nova?sslmode=disable" \
  /Users/emirerben/Projects/nova/src/apps/api/.venv-test/bin/python \
  scripts/export_eval_fixtures.py --only template_text --dry-run
```

Expected output (with at least one eligible template):
```
Reading from DATABASE_URL host: localhost:15432/nova
[dry-run] would write .../template_text/prod_snapshots/morocco.json
[dry-run] would write .../template_text/prod_snapshots/football_face_hook.json
...
Exported: 3 template_text, ...
```

If the count is 0, no templates have been re-analyzed by the new agent yet. Trigger
re-analysis from the admin panel at `/admin/templates/[id]` for a few templates, wait
for the `agentic_template_build_task` Celery task to complete, then re-run.

### 5. Run for real

```bash
DATABASE_URL="postgresql://postgres:<password>@localhost:15432/nova?sslmode=disable" \
  /Users/emirerben/Projects/nova/src/apps/api/.venv-test/bin/python \
  scripts/export_eval_fixtures.py --only template_text
```

### 6. Inspect for PII before committing

Each fixture at
`src/apps/api/tests/fixtures/agent_evals/template_text/prod_snapshots/<slug>.json`
contains the template name, overlay text, and bboxes — no user data. Still check:

```bash
# Look at sample_text fields — these are the visible text strings extracted from
# the template video. Template names and marketing copy are fine. Real user names,
# email addresses, or phone numbers found in overlay text → skip that fixture.
python -c "
import json, glob, pathlib
for f in sorted(pathlib.Path('src/apps/api/tests/fixtures/agent_evals/template_text/prod_snapshots').glob('*.json')):
    d = json.loads(f.read_text())
    texts = [ov.get('sample_text','') for ov in (d.get('output',{}).get('overlays') or [])]
    print(f.name, '->', texts[:5])
"
```

### 7. Choose 3 fixtures to keep (variety criterion)

Keep fixtures that cover at least these three text-density / effect profiles:
- **watermark-heavy**: a template with 3+ short watermark-style overlays (logo, handle)
  in addition to hook text.
- **hook-and-label**: a template with a distinct hook overlay + subject/speaker labels.
- **word-by-word**: a template where overlay text is short fragments (1-3 words each)
  that together form a sentence — typical of typewriter/font-cycle effect templates.

Delete the rest before staging.

### 8. Commit and push

```bash
git add src/apps/api/tests/fixtures/agent_evals/template_text/prod_snapshots/
git commit -m "$(cat <<'EOF'
feat(evals): add 3 prod-snapshot fixtures for template_text eval gate

Exported from prod DB (nova-db) using scripts/export_eval_fixtures.py.
Unlocks test_template_text_evals.py which was previously skip-guarded.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
git push -u origin docs/template-text-fixtures
gh pr create --title "feat(evals): template_text prod-snapshot fixtures" \
  --body "Adds 3 exported fixtures so test_template_text_evals.py runs."
```

---

## Step after fixtures land: build ground truth

The eval can run in **qualitative mode** (judge score 1-5 per rubric) without ground
truth. To enable the objective scoring path (completeness, temporal IoU, spatial IoU):

```bash
# For each fixture slug (e.g. "morocco"):
cd src/apps/api
python scripts/build_text_ground_truth.py \
  --video /path/to/template_reference.mp4 \
  --slot-boundaries 0.0:3.0,3.0:7.5,7.5:12.0 \
  --out tests/fixtures/agent_evals/template_text/ground_truth/morocco.json
```

`--slot-boundaries` is a comma-separated list of `start:end` seconds for each slot.
The values are in `fixture.output.slot_boundaries` (or derive from
`recipe_cached.slots[*].target_duration_s`).

The script runs pytesseract OCR, groups frame detections into intervals, and opens
each overlay's representative frame for operator review (confirm effect label + role).
Commit the output — it is a hand-validated artifact.

---

## Run the eval

```bash
cd src/apps/api

# Structural only (replay mode, no keys needed):
pytest tests/evals/test_template_text_evals.py -v

# With LLM judge (needs ANTHROPIC_API_KEY):
pytest tests/evals/test_template_text_evals.py -v --with-judge

# Live Gemini re-run + judge (needs both GEMINI_API_KEY + ANTHROPIC_API_KEY, ~$2-5):
NOVA_EVAL_MODE=live pytest tests/evals/test_template_text_evals.py -v \
  --eval-mode=live --with-judge
```

Pass threshold: avg judge score ≥ 3.5 across the 5 rubric dimensions
(completeness, timing_accuracy, position_accuracy, font_color_accuracy, effect_label_accuracy).

---

## If no templates are eligible after re-analysis

If prod templates were re-analyzed before the deploy at ~13:55 UTC 2026-05-17,
or if the task short-circuited early, the `_extracted_by` marker won't be present.
Force a re-analysis:

```bash
# From admin UI: /admin/templates/[id] → "Reanalyze"
# OR via API:
curl -X POST https://nova-video.fly.dev/admin/templates/<id>/reanalyze \
  -H "X-Admin-Key: $ADMIN_API_KEY"
```

Then wait for the Celery `agentic_template_build_task` to complete (check Fly logs:
`fly logs -a nova-video | grep template_text`). Re-run the export script.
