# KRI-185 P6b: request-following golden set, live evals and KPI (2026-09-25)

Closes the second half of KRI-192 (KRI-212). Harness: `src/apps/api/tests/evals/request_following/`.
Everything below is either a number the harness printed or a live run whose command is listed;
nothing is estimated except where it says so.

## Headline

1. **Golden set: 30 threads, 76 requirements, 14 of 14 request types covered.** East Run (prod
   capture) plus 29 authored briefs over 5 synthetic footage sets (`food_day`, `trip`, `sport`,
   `vlog`, and the new `harbor_run`; invented places only). 9 authored threads are multi-turn
   (8 two-turn, 1 three-turn). The 17 P6b briefs cover the reversed-route receipt, consecutive-
   label dedupe, the brief-sourced title receipt, label-each-clip, one-label correction and
   "change all fonts" as one edit, plus mixed-language labels, no-fact labels and standing rules
   that must survive a later turn.
2. **The measured KPI is still one thread.** Authored threads remain `awaiting recording` and
   are excluded from the KPI, as the harness convention says: they carry a hand-built reference
   edit, not a recorded outcome of the system, and this harness has no way to record one for
   them without the paid v2 planner path. So the KPI below is East Run only: **44% met (4/9), 4
   reply overclaims**, unchanged from the P6a baseline. Read it as a trend line, not a rate.
3. **The replay KPI is flat from P0 to P5, and that is expected, not good news.** East Run
   replays *recorded* model text through the *current* v1 copilot path. P1-P4 changed the
   phone/v2 planner, which that path never touches, and the recorded text predates every phase.
   A phase can only move this number when the model is called again, which is what the live
   column is for.
4. **Live evals: none is fully green, and one is incomplete.** `landmark_guess` passes 5/5
   (structural only). `main_creator`, `edit_proposal` and `clip_intent_planner` each have 1-2
   failing fixtures; each one that I could compare fails on the prior prompt version too, or
   drifted before v39, so none is a demonstrable regression from this train's bumps. They are
   real gaps the replay-only CI never exercised. `edit_copilot` v47 ran 76 of 107 fixtures
   (75 pass, 1 fail) before the local dev AI budget cap stopped it; the four new v47 goldens
   (label-each-clip, one-label fix, bulk fonts) all passed live, the 31 un-run are older
   fixtures.
5. **Wrong-landmark rate is wired:** `harbor_run` 14% (1 of 7 guesses wrong), `trip` 25% (1 of
   4). These guesses are recorded synthetic guesses, not a measurement of the model; the
   number proves the denominator and the scorer, and `wrong_landmark_rate(footage, guesses)`
   scores a real run against the same footage.

## Golden set

| Footage | Threads | Notes |
|---|---:|---|
| `east_run` | 1 | prod capture (KRI-185), 7 turns, the only measured thread |
| `food_day` | 4 | exact title, described labels, readability, fonts-then-forbid |
| `trip` | 6 | chronological, route, title facts, label-each-clip, Turkish and English landmark labels |
| `sport` | 6 | exact labels, explicit order, duration+pacing, no-facts labels, title-only font, exclude-then-duration |
| `vlog` | 6 | creator facts, selection, restructure, default title, change-all-fonts, exact title then labels |
| `harbor_run` | 7 | reversed route (+ follow-up, + control), dedupe, title from brief, landmark correction, selection+duration+pacing |

Requirements per request type (all threads): title_exact 2, title_described 4, label_exact 4,
label_described 22, creator_facts 4, order_chronological 2, order_explicit 2, order_route 8,
selection 6, duration 5, pacing 3, readability 2, restructure 3, style 9.

New checkers (all deterministic, pure): `label_no_consecutive_repeat`, `text_avoids`,
`labels_none`, `font_all_equal`, `clips_unchanged`, `label_single_change`, and the first
reply-reading checker, `reply_states` (a receipt must say what the AI saw and did; judged on
the turn it was asked, so a later reply cannot launder it). Every authored reference scores
all `met` and an untouched attachment-order edit never does (`test_fixtures.py`).

What the authored set does **not** yet prove: that the *system* follows these briefs. A
reference is the answer key. Recording outcomes (P6c) needs the v2 planner run end to end
against these footage sets, which this lane deliberately did not do.

## KPI per phase (East Run, replay at each phase's merge commit)

Replayed by overlaying this harness on each phase's tree (`report.py --phase Pn`).

