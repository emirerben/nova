# Agent evals

Per-agent quality eval harness. Phase 1 covers the original Big 3 (`template_recipe`, `clip_metadata`, `creative_direction`); Phase 2 extends to the three additional in-pipeline agents (`transcript`, `platform_copy`, `audio_template`); the auto-music work then adds `song_classifier` (Phase 1 of auto-music) and `music_matcher` (Phase 2 of auto-music), taking the eval-covered Big set to 5.

The other Phase 2 agent classes that exist in `app/agents/` (`text_designer`, `transition_picker`, `clip_router`, `shot_ranker`) are not yet wired into the pipeline — their evals will land alongside the PR that wires each into the runtime.

## Run modes

```bash
cd src/apps/api

# 1) Default — structural-only, replay mode, no network. Runs in CI.
pytest tests/evals/ -v

# 2) Structural + Claude-Sonnet judge, still replay mode. Needs ANTHROPIC_API_KEY.
ANTHROPIC_API_KEY=… pytest tests/evals/ -v --with-judge

# 3) Live mode — actually call Gemini for each fixture. Slow and costs money.
#    The harness pre-flights estimated cost; pass --allow-cost after reviewing.
NOVA_EVAL_MODE=live GEMINI_API_KEY=… ANTHROPIC_API_KEY=… \
  pytest tests/evals/ -v --eval-mode=live --with-judge --allow-cost

# 4) Single agent
pytest tests/evals/test_clip_metadata_evals.py -v --with-judge
pytest tests/evals/test_transcript_evals.py -v --with-judge
pytest tests/evals/test_platform_copy_evals.py -v --with-judge
pytest tests/evals/test_audio_template_evals.py -v --with-judge
```

## What gets checked

- **Structural assertions** (free, deterministic): pydantic re-parse, field invariants (slot durations sum, energy ranges, valid enums, overlay bounds, football-filter compliance, creative-direction topic coverage, transcript word ordering, platform-copy placeholder leakage, audio-template beat monotonicity). See `runners/structural.py`.
- **LLM-as-judge** (Claude Sonnet 4.6, opt-in): scores output against the rubric at `rubrics/{agent}.md`. Avg ≥ 3.5 to pass by default. Rubrics are prompt-cached so repeated calls hit Anthropic's cache.

## Agents covered

| Agent | Structural | Rubric | Prod-snapshot fixtures | DB source |
|---|---|---|---|---|
| `nova.compose.template_recipe` | ✓ | `rubrics/template_recipe.md` | exported | `VideoTemplate.recipe_cached` |
| `nova.video.clip_metadata` | ✓ | `rubrics/clip_metadata.md` | hand-authored only (`best_moments` not persisted) | — |
| `nova.compose.creative_direction` | ✓ | `rubrics/creative_direction.md` | exported | `VideoTemplate.recipe_cached.creative_direction` |
| `nova.audio.transcript` | ✓ | `rubrics/transcript.md` | exported | `Job.transcript` |
| `nova.compose.platform_copy` | ✓ | `rubrics/platform_copy.md` | exported | `JobClip.platform_copy` |
| `nova.audio.template_recipe` (audio_template) | ✓ | `rubrics/audio_template.md` | exported | `MusicTrack.recipe_cached` |
| `nova.audio.song_classifier` | ✓ | `rubrics/song_classifier.md` | exported + hand-authored golden | `MusicTrack.ai_labels` |
| `nova.audio.music_matcher` | ✓ | `rubrics/music_matcher.md` | hand-authored golden only (not persisted) | — |
| `nova.audio.retake_detector` | ✓ | `rubrics/retake_detector.md` | hand-authored golden only (spans not persisted); incl. TR/EN restarts + negative fixtures gated by `meta.expect_empty` | — |

## Layer-2 text-overlay pipeline eval

