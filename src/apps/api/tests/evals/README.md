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

# 3) Live mode — only after a prompt/model/provider/schema change.
NOVA_EVAL_MODE=live GEMINI_API_KEY=… \
  AI_COST_CONTROL_ENABLED=true AI_USAGE_ENVIRONMENT=development \
  pytest tests/evals/test_clip_metadata_evals.py -v --eval-mode=live \
  --usage-purpose=live_eval --test-run-id=clip-metadata-<date> \
  --max-cost-usd=2 --approve-reservation

# 4) Single agent
pytest tests/evals/test_clip_metadata_evals.py -v --with-judge
pytest tests/evals/test_transcript_evals.py -v --with-judge
pytest tests/evals/test_platform_copy_evals.py -v --with-judge
pytest tests/evals/test_audio_template_evals.py -v --with-judge
pytest tests/evals/test_narrated_storyboard_evals.py -v --with-judge
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
| `nova.creator.main` | ✓ | `rubrics/main_creator.md` | hand-authored golden + exported | — |
| `nova.compose.sfx_placement` | ✓ | `rubrics/sfx_placement.md` | hand-authored golden (visual moments) | — |
| `nova.compose.narrated_storyboard` | ✓ | `rubrics/narrated_storyboard.md` | hand-authored sports, non-sports, and adversarial golden fixtures | `Job.assembly_plan.variants[].narrated_storyboard` |
| `nova.compose.narrated_clip_alignment` | ✓ | `rubrics/narrated_clip_alignment.md` | live-captured goldens (cacio e pepe, locked + free order; Cappadocia pinned-last, re-sorted from prod 38caaebd) | — |

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

Live mode (`--eval-mode=live` plus the mandatory attribution flags) downloads the source video from GCS,
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
# 4) OPTIONAL (paid): test the candidate against live Gemini:
NOVA_EVAL_MODE=live pytest tests/evals/test_clip_metadata_evals.py \
  -v --eval-mode=live --usage-purpose=live_eval \
  --test-run-id=clip-metadata-<date> --max-cost-usd=2 \
  --approve-reservation | tee /tmp/new.log
# 5) Compare avg scores per fixture if you ran step 4. Live runs are optional, not a merge gate.
```

### Auto `--shadow-prompts-dir` mode (live-only)

Run prod and a candidate prompt side-by-side, judge both, report a per-fixture delta — no stash dance.

```bash
# 1) Drop a candidate prompt at src/apps/api/prompts.candidate/<prompt_id>.txt
mkdir -p prompts.candidate
cp prompts/analyze_clip.txt prompts.candidate/analyze_clip.txt
$EDITOR prompts.candidate/analyze_clip.txt

# 2) Run the eval suite with shadow:
NOVA_EVAL_MODE=live GEMINI_API_KEY=… \
  pytest tests/evals/test_clip_metadata_evals.py -v --eval-mode=live \
  --shadow-prompts-dir=prompts.candidate --usage-purpose=live_eval \
  --test-run-id=clip-shadow-<date> --max-cost-usd=2 --approve-reservation
```

How it works:
- For each fixture, the harness runs the agent twice in live mode: once with prod prompts/, once with prompts.candidate/<prompt_id>.txt overlaid on prod prompts/ (any prompt file not in the candidate dir falls through to prod).
- Both runs receive structural checks. Paid judging must be run separately in
  replay mode; combining an unmetered judge with a live run would invalidate the
  run's maximum-cost approval.
- Shadow result is **informational only** — the test still gates on the primary
  run. Shadow failures are reported but never break the test.

Constraints:
- Live-mode only. Replay's `raw_text` was recorded under the prod prompt; comparing it against a candidate prompt is meaningless. The harness errors clearly if `--shadow-prompts-dir` is set without `--eval-mode=live`.
- Inline-prompt agents (`platform_copy`, `text_designer`, `transition_picker`, `clip_router`, `shot_ranker`) build their prompts from input rather than loading a file — shadow has no effect for those.

## Live-mode cost cap

Live runs hit the Gemini API. The harness requires explicit purpose, run ID,
approved maximum, and reservation acknowledgement. The hard ceiling is **$2**
for full evals and **$0.20** for a `provider_smoke` run; there is no bypass.

```bash
NOVA_EVAL_MODE=live AI_COST_CONTROL_ENABLED=true AI_USAGE_ENVIRONMENT=development \
pytest tests/evals/test_clip_metadata_evals.py --eval-mode=live \
  --usage-purpose=live_eval --test-run-id=clip-<date> \
  --max-cost-usd=2 --approve-reservation
