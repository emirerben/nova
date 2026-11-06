# Says-done group E: paid live check of the brief-writer teaching (RUN 2026-11-06: PASS)

## Result (run 2026-11-06, approved by Yasin): all 6 Main Creator answers PASS, $0.56 settled

The first attempt (Plan B as written below) made no model call: the cost guard reserves about
$0.454 before every Main Creator call (`estimate_call_cost_usd` counts the 16,384-token output
budget twice, as output and as thinking), which is above the 0.25-0.45 per-command caps, so every
call was refused with `ai_cost_control_policy_error` ($0.016 spent, on the extractor follow-up,
which PASSED). **Any Main Creator live command needs `--max-cost-usd` >= prior spend in that
test-run-id + about $0.46.** The re-run used two commands within the approved $1.95:

| Command (test-run-id, cap) | Fixture | Result | Settled |
|---|---|---|---|
| `says-done-e-s1-20261106`, $1.20 | Q1 | PASS: one captions row `word_by_word` + `caption_words: 2` + middle + `#FFFFFF`; cleanup `pauses: "tight"` + `cut_restarts`; title `text`/`title`, ending `text`/`global`, no `duration_s` | $0.124 |
| | Q2 | PASS: one captions row `word_by_word` + `caption_box: "#E5E5E5"` + bottom; cleanup `pauses: "tight"` (action `ask_user`: her recordings are not attached in this shell) | $0.127 |
| | case 10 control | PASS: title `text`/`title` + `timing`/`global` `duration_s: 30` | $0.071 |
| | decoy | PASS: `captions: word_by_word` only; plain cleanup (no `pauses`) | $0.086 |
| `says-done-e-s2-20261106`, $0.70 | Q1 (repeat) | PASS (same facts; action `ask_user`) | $0.079 |
| | Q2 (repeat) | PASS (same facts) | $0.077 |

Captures: `~/Downloads/kria-qendresa-test/handoff/says_done_e_live/step1/`, `step2/`. Two runs per
prompt, not three: still a small sample. Not covered: her real footage and the rendered video.


Status: run on 2026-11-06 (result above). The plans below are kept as written; their per-command
caps are too low for Main Creator (see above).
It spends real money on Gemini, so it needs Yasin's OK first.
Free checks already done: the replay evals (`pytest tests/evals/`) pass, but replay answers are
hand-written and the cassette ignores the prompt, so only this live run shows what the model
actually records for her sentences.

## What it runs

The real Main Creator (Gemini 3.1 Pro, prompt v137) on Qendresa's exact Q1 and Q2 prompts (three
times each in the full plan, since the model is not deterministic), plus one plain-ask decoy, one
brief-extractor follow-up, and one case 10 control. The test file applies the same checks it applies
in replay to the model's real answer (`meta.caption_cleanup_brief`, `meta.restart_brief`,
`meta.moment_hold_brief`, `meta.prompt_contains`), so a pass or fail is printed per case. Each real
answer is saved under `NOVA_EVAL_CAPTURE_DIR`.

Case 10 is covered too. The Q1 fixture also checks it, so running Q1 costs nothing extra for case 10.

| Case | Fixture | Expected facts (checked automatically) |
|---|---|---|
| Q1 | `main_creator/qendresa_q1_caption_two_words_tight` | one captions requirement: `captions: "word_by_word"`, `caption_words: 2`, `caption_position: "middle"`, `caption_color: "#FFFFFF"`, no box; a cleanup ask with `pauses: "tight"`; `cut_restarts: true`. Case 10 (`moment_hold_brief`): the title "lets build an agent that reads an APP before I trust it!!" is a `text`/`title` requirement and "comment READ" a `text`/`global` requirement, with NO `timing` requirement carrying `duration_s: 3` (`length_policy.creator_duration` returns None) |
| Q2 | `main_creator/qendresa_q2_caption_box_all_pauses` | one captions requirement: `captions: "word_by_word"`, `caption_box: "#E5E5E5"`, `caption_position: "bottom"`, no count; a cleanup ask with `pauses: "tight"`; no `cut_restarts` |
| Case 10 control | `main_creator/qendresa_title_hold_with_video_length` | prompt: "Start with the title \"MY WEEK\" for the first 3 seconds and make the video 30 seconds long." Expects the title as `text`/`title` AND a `timing`/`global` requirement with `duration_s: 30` (`creator_duration` = 30) |
| Decoy | `main_creator/decoy_qendresa_plain_word_captions_long_pauses` | `captions: "word_by_word"` with no count / box / position; cleanup NOT tight |
| Follow-up | `brief_extractor/qendresa_followup_two_words_all_pauses_en` | captions changed to `caption_words: 2` (middle, white); a cleanup ask with `pauses: "tight"` |

## Cost

Measured locally (free): each Main Creator prompt is about 90,000 characters (~26,000 tokens).
Gemini 3.1 Pro is $2 per million input tokens and $12 per million output tokens
(`app/services/ai_cost_control.py`), so one turn is about $0.05 in plus $0.05-0.12 out (thinking
included), roughly $0.10-0.17, double if the schema retry fires. The extractor turn (Gemini 3.6
Flash) is about $0.02.

- **Plan A:** 3 Main Creator turns, about $0.30-0.50 (up to about $1 if every call retries). Caps add up to $0.75.
- **Plan B:** 8 Main Creator turns + 1 extractor turn, about $0.80-1.40. Caps add up to $1.95 (3 × 0.45 + 0.25 + 0.25 + 0.1), under the $2 total limit.