`test_text_overlay_pipeline_evals.py` evaluates `run_full_pipeline()` from
`app.pipeline.text_overlay_v2` (the OCR + grouping + alignment + classification
pipeline shipped in PRs #204–#214). See the design doc at
`~/.claude/plans/template-text-overlay-layer-2-architecture.md` for architecture
details.

Hard floors (design doc §Verification plan): **precision ≥ 0.95, recall ≥ 0.95,
mean bbox IoU ≥ 0.85**. All three gates are overridable per-fixture via a
`ground_truth/<stem>.thresholds.json` sidecar (same `.thresholds.json` pattern as
Layer-1).

The eval gate is currently **dormant** — all tests skip until two sidecars are added
per fixture:

1. `tests/fixtures/agent_evals/template_text/ground_truth/<stem>.json` — human-verified
   ground-truth overlays (same format as Layer-1).
2. `tests/fixtures/agent_evals/template_text/prod_snapshots/<stem>.transcript.json` — the
   Whisper transcript for the template's audio.  Shape:
   ```json
   {
       "transcript_words": [
           {"text": "It's", "start_s": 0.08, "end_s": 0.42, "confidence": 0.99},
           ...
       ]
   }
   ```
   Produce by running `nova.audio.transcript` on the template and serialising
   `output.words`.

Live mode (`--eval-mode=live --allow-cost`) downloads the source video from GCS,
invokes the full pipeline, and scores the result. Estimated cost: **~$0.03/template**
(Cloud Vision OCR @ 2 fps). Requires `STORAGE_BUCKET`, GCS credentials, and
`GEMINI_API_KEY`.

Next step (human-paced): capture transcripts for the three existing prod snapshots
(`not_just_luck`, `rich_in_life`, `rich_in_life_v2`) and add at least 4 more
hand-verified ground truths covering varied caption styles (typewriter, font-cycle,
static label, subtitle).

## Adding fixtures

```bash
# 1) Export production rows for all in-pipeline agents:
.venv/bin/python scripts/export_eval_fixtures.py
#    Writes tests/fixtures/agent_evals/<agent>/prod_snapshots/*.json

# 2) Export only one agent:
.venv/bin/python scripts/export_eval_fixtures.py --only platform_copy

# 3) Verify they pass structural checks:
pytest tests/evals/ -v

# 4) For clip_metadata: best_moments aren't persisted in DB. Either:
#    - hand-craft a fixture under .../clip_metadata/golden/<case>.json, OR
#    - capture a live run by setting NOVA_EVAL_MODE=live and printing raw_text
#      from a debug session.
```

## Fixture format

```json
{
  "agent": "nova.compose.template_recipe",
  "prompt_version": "2026-05-09",
  "input": { /* matches Agent.Input pydantic */ },
  "raw_text": "/* recorded model response */",
  "output": { /* matches Agent.Output pydantic; sanity + judge sees this */ },
  "meta": { "template_id": "...", "exported_at": "..." }
}
```

`raw_text` provenance: the export script serializes the persisted output dict back to JSON and uses that as `raw_text`. Replay re-parses it through the agent's `parse()`. This means replay validates the structural floor but does not validate Gemini's actual JSON-shape compliance — that's why we keep at least one hand-authored `golden/` fixture per agent recorded from a real model response.

## Iterating on a prompt

Two workflows. The manual one (works on any prompt) and the automatic one (live-only).

### Manual stash + run + diff

```bash
# 1) Edit the prompt file (e.g., prompts/analyze_clip.txt).
# 2) Bump prompt_version in the agent's AgentSpec.
# 3) Baseline the OLD prompt (replay is fine):
git stash
pytest tests/evals/test_clip_metadata_evals.py -v --with-judge | tee /tmp/old.log
git stash pop
# 4) Test the candidate against live Gemini:
NOVA_EVAL_MODE=live pytest tests/evals/test_clip_metadata_evals.py \
  -v --with-judge --eval-mode=live --allow-cost | tee /tmp/new.log
# 5) Compare avg scores per fixture. Ship only if no fixture regressed.
```

### Auto `--shadow-prompts-dir` mode (live-only)

Run prod and a candidate prompt side-by-side, judge both, report a per-fixture delta — no stash dance.

```bash
# 1) Drop a candidate prompt at src/apps/api/prompts.candidate/<prompt_id>.txt
mkdir -p prompts.candidate
cp prompts/analyze_clip.txt prompts.candidate/analyze_clip.txt
$EDITOR prompts.candidate/analyze_clip.txt

# 2) Run the eval suite with shadow:
NOVA_EVAL_MODE=live GEMINI_API_KEY=… ANTHROPIC_API_KEY=… \
  pytest tests/evals/test_clip_metadata_evals.py -v --eval-mode=live --with-judge \
  --shadow-prompts-dir=prompts.candidate --allow-cost
```

How it works:
- For each fixture, the harness runs the agent twice in live mode: once with prod prompts/, once with prompts.candidate/<prompt_id>.txt overlaid on prod prompts/ (any prompt file not in the candidate dir falls through to prod).
- Both runs are judged. Per-fixture summary prints `primary_avg=… shadow_avg=… Δ=…`.
- Shadow result is **informational only** — the test still gates on the primary (prod) run. Shadow failures (raise, structural-fail, judge-fail) are reported but never break the test.

Constraints:
- Live-mode only. Replay's `raw_text` was recorded under the prod prompt; comparing it against a candidate prompt is meaningless. The harness errors clearly if `--shadow-prompts-dir` is set without `--eval-mode=live`.
- Inline-prompt agents (`platform_copy`, `text_designer`, `transition_picker`, `clip_router`, `shot_ranker`) build their prompts from input rather than loading a file — shadow has no effect for those.

## Live-mode cost cap

Live runs hit the Gemini API. To prevent an accidental 100-fixture run from quietly burning real $, the harness pre-flights estimated cost at pytest collection time and refuses to run if the total exceeds **$20**.

```bash
# Default — refuses if estimate > $20:
NOVA_EVAL_MODE=live pytest tests/evals/ --eval-mode=live

# Bypass after reviewing the estimate:
NOVA_EVAL_MODE=live pytest tests/evals/ --eval-mode=live --allow-cost
```

The estimate is intentionally pessimistic: input tokens ≈ chars/3 of (input payload + 4KB prompt overhead), output tokens fixed at 1500 per call. Replay mode skips the check entirely.

## CI

- **Default CI:** runs structural-only on every PR (~30s, no secrets).
- **Manual:** `.github/workflows/agent-evals.yml` is `workflow_dispatch` only. Triggers full live + judge run with `--allow-cost`.

## Related

- `tests/quality/eval_scoring.py` — the original recall@3 launch gate for hook scoring.
- `app/agents/_runtime.py` — runtime that this harness relies on (`ModelClient`, `Agent`, `run_with_shadow`).
- `app/agents/{template_recipe,clip_metadata,creative_direction,transcript,platform_copy,audio_template,song_classifier,music_matcher}.py` — the eight agents under test.
- `scripts/export_eval_fixtures.py` — DB → fixture exporter.
- `scripts/reanalyze_underbaked_templates.py` — one-off to re-run two-pass analysis on templates with under-baked `creative_direction`.
