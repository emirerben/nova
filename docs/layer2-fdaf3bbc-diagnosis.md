# Layer-2 diagnosis handoff — template `fdaf3bbc-2f4f-43bc-ba7c-e5cd819de102`

## What this branch contains

Worktree: `../nova-layer2-text-fixes` (branch `feat/layer2-text-fixes-2026-05-18` off `origin/main` @ `b2e09b5`).

**Code (lands today, no prod creds needed):**

- `app/pipeline/text_overlay_v2/pipeline.py` — added `dump_stages_dir` param to `run_full_pipeline()`. When set, writes `stage_a.json` … `stage_g.json` (+ `stage_e_dropped.txt`) to the directory. Purely additive; default `None` = no-op.
- `tests/pipeline/text_overlay_v2/test_pipeline.py` — 4 new unit tests covering the dump helper.
- `scripts/debug_layer2.py` — operator CLI that runs the pipeline on a local video with `dump_stages_dir`, prints a one-line-per-stage summary, and optionally diffs against ground truth.
- `tests/fixtures/agent_evals/template_text/ground_truth/fdaf3bbc.json` — placeholder ground-truth file (empty `overlays: []`, with a `_comment` explaining how to fill it).
- `tests/fixtures/agent_evals/template_text/ground_truth/fdaf3bbc.thresholds.json` — initial loose floors mirroring `rich_in_life_v2.thresholds.json`.

**Verification run:** `pytest tests/pipeline/text_overlay_v2/ tests/evals/test_template_text_evals.py` → **57 + (3 passed + 4 skipped)** all green. The 4 skips are the placeholder ground-truth files; they are intentional and will activate once `prod_snapshots/fdaf3bbc.json` lands (see Step 3 below).

---

## Operator steps remaining (need prod creds / hand-labeling)

### Step 1 — Pull the template's current Layer-2 output from prod

The admin GET-single-template endpoint is at `src/apps/api/app/routes/admin*.py`. If
no GET-single exists, the simplest path is to query the prod DB directly or to add
a small read endpoint.

```bash
TEMPLATE_ID=fdaf3bbc-2f4f-43bc-ba7c-e5cd819de102
curl -s -H "Authorization: Bearer $NOVA_ADMIN_TOKEN" \
  "https://nova-video.fly.dev/admin/templates/$TEMPLATE_ID" \
  | jq '.recipe_cached.slots[] | {slot_index, text_overlays}' \
  > /tmp/fdaf3bbc-prod-overlays.json
```