| Phase | Tree | Met | Partial | Unmet | Overclaims | Live (1 sample, same thread) |
|---|---|---:|---:|---:|---:|---|
| P0 honest replies (#1217) | `79871f372` | 4/9 | 1 | 4 | 4 | 4/9, 4 overclaims |
| P1 phones on v2 (#1218) | `59ca3da0e` | 4/9 | 1 | 4 | 4 | not run |
| P2 brief + receipts (#1221) | `d9fd7b6a0` | 4/9 | 1 | 4 | 4 | not run |
| P3 clip facts (#1225) | `a3a94cb86` | 4/9 | 1 | 4 | 4 | not run |
| P4 unified montage (#1236) | `e53a8d8b3` | 4/9 | 1 | 4 | 4 | not run |
| main at branch point | `ca2a33a52` | 4/9 | 1 | 4 | 4 | 4/9, 4 overclaims |
| P5 copilot sees clips (#1255 head) | `032a33452` | 4/9 | 1 | 4 | 4 | not run (dev AI budget exhausted, see below) |

The live column calls the real model on the five `v1_copilot` turns (about $0.04-0.07 per
thread). Two honest readings:

- P0's promise (a reply names what it did not do) did **not** reach the brief turn: live, the
  brief turn still answers "Edit text. Everything else is unchanged." while 0 of 11 clips carry a
  label. Both live runs kept 4 overclaims. The refusal turns (recreate, 15 seconds) are honest
  ("I can't ..."), so P0 fixed the rejected-op path and not the partially-applied-brief path.
- The recorded `variant_snapshot` was captured before P5, so replaying it cannot show the
  copilot seeing clips. P5's effect needs a new recording of the East Run turns with the v47
  snapshot (`capture_east_run` against a fresh thread), not another replay.

### Per request type (which requirement is measured where)

| Request type | Measured (East Run) | Authored refs | Expected mover | Evidence today |
|---|---|---:|---|---|
| title_exact | not measured | 2 | P0/P2 | reference only |
| title_described | not measured | 4 | P2, #1254 receipt | reference only |
| label_exact | not measured | 4 | P5 (#1255 one-label edit) | reference only |
| label_described | 0/1 unmet, overclaim | 22 | P3+P4 (labels from facts), #1254 dedupe | live: brief turn still no labels |
| creator_facts | 1/1 met | 4 | P2 | measured, met at every phase |
| order_chronological | not measured | 2 | P3 | reference only |
| order_explicit | not measured | 2 | P2 | reference only |
| order_route | 0/1 unmet, overclaim | 8 | P3/P4, #1254 reversed-route receipt | measured unmet at every phase |
| selection | 1/1 met | 6 | P2 | measured, met |
| duration | 0/1 partial (16.6s vs 15s) | 5 | P2/P4 | measured partial |
| pacing | 1/1 met, overclaim | 3 | P2 | measured met |
| readability | 0/1 unmet, overclaim | 2 | P4 | measured unmet (no labels to read) |
| restructure | 0/1 unmet | 3 | P0/P1 | measured unmet ("create again" does nothing) |
| style | 1/1 met | 9 | #1255 one-op bulk fonts | measured met (single font ban); bulk untested live |

"Expected mover" is the phase the KRI-185 plan assigned to the behaviour, not a measurement.

## Live evals

Command shape (no judge, per the run rules):
`NOVA_EVAL_MODE=live AI_COST_CONTROL_ENABLED=true AI_USAGE_ENVIRONMENT=development pytest
tests/evals/test_<agent>_evals.py --eval-mode=live --usage-purpose=live_eval --test-run-id=<id>
--max-cost-usd=2 --approve-reservation`. Gemini keys came from the repo `.env`; no 429 or
credit error was hit.

| Agent | Version | Tree | Result | Fixtures |
|---|---|---|---|---|
| `main_creator` | `2026-09-24-v39` | main `ca2a33a52` | **27 pass, 2 fail** | 29 |
| `edit_proposal` | `1.18.0` | main `ca2a33a52` | **19 pass, 1 fail** | 20 |
| `clip_intent_planner` | `2026-09-24.1` | main `ca2a33a52` | **7 pass, 2 fail** | 9 |
| `landmark_guess` | `2026-09-25.1` | #1254 head `6aac4fb1b` | **5 pass** (structural only, see note) | 5 |
| `edit_copilot` | `2026-09-25-v47` | #1255 head `032a33452` | **75 pass, 1 fail, 31 not evaluated** (budget) | 107 |

Cost. The harness pre-flight estimates are tiny (`edit_proposal` $0.012, `clip_intent_planner`
$0.005; `main_creator` has no cost spec so it is not gated). Measured: about $0.04-0.07 for
each 5-call East Run live report. The local dev ledger (`ai_cost_reservations`, environment
`development`, cap `AI_DEVELOPMENT_MONTHLY_BUDGET_USD` = $2.50) already held $3.84 settled +
$0.93 released this month across 508 calls before and during these runs, so it, not Gemini,
stopped the last runs (below). Order actually run: main-based agents, the two report runs,
`landmark_guess`, then `edit_copilot` last.

**Blocked by the cap, not run:** `edit_copilot` v47 stopped at fixture 76 of 107 with
`agent.run failed: ai_budget_exhausted` (28 fixtures) and `paid call ... already settled` (3
fixtures: `kria_bulk_followup_impossible_all`, `kria_bulk_followup_satisfiable`,
`sfx_hallucinated_effect_id`, a ledger idempotency error, not a model answer). A rerun of the 32
under a new `--test-run-id` failed the same way immediately, so I stopped rather than raise the
cap (`AI_DEVELOPMENT_MONTHLY_BUDGET_USD`), which is a spending decision I was not given. The
32 ids are the un-run set; to finish: raise that env var for one process, then
`pytest tests/evals/test_edit_copilot_evals.py -k "<ids>" --eval-mode=live ...` from the #1255
worktree. The P5 live East Run report was blocked the same way. The four new `kria_v47` goldens for label-each-clip, one-label correction and bulk fonts
(`font_all_bars_one_appearance_op_kria_v47`, `font_labels_group_appearance_op_kria_v47`,
`label_each_clip_from_facts_kria_v47`, `label_one_correction_edit_text_kria_v47`) all **passed
live**, exact-op assertions included, so the behaviours #1255 adds are gated. What is unknown is
the 31 other fixtures listed above, which are older goldens.

Note on `landmark_guess`: it needed the dev bucket (fixtures point at `clips/devtest-...`), so
that one run also got `STORAGE_BUCKET`, `STORAGE_PROVIDER` and `GOOGLE_APPLICATION_CREDENTIALS`
from the repo `.env` for a read-only download by the harness's own uploader. Its fixtures'
`live_note` says live is structural only (the dev clip's real content differs from the
recorded answer), so 5/5 means "parses, in range, no leaks", not "names the right place".


### Failure detail and classification

**`main_creator` v39 (2 fail).** Both expect `propose_strategy` and got `ask_user`.
- `kri127_open_vocabulary_dish_labels`: asks "what are the dish names?" on 3 of 3 probes at
  v36 (P0 tree), v38 (P3) and v39. Pre-existing; not a v39 regression.
- `kri129_caption_food_and_weather`: asks "what exact text for the weather caption?". Probes
  (3 each): v36 P0 tree 0 asks (all `propose_strategy`), v36 P1 tree 2 asks, v37 2 asks, v38 3
  asks, v39 3 asks. Behaviour drifted toward asking between P0 and P3 and stays there; the
  prompt bumps in P2/P3 are the candidates, v39 is not where it started. The fixture's own
  contract says a *described* caption is authored server-side, so asking is the wrong answer.
  Fixture text is synthetic (`user_message`: 'Say "post match feast" on the food clips, and
  add a caption about the weather on the park clips.').

**`edit_proposal` 1.18.0 (1 fail).** `narrated_vo_cue_asr_errors`: "image-09 repeated while an
unused source was still available". Fails 3 of 3 reruns on 1.18.0 and 2 of 2 on 1.17.1 (P0
tree). Pre-existing, not a 1.18.0 regression.

**`clip_intent_planner` 2026-09-24.1 (2 fail).**
- `golden/cooking`: `terminal_schema` after the retry. The model emits `op: "caption"` with
  `attribute: null` (it puts the phrase in `creator_text` only), which the schema rejects. 3 of
  4 probes fail on 2026-09-24.1 (P3 tree), 5 of 5 on 2026-09-22.3 (P2 tree). Pre-existing.
- `golden/order_by_capture_time`: the model returns `order` intents but never sets
  `order_by: "capture_time"` (4 of 4 probes `order_by=None`, same on the P3 tree), so the
  expectation `{op: order, order_by: capture_time}` fails. The `order_by` note is only injected
  when `clip_facts` is true, which this fixture sets, so this is the model ignoring the
  instruction, not a wiring gap. Present since KRI-189 shipped; replay never caught it.

**`edit_copilot` v47 (1 real fail).** `phone_title_top_left_inter_no_shadow`: exact-ops fixture
expects three `patch_text_style` ops (`font_family: Inter`, `size_px: 64`, left/custom
position, ...) and the model returned `ops == []` with `intent == "edit"` and no clarification.
One sample; the cap prevented a rerun, so I cannot say whether it is flaky.

None of the above was fixed here (other lanes own the code). The failing outputs are in the
scratchpad logs of this run, not committed.


## Wrong-landmark rate

| Footage | Rate | Guesses judged |
|---|---:|---:|
| harbor_run | 14% | 7 |
| trip | 25% | 4 |
| food_day, sport, vlog, east_run | n/a | 0 |

The trip guess `Red Valley` (truth `Rose Valley`) and the harbor guess `Halden Watchtower`
(truth `Fort Halden`) are the deliberately wrong ones. The `landmark_guess` golden fixtures
point at dev-bucket clips whose real content differs from the recorded answers (their own
`live_note` says structural only), so a live landmark run cannot be scored against
`true_landmark` yet; that needs a footage set with real media.

## Baseline

`request-following-baseline.md` was regenerated because the authored-thread count (12 to 29)
and the wrong-landmark table changed. The East Run **pin was not re-stamped**: no requirement
status moved (`test_east_run_baseline_is_pinned` passes untouched).

## Not done, and why

- **Recording authored threads** (would put them in the KPI). Needs the v2 planner run on
  synthetic footage, not a replay of text; out of scope for an evals-only lane.
- **A live `landmark_guess` accuracy number.** See above.
- **Judge runs** (`--with-judge`). Not requested; the Anthropic judge bills separately.