```

The estimate is intentionally pessimistic: input tokens ≈ chars/3 of (input payload + 4KB prompt overhead), output tokens fixed at 1500 per call. Replay mode skips the check entirely.

## Request-following eval (`request_following/`)

The agent evals above grade each agent alone. This one asks the question KRI-185 slipped
through: **did the finished edit do what the creator asked, across several turns?** A fixture
is one creator thread over one footage set; every requirement in it has a deterministic
checker, and the KPI is the share of requirements `met`, reported per request type.

```bash
cd src/apps/api
pytest tests/evals/request_following tests/kria/test_replay_thread.py -v      # CI, offline
python -m tests.evals.request_following.report --phase P0 \
  --out ../../../docs/reviews/kri-185/request-following-P0.md                  # KPI per phase
```

- **Fixtures** live in `tests/fixtures/request_following/`: `footage/<id>.json` (clip
  understanding + facts as JSON, never video) and `threads/<id>.json` (turns, `requirements[]`,
  each with kind, scope, request type and a checker id from `checkers.CHECKERS`).
- **Scorer:** `scorer.score(requirements, final_plan, reply, footage=...)` returns `met | partial |
  unmet` per requirement. It also flags **reply overclaims**: the reply names a requirement
  (`claim_terms`) that was not met, with no "not done" marker.
  State requirements are re-judged on every later edit; event requirements ("do it again") are
  judged once, on the turn they were asked.
- **Runner:** `runner.run_thread` replays each turn through the real path. `v1_copilot` turns run
  the recorded model text through `EditCopilotAgent` -> `_honest_outcome` -> `compile_editor_ops`,
  so a change to any of them moves the score; `v2_kria` turns go through
  `app.kria.replay.replay_thread` (multi-turn, state carried); `recorded` turns use the stored
  outcome. `mode="live"` swaps the recorded model text for a real Gemini call on `v1_copilot`
  turns; use the same paid-run guards as the rest of this directory. Running it live is optional
  (paid); use it when investigating a suspected regression.
- **Provenance.** `east_run` is a read-only prod capture (KRI-185, regenerate with
  `python -m tests.evals.request_following.capture_east_run`). The other 30 are authored briefs
  (`author_fixtures.py`, five synthetic footage sets incl. `harbor_run`, invented places only)
  with a hand-built reference edit: they prove each checker can be passed and can fail, and
  stay out of the KPI (`awaiting recording`) until their turns carry recorded outcomes. Nine
  are multi-turn (up to three turns). P6b added briefs for the reversed-route receipt,
  consecutive-label dedupe, brief-sourced title receipt, label-each-clip, one-label
  correction and bulk "change all fonts". Some checkers read the reply (`reply_states`, a
  receipt must say what the AI saw and did); those are event checkers, judged on the turn they
  were asked. `--eval-mode live` on `report.py` needs the same paid-run guards as pytest live.
- **Baseline pin.** `east_run.json` stores `baseline` statuses and `test_east_run_baseline_is_pinned`
  fails when a code change moves one. That is the intended signal: review the movement, then
  re-stamp with `runner.stamp_baseline`. The committed report
  `docs/reviews/kri-185/request-following-baseline.md` is kept in sync by
  `test_committed_baseline_report_matches_the_replay`.
- **Wrong-landmark rate** (`scorer.wrong_landmark_rate`) is judged over clips that carry an
  `inferred` landmark fact and a ground-truth `true_landmark` (`harbor_run`, `trip`; the truth
  is on the clip record, never in `facts`). Pass `guesses={clip_id: name}` to score a live
  `landmark_guess` run against the same footage.

## Suggestion-chip replay eval (`test_suggestion_chips_evals.py`, `test_chip_suggester_evals.py`, KRI-756)

iOS app only (the web client ignores the feature). Two layers, both free, offline, no network, no
skia, no paid call:

1. the **deterministic chip engine** (`app/services/suggestion_chips.py`) decides which chips MAY
   exist: is every chip real, enabled, not already in the edit, and executable on the compile path?
2. the **wording agent** `nova.chat.suggester` only ranks and words those candidates: whatever it
   says, can the served set break the contract, and how much of its wording survives validation?

```bash
cd src/apps/api
pytest tests/evals/test_suggestion_chips_evals.py tests/evals/test_chip_suggester_evals.py \
  tests/services/test_suggestion_chips_guards.py tests/scripts/test_suggestion_chip_funnel.py -q