Every single command has its own cap, and each is at most $0.5, well under the $2 hard limit per run.
The harness pre-flight assumes a 4 KB prompt, so it under-counts this ~90 KB prompt; trust the estimate above. `AI_COST_CONTROL_ENABLED=true` records the spend in the development ledger, which needs the local Postgres from the repo `.env` to be reachable.

## Commands (run from this worktree, after Yasin approves)

Shared setup (once, for either plan):

```bash
cd /Users/yasinberkyesilyurt/projects/nova/.claude/worktrees/says-done-e-prompt-069396/src/apps/api
set -a; source /Users/yasinberkyesilyurt/projects/nova/.env; set +a
export NOVA_EVAL_MODE=live AI_COST_CONTROL_ENABLED=true AI_USAGE_ENVIRONMENT=development
export NOVA_EVAL_CAPTURE_DIR="$HOME/Downloads/kria-qendresa-test/handoff/says_done_e_live"
PY=/Users/yasinberkyesilyurt/projects/nova/src/apps/api/.venv/bin/python
```

### Plan A (cheap, recommended first): about $0.35-0.55

One round of Q1 and Q2, plus the case 10 control. 3 Main Creator turns.

```bash
PYTHONPATH=$PWD $PY -m pytest tests/evals/test_main_creator_evals.py --eval-mode=live -rA -q \
  -k "qendresa_q1_caption_two_words_tight or qendresa_q2_caption_box_all_pauses" \
  --usage-purpose=live_eval --test-run-id=says-done-e-a-20261106 \
  --max-cost-usd=0.5 --approve-reservation
PYTHONPATH=$PWD $PY -m pytest tests/evals/test_main_creator_evals.py --eval-mode=live -rA -q \
  -k "qendresa_title_hold_with_video_length" \
  --usage-purpose=live_eval --test-run-id=says-done-e-a-len-20261106 \
  --max-cost-usd=0.25 --approve-reservation
mkdir -p "$NOVA_EVAL_CAPTURE_DIR/plan_a"
mv "$NOVA_EVAL_CAPTURE_DIR"/main_creator/*.json "$NOVA_EVAL_CAPTURE_DIR/plan_a/"
```

### Plan B (full): about $0.85-1.45

Three rounds of Q1 and Q2, the decoy, the brief-extractor follow-up, and the case 10 control.
8 Main Creator turns + 1 extractor turn. Caps: 3 × 0.45 + 0.25 + 0.25 + 0.1 = $1.95 in total (under $2).

```bash
for round in 1 2 3; do
  PYTHONPATH=$PWD $PY -m pytest tests/evals/test_main_creator_evals.py --eval-mode=live -rA -q \
    -k "qendresa_q1_caption_two_words_tight or qendresa_q2_caption_box_all_pauses" \
    --usage-purpose=live_eval --test-run-id="says-done-e-r${round}-20261106" \
    --max-cost-usd=0.45 --approve-reservation
  mkdir -p "$NOVA_EVAL_CAPTURE_DIR/round${round}"
  mv "$NOVA_EVAL_CAPTURE_DIR"/main_creator/*.json "$NOVA_EVAL_CAPTURE_DIR/round${round}/"
done
PYTHONPATH=$PWD $PY -m pytest tests/evals/test_main_creator_evals.py --eval-mode=live -rA -q \
  -k "decoy_qendresa_plain_word_captions_long_pauses" \
  --usage-purpose=live_eval --test-run-id=says-done-e-decoy-20261106 \
  --max-cost-usd=0.25 --approve-reservation
PYTHONPATH=$PWD $PY -m pytest tests/evals/test_main_creator_evals.py --eval-mode=live -rA -q \
  -k "qendresa_title_hold_with_video_length" \
  --usage-purpose=live_eval --test-run-id=says-done-e-len-20261106 \
  --max-cost-usd=0.25 --approve-reservation
PYTHONPATH=$PWD $PY -m pytest tests/evals/test_brief_extractor_evals.py --eval-mode=live -rA -q \
  -k "qendresa_followup_two_words_all_pauses_en" \
  --usage-purpose=live_eval --test-run-id=says-done-e-followup-20261106 \
  --max-cost-usd=0.1 --approve-reservation
```

The harness refuses to start without all four paid-run flags and refuses any cap above $2.
Each answer that passes the structural check is written to `<capture dir>/<agent>/<fixture>.json` (also when a later fact check fails; an `agent.run` failure is written as `<fixture>--failed.json`); a failing
case prints which fact was missing (for example `caption look words None ... expected 2`).

## Reading the result

- All rounds of Q1 and Q2 pass: the teaching works for her prompts; cases 6-8 are fixed at the
  source as far as the model runs can show.
- A round fails on `caption_words` / `caption_box` / `pauses`: the model still drops the fact; the
  captured answer shows how (for example the look on a second requirement, or `caption_words: 1`).
  Send the captured JSON back to group E.
- Only `restart_brief` fails on Q1 (`cut_restarts` missing for "mistakes"): the caption and pause
  facts are fine; the "mistakes" line is a separate, smaller teaching gap.
- Q1 or the control fails on `moment_hold_brief`: the model still files a title/ending hold as the
  video length (or drops a real length). Send the captured JSON back to group E.
- The decoy fails (Plan B only): the new wording over-reaches (plain asks turned tight or counted)
  and must be narrowed before merge.
