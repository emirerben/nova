# Agent evals — Big 3

Per-agent quality eval harness for `nova.compose.template_recipe`, `nova.video.clip_metadata`, and `nova.compose.creative_direction`.

## Run modes

```bash
cd src/apps/api

# 1) Default — structural-only, replay mode, no network. Runs in CI.
pytest tests/evals/ -v

# 2) Structural + Claude-Sonnet judge, still replay mode. Needs ANTHROPIC_API_KEY.
ANTHROPIC_API_KEY=… pytest tests/evals/ -v --with-judge

# 3) Live mode — actually call Gemini for each fixture. Slow and costs money.
NOVA_EVAL_MODE=live GEMINI_API_KEY=… ANTHROPIC_API_KEY=… \
  pytest tests/evals/ -v --eval-mode=live --with-judge

# 4) Single agent
pytest tests/evals/test_clip_metadata_evals.py -v --with-judge
```

## What gets checked

- **Structural assertions** (free, deterministic): pydantic re-parse, field invariants (slot durations sum, energy ranges, valid enums, overlay bounds, football-filter compliance, creative-direction topic coverage). See `runners/structural.py`.
- **LLM-as-judge** (Claude Sonnet 4.6, opt-in): scores output against the rubric at `rubrics/{agent}.md`. Avg ≥ 3.5 to pass by default. Rubrics are prompt-cached so repeated calls hit Anthropic's cache.

## Adding fixtures

```bash
# 1) Export production templates (template_recipe + creative_direction):
.venv/bin/python scripts/export_eval_fixtures.py
#    Writes tests/fixtures/agent_evals/{template_recipe,creative_direction}/prod_snapshots/*.json

# 2) Verify they pass structural checks:
pytest tests/evals/ -v

# 3) For clip_metadata: best_moments aren't persisted in DB. Either:
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

## Iterating on a prompt

The whole point of this harness is closing the loop on prompt edits.

```bash
# 1) Edit the prompt file (e.g., prompts/analyze_clip.txt).
# 2) Bump prompt_version in the agent's AgentSpec, e.g.:
#       prompt_version="2026-05-10"
# 3) Run a baseline judge run on the OLD prompt (replay mode is fine):
git stash
pytest tests/evals/test_clip_metadata_evals.py -v --with-judge | tee /tmp/old.log
git stash pop
# 4) Run with the candidate prompt against live Gemini:
NOVA_EVAL_MODE=live pytest tests/evals/test_clip_metadata_evals.py -v --with-judge | tee /tmp/new.log
# 5) Compare avg scores per fixture. Ship only if no fixture regressed.
```

A future iteration of this harness will add `--shadow` to do this comparison automatically inside one pytest run via `app.agents._runtime.run_with_shadow`.

## CI

- **Default CI:** runs structural-only on every PR (~30s, no secrets).
- **Manual:** `.github/workflows/agent-evals.yml` is `workflow_dispatch` only. Triggers full live + judge run.

## Related

- `tests/quality/eval_scoring.py` — the original recall@3 launch gate for hook scoring (heuristic + Gemini end-to-end).
- `app/agents/_runtime.py` — runtime that this harness relies on (`ModelClient`, `Agent`, `run_with_shadow`).
- `app/agents/{template_recipe,clip_metadata,creative_direction}.py` — the three agents under test.