.venv/bin/python scripts/suggestion_chip_report.py                   # -> <repo>/.chip-eval/report.md
.venv/bin/python scripts/suggestion_chip_report.py --write-contract  # regenerate the iOS mock JSON
.venv/bin/python scripts/suggestion_chip_funnel.py --fixture         # shown/tapped/kept derivation
```

### Engine (`test_suggestion_chips_evals.py`)

- **Fixtures** (`tests/fixtures/agent_evals/suggestion_chips/*.json`): `{name, description,
  context, expect}`. The context is the engine's `SuggestionContext`; ready-state snapshots use
  a preset (`snapshot_preset`) plus a `snapshot` patch (`null` deletes a key). The `nermin-*`
  turn fixtures and `east-run-brief` (Turkish, 5 clips, hand-authored analysis) in
  `tests/fixtures/kria_turns` are projected into first-prompt and ready cases at load time. The
  12-clip case is seeded from the redacted KRI-126 prod input (real notable moments);
  `loader_projection` re-projects all 30 through the loader's own `_clip_rows`.
- **Real witnesses.** First-prompt levers are dry-run through a real `ResolvedCreatorManifest`
  and `check_strategy_for_runtime_v2`; ready-state levers through `parse_editor_operation` +
  `compile_editor_ops` over a synthetic variant (`runners/snapshot_variant.py`). Nothing is
  faked unless a fixture says `"witness": "none"`. The report prints each chip's **witness tier**
  (`strategy` | `editor` | `policy_only`) so "executable" is never claimed more strongly than it was
  proven.
- **Gates**, run on every fixture, each with negative controls so it cannot pass vacuously:
  G1 grounding, G2 capability matrix (dark kinds `opener_clip` / `pinned_text` never), G3 not
  already in the edit, G4 diversity (1-based dense ranks, 1 chip per kind, at most one copy-bearing
  chip), G5 chip #1 is `default_edit` / `Suggest an edit.`, G6 hygiene (length, language, quotes,
  stoplist, label parity for agent labels, grounded `why`), G7 closed template set (Gemini text
  only ever in `why`, never in a label or message), G8 determinism, G11 wire contract (status /
  state / retry / closed kind enum). The oracles (`KIND_REQUIRES`, `MESSAGE_DECK`, `LABEL_DECK`,
  `_is_title_like`) are restated from the planner's sources, not imported from the engine.
- **G1 is not a specificity measure.** It validates only an anchor a chip carries; the template
  engine emits a closed deck of generic labels (about a dozen distinct labels across ~150 chips,
  almost none anchored), so a 100% G1 and an empty stoplist do NOT show the chips fit THIS footage.
  No offline gate measures that. The report prints the numbers (`specificity_stats`); the real bar is
  the paid live replay + judge on `specificity_to_this_footage`, still an open TODO needing approval.
- **G9 executability (COMPILE PATH ONLY).** `executability_cassettes.json` holds a hand-authored
  planner answer per (kind, state), replayed through `replay_v2_strategy` / `replay_v2_editor`.
  Pre-render treatment kinds also diff `optional_treatments` before/after
  `check_strategy_for_runtime_v2`, because the policy drops disabled treatments silently. This does
  NOT prove today's prompts choose that answer. The first-prompt `hook` is graded "reaches wording
  approval with a candidate", not "executes an op".
- **Known gaps** are strict xfails (`KNOWN_GAPS` in `suggestion_chips_harness.py`; empty today).

### Wording agent (`test_chip_suggester_evals.py`, `chip_suggester_harness.py`)

- **Cassettes** (`tests/fixtures/agent_evals/chip_suggester/*.json`, 25): hand-authored raw model
  answers, NOT recorded output, natural and adversarial (invented / duplicate / guessed cids for
  gated-off, already-in-edit and dark kinds, extra `message` / `kind` keys on a row, cross-kind,
  hype, digit / quote / emoji, footage text in a label, prompt injection, unknown placeholders,
  wrong language, ungrounded and handle / URL `why`, non-JSON, wrong shape, fenced JSON). Each is
  replayed through the REAL agent `parse`, the REAL task body `kria_suggester._run_agent` (with the
  model client and Redis swapped for in-memory fakes) and the REAL serve join. `{{cid:<kind>}}`
  is the live candidate id; `{{guess:<kind>}}` is the id an adversary who knows the cid formula
  would mint (live only if the engine allowed that kind).
- **G10 fuzz.** 45 contexts x 30 seeded answers (random cids from this and other contexts, junk and
  plausible labels, grounded and hostile `why`, extra row keys, broken JSON) must leave the served
  set contract-clean: every chip a live candidate with `message`, `kind`, `capability`, `anchor`
  and `requires_protocol` copied from it, no flag-off / already-in-edit / dark kind, label and
  `why` valid, Gemini text out of labels and messages, limits held. Mutation controls break the
  label validator, the `why` validator and the pool check in turn and require the fuzz to go red.
- **Label acceptance** (`label_corpus.json`, 130 hand-authored labels): recall of plausible
  creator-voiced rewordings (bar >= 80%), rejection of cringe / wrong-kind / leaking / hostile
  labels (bar 100%), and how much loosening to stoplist-only would let through. The corpus is MY
  guess of the model, so the rate is a floor on strictness, not a measurement of Flash.
- **Backend gaps** found by the eval are strict xfails (`BACKEND_GAPS` in
  `chip_suggester_harness.py`), listed in the report.

### Telemetry (contract section 8), asserted with `structlog.testing.capture_logs`

`suggestion_chips.shown` (once per final `set_id`; the pending set does not emit),
`suggestion_chips.agent_run` (one per run, with `fallback_reason` in schema_error | label_rejected |
timeout | budget | stale_digest | flag_off | run_cap, each triggered from its real cause) and
`suggestion_chips.tap` (the 8-key stamp). The stamp round-trips into
`scripts/suggestion_chip_funnel.py`, which derives offered / tapped / kept, split by `label_source`
and `wording`, and prints the chip-#1 bias (a media-only send counts as a tap of chip #1 because the
stamp has no explicit-vs-text flag). The old iOS PromptChips only filled the composer, so there is
NO tap baseline: compare completed first renders and chat edits per render between a flag-off and a
flag-on cohort, not a tap rate.

### Paid / approval-needed items (NOT run; nothing here spends money)

1. **Live tap-to-op proof.** Run today's `main_creator` and `edit_copilot` on each chip message and
   check the claimed op appears (development ledger, hard cap $2, no judge; bar 95%). First record
   a cassette per kind from the live answer; the hand-authored answers in
   `executability_cassettes.json` are the reference to diff against. Until then the executability
   bar is proven only on the compile path.
2. **Empathy / cringe judge** of the finite template set and of recorded agent labels with
   `audit-user-empathy` (paid Anthropic grader; use the worktree `.venv`). The deck is 7 kinds x 2
   languages.
3. **Blind old-vs-new pairwise judge** on about 20 real sessions (`llm_judge.py` has no pairwise
   mode, so this needs a new script beside it; the sessions are not in the repo). Pass bar >= 70%
   preference, founder agrees on 10. UNMEASURED.
4. **Flash-vs-template wording A/B** by `label_source` in real taps (needs the flag on for a
   cohort; fix the `label_source` attribution gap in the report first).
5. **Live replay of `nova.chat.suggester`** on the nermin / east-run contexts to record real
   outputs and replace the hand-authored label corpus with a measured acceptance rate.
6. **`opener_clip` cassette** proving filename-to-hero resolution through the planner; only then
   may `CLIP_ORDINAL_PROVEN` flip.

## CI

- **Default CI:** runs structural-only on every PR (~30s, no secrets).
- **Paid:** `.github/workflows/agent-evals.yml` permits manual runs only for
  prompt/model/provider/structured-output changes (no schedule). Live runs are
  optional, never a merge requirement. The workflow uses the development key and reservation ledger.
- Successful paid responses are uploaded as replay-capture artifacts; review and
  copy the capture into the matching fixture directory before subsequent prompt work.

## Related

- `tests/quality/eval_scoring.py` — the original recall@3 launch gate for hook scoring.
- `app/agents/_runtime.py` — runtime that this harness relies on (`ModelClient`, `Agent`, `run_with_shadow`).
- `app/agents/{template_recipe,clip_metadata,creative_direction,transcript,platform_copy,audio_template,song_classifier,music_matcher}.py` — the eight agents under test.
- `scripts/export_eval_fixtures.py` — DB → fixture exporter.
- `scripts/reanalyze_underbaked_templates.py` — one-off to re-run two-pass analysis on templates with under-baked `creative_direction`.
