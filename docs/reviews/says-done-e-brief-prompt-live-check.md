# Says-done group E: paid live check of the brief-writer teaching (NOT RUN)

Status: written, not run. It spends real money on Gemini, so it needs Yasin's OK first.
Free checks already done: the replay evals (`pytest tests/evals/`) pass, but replay answers are
hand-written and the cassette ignores the prompt, so only this live run shows what the model
actually records for her sentences.

## What it runs

The real Main Creator (Gemini 3.1 Pro, prompt v137) on Qendresa's exact Q1 and Q2 prompts, three
times each (the model is not deterministic), plus one plain-ask decoy and one brief-extractor
follow-up. The test file applies the same checks it applies in replay to the model's real answer
(`meta.caption_cleanup_brief`, `meta.restart_brief`, `meta.prompt_contains`), so a pass or fail is
printed per case. Each real answer is saved under `NOVA_EVAL_CAPTURE_DIR`.

| Case | Fixture | Expected facts (checked automatically) |
|---|---|---|
| Q1 ×3 | `main_creator/qendresa_q1_caption_two_words_tight` | one captions requirement: `captions: "word_by_word"`, `caption_words: 2`, `caption_position: "middle"`, `caption_color: "#FFFFFF"`, no box; a cleanup ask with `pauses: "tight"`; `cut_restarts: true` |
| Q2 ×3 | `main_creator/qendresa_q2_caption_box_all_pauses` | one captions requirement: `captions: "word_by_word"`, `caption_box: "#E5E5E5"`, `caption_position: "bottom"`, no count; a cleanup ask with `pauses: "tight"`; no `cut_restarts` |
| Decoy ×1 | `main_creator/decoy_qendresa_plain_word_captions_long_pauses` | `captions: "word_by_word"` with no count / box / position; cleanup NOT tight |
| Follow-up ×1 | `brief_extractor/qendresa_followup_two_words_all_pauses_en` | captions changed to `caption_words: 2` (middle, white); a cleanup ask with `pauses: "tight"` |

## Cost

Measured locally (free): each Main Creator prompt is about 90,000 characters (~26,000 tokens).
Gemini 3.1 Pro is $2 per million input tokens and $12 per million output tokens
(`app/services/ai_cost_control.py`), so one turn is about $0.05 in plus $0.05-0.12 out (thinking
included), roughly $0.10-0.17, double if the schema retry fires. Seven Main Creator turns are about
$0.70-1.20; the extractor turn (Gemini 3.6 Flash) is about $0.02. **Expected total about $1-1.50.**
Each command below carries its own hard cap; the caps add up to $1.90.
The harness pre-flight assumes a 4 KB prompt, so it under-counts this ~90 KB prompt; trust the estimate above. `AI_COST_CONTROL_ENABLED=true` records the spend in the development ledger, which needs the local Postgres from the repo `.env` to be reachable.

## Commands (run from this worktree, after Yasin approves)

```bash
cd /Users/yasinberkyesilyurt/projects/nova/.claude/worktrees/says-done-e-prompt-069396/src/apps/api
set -a; source /Users/yasinberkyesilyurt/projects/nova/.env; set +a
export NOVA_EVAL_MODE=live AI_COST_CONTROL_ENABLED=true AI_USAGE_ENVIRONMENT=development
export NOVA_EVAL_CAPTURE_DIR="$HOME/Downloads/kria-qendresa-test/handoff/says_done_e_live"
PY=/Users/yasinberkyesilyurt/projects/nova/src/apps/api/.venv/bin/python
for round in 1 2 3; do
  PYTHONPATH=$PWD $PY -m pytest tests/evals/test_main_creator_evals.py --eval-mode=live -rA -q \
    -k "qendresa_q1_caption_two_words_tight or qendresa_q2_caption_box_all_pauses" \
    --usage-purpose=live_eval --test-run-id="says-done-e-r${round}-20261106" \
    --max-cost-usd=0.5 --approve-reservation
  mkdir -p "$NOVA_EVAL_CAPTURE_DIR/round${round}"
  mv "$NOVA_EVAL_CAPTURE_DIR"/main_creator/*.json "$NOVA_EVAL_CAPTURE_DIR/round${round}/"
done
PYTHONPATH=$PWD $PY -m pytest tests/evals/test_main_creator_evals.py --eval-mode=live -rA -q \
  -k "decoy_qendresa_plain_word_captions_long_pauses" \
  --usage-purpose=live_eval --test-run-id=says-done-e-decoy-20261106 \
  --max-cost-usd=0.3 --approve-reservation
PYTHONPATH=$PWD $PY -m pytest tests/evals/test_brief_extractor_evals.py --eval-mode=live -rA -q \
  -k "qendresa_followup_two_words_all_pauses_en" \
  --usage-purpose=live_eval --test-run-id=says-done-e-followup-20261106 \
  --max-cost-usd=0.1 --approve-reservation
```

The harness refuses to start without all four paid-run flags and refuses any cap above $2.
Each answer that passes the structural check is written to `<capture dir>/<agent>/<fixture>.json` (also when a later fact check fails; an `agent.run` failure is written as `<fixture>--failed.json`); a failing
case prints which fact was missing (for example `caption look words None ... expected 2`).

## Reading the result

- All 3 rounds of Q1 and Q2 pass: the teaching works for her prompts; cases 6-8 are fixed at the
  source as far as one model run can show.
- A round fails on `caption_words` / `caption_box` / `pauses`: the model still drops the fact; the
  captured answer shows how (for example the look on a second requirement, or `caption_words: 1`).
  Send the captured JSON back to group E.
- Only `restart_brief` fails on Q1 (`cut_restarts` missing for "mistakes"): the caption and pause
  facts are fine; the "mistakes" line is a separate, smaller teaching gap.
- The decoy fails: the new wording over-reaches (plain asks turned tight or counted) and must be
  narrowed before merge.