Also grab the `gcs_path` (you'll need it for Step 2):

```bash
curl -s -H "Authorization: Bearer $NOVA_ADMIN_TOKEN" \
  "https://nova-video.fly.dev/admin/templates/$TEMPLATE_ID" \
  | jq '{gcs_path, recipe_cached_at, use_layer2_default}'
```

Confirm `use_layer2_default: true` — if false, the prod overlays you pulled came
from Layer-1, not Layer-2, and the comparison is meaningless. PR #227 added the
flag; set it via the admin UI toggle if needed.

### Step 2 — Run the pipeline locally with stage dumps

```bash
gsutil cp gs://$NOVA_BUCKET/<gcs_path> /tmp/fdaf3bbc.mp4

cd /Users/emirerben/Projects/nova-layer2-text-fixes/src/apps/api
.venv/bin/python scripts/debug_layer2.py \
  --video /tmp/fdaf3bbc.mp4 \
  --out /tmp/fdaf3bbc-stages \
  --slot-boundaries-s "<copy from recipe.slots, e.g. 0:5.5,5.5:22.37>" \
  --template-id fdaf3bbc-2f4f-43bc-ba7c-e5cd819de102
```

You'll see something like:

```
  A frames        = 21
  B detections    = 58
  C events        = 14
  D phrases       = 12     ← compare to what's actually on screen
  E aligned       = 12  (dropped_count=0)
  F classified    = 12
  G overlays_out  = 10     ← if 12→10, two failed schema validation
```

**Attribute each user-visible failure to a stage by reading the JSONs:**

| User-visible symptom | Read these stages in order |
| --- | --- |
| Overlay missing entirely | `stage_b.json` (was it OCR'd at all?) → `stage_d.json` (did clustering drop it?) → `stage_e.json` (did alignment drop it?) → `stage_g.json` (did schema reject it?) |
| Wrong text content | `stage_d.json` (what did OCR see?) vs `stage_e.json` (what did alignment "correct" it to?) |
| Wrong color / size / effect / role | `stage_e.json` vs `stage_f.json` (classification is the only place these are set) |
| Wrong timing / position | `stage_d.json` (phrase t/aabb) vs `stage_g.json` (after slot assignment + clamp) |

### Step 3 — Hand-build ground truth

Run the tesseract first-pass:

```bash
.venv/bin/python scripts/build_text_ground_truth.py \
  --video /tmp/fdaf3bbc.mp4 \
  --slot-boundaries 0:5.5,5.5:22.37 \
  --out tests/fixtures/agent_evals/template_text/ground_truth/fdaf3bbc.json
```

Then open the file and hand-correct frame by frame: every overlay needs the
right `sample_text`, `start_s`/`end_s`, `bbox` (x/y/w/h normalised), `font_color_hex`
(eyedropper a sample frame), `effect` (watch the animation), `role`, `size_class`.
Mirror the shape of `rich_in_life_v2.json`.

### Step 4 — Capture as a regression fixture

Layer-2 calls 2 Gemini agents (E + F) + an OCR backend (B). The existing
`CassetteModelClient` mocks a single Gemini response, so **Layer-2 fixtures
can only run in `--eval-mode=live`** with `GEMINI_API_KEY` + GCS creds + ffmpeg.
There is no replay-mode shortcut today.

Use the existing exporter (or write a fresh capture script) to drop a snapshot
at `tests/fixtures/agent_evals/template_text/prod_snapshots/fdaf3bbc.json`:

```json
{
  "agent": "nova.compose.template_text",
  "prompt_version": "exported",
  "input": {
    "file_uri": "templates/fdaf3bbc-.../reference.mp4",
    "gcs_path": "templates/fdaf3bbc-.../reference.mp4",
    "file_mime": "video/mp4",
    "transcript_words": [...],
    "slot_boundaries_s": [[0.0, 5.5], [5.5, 22.37]],
    "force_layer2": true
  },
  "raw_text": "{}"
}
```

`raw_text` is unused in live mode but the schema requires it. Once dropped:

```bash
pytest tests/evals/test_template_text_evals.py -v -k fdaf3bbc --eval-mode=live --with-judge
```

Expected: **fails** initially (that's the point — the eval is the regression gate
for the fixes you're about to make). The pre-existing
`ground_truth/fdaf3bbc.json` will activate automatically.

### Step 5 — Fix the stages Step 2 attributed

Per-stage playbook is in `~/.claude/plans/i-just-tested-the-sparkling-deer.md`
(the approved plan), Step 6 table. Each fix lands with either a new unit test in
`tests/pipeline/text_overlay_v2/` or a tightened threshold in
`fdaf3bbc.thresholds.json` — never just a prompt tweak with no test.

Re-run `pytest tests/evals/test_template_text_evals.py -v` after each fix.

### Step 6 — Verify (regression + neighbour fixtures)

```bash
cd src/apps/api

# Must be green
pytest tests/pipeline/text_overlay_v2/ tests/agents/test_text_*.py tests/agents/test_template_text_*.py

# Now expected to pass
pytest tests/evals/test_template_text_evals.py -v -k fdaf3bbc

# Must NOT regress
pytest tests/evals/test_template_text_evals.py -v -k "rich_in_life or not_just_luck"

# Full live judge run on all 3 fixtures (~$2-5)
NOVA_EVAL_MODE=live ./scripts/run_template_text_eval.sh --with-judge --eval-mode=live

# Manual re-test against prod admin
curl -X POST -H "Authorization: Bearer $NOVA_ADMIN_TOKEN" \
  "https://nova-video.fly.dev/admin/templates/$TEMPLATE_ID/reanalyze-agentic?use_layer2=true"
# Wait for analysis_status=ready, then open admin UI and compare overlays
# to ground truth.
```

---

## Interesting context found during this pass

- The canary that triggered PR #222 (`download from GCS before invoking Layer-2 pipeline`)
  was specifically this template — `app/agents/template_text.py:164` has the inline
  comment: *"canary evidence: 2026-05-17 fdaf3bbc, 'No such object: nova-videos-dev/
  https://generativelanguage.googleapis.com/...'"*. So `fdaf3bbc` has been the
  problem child of Layer-2 since its first canary. Whatever's still wrong is likely
  template-shape-specific (fast-cut? low-contrast? animated text? music-only?) —
  Step 2's stage dumps will tell.
- `not_just_luck.json` (the existing prod snapshot) is template `7d98d667-...`, NOT
  `fdaf3bbc`. Don't conflate them.
- Layer-2 evals only run live. The `CassetteModelClient` was built for Layer-1's
  single-Gemini-call shape and would need a multi-response queue to handle Layer-2.
  Logged as P3 in TODOS.md if you want it long-term; not blocking.
