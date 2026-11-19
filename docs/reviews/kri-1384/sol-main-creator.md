# KRI-1385: a safe `MAIN_CREATOR_MODEL=gpt-6.1-sol` (and one model registry, KRI-1393 step 4)

Status: implemented and offline verified; live model checked on the development ledger. After
the prompt work below, Sol passes 95 of 118 fixtures and Gemini passes 96. The quality gate for a
production flip (Sol at or above Gemini) is **not met** in this sample (see Prompt work for Sol).
Prod config is unchanged.

## What changed

| Area | Before | After |
| --- | --- | --- |
| Model facts | Non-Gemini names spelled in 16 files (dispatch, preflight, efforts, price, ledger SQL, comparison, credit plan) | `app/agents/model_registry.py` holds provider, efforts, `effort_ladder`, price, media and tool support. `tests/agents/test_model_registry_sentinel.py` fails on any other spelling (on the merge base it flags 41 spellings in 16 files, the ledger SQL included). |
| Price | `_price_for` branched on names | `_price_for` reads the registry. 744 old-vs-new cost, estimate and provider cases are equal (`scripts/registry_equivalence.py`). `PRICE_VERSION` is unchanged because no rate changed. |
| Budget | A Sol Main Creator call had no capability: unmetered with `AI_COST_CONTROL_ENABLED` off, only inside the shared OpenAI monthly cap when on | New `planning` capability for `nova.creator.main` and `nova.edit.copilot` on OpenAI/Anthropic, always metered, with `AI_OPENAI_PLANNING_BUDGET_USD` / `AI_ANTHROPIC_PLANNING_BUDGET_USD`. Cap sums read the capability each reservation stored, not model names in SQL. |
| Fallback | Sol primary had no fallback; any Sol failure ended the turn | Fallback `gemini-3.1-pro-preview`. When the next model is at another provider, any provider or ledger failure moves the turn: outage, unknown outcome, 4xx, spent budget, attribution refusal. Schema and refusal failures still end the turn. |
| Error classes | Text-provider 5xx and connection errors were `ProviderOutcomeUnknownError` | A provider 5xx/429 or a connection that never opened is `TransientError` (released, retried within `max_attempts`). A read timeout is `ProviderDeadlineExceededError`. Any other failure after the request was sent stays unknown. The paid intent review (`_openai_vision`) keeps 5xx as unknown (KRI-1171 contract). |
| Ladder | `startswith("gemini-3")` | The registry `effort_ladder`. A deadline steps one rung once per model (Sol `medium` -> `low`), and the next model runs on its lightest rung. Gemini behaviour is identical for truncation and for the `high` -> `medium` step. |
| Thinking row | Text providers ignored `thought_summary_callback` | Sol streams `reasoning.summary="concise"`; Sonnet streams `display: "summarized"` thinking. Both feed the unchanged `ThoughtSummaryPublisher` under one total deadline. |

## Verification

Offline (this branch):

- Touched areas: 15,254 passed across `tests/agents`, `tests/services`, `tests/kria`, the intent-review integration suites and `tests/tasks/test_task_time_limits.py`. The 7 failures are `test_render_review_admission_postgres` / `test_initial_render_review_admission_postgres`, which fail identically on `origin/main` (local storage). The full suite had 2 more local-only failures that pass without the repo `.env`.
- `pytest tests/evals/` (replay, clean env): 4,247 passed, 29 skipped, 2 xfailed.
- Red control: the new and changed tests were run against a checkout of the merge base (`b2df43ca5`). 22 fail there and pass here: 6 Sol ladder and fallback replays, 10 provider classification and streamed-summary contracts, 3 planning-ledger tests, 2 unknown-outcome ledger tests and 1 registry guard. The sentinel cannot import there; on the merge base's files it flags 41 spellings.

Live, development ledger (`nova_kri1384_dev`, `AI_USAGE_ENVIRONMENT=development`, `live_eval`, each run capped at $2):

### Forced OpenAI failure, real Main Creator with `MAIN_CREATOR_MODEL=gpt-6.1-sol`

`OPENAI_BASE_URL` pointed at `scripts/fake_openai.py` or at a closed port, with fixture `alternating_matches`.

| Failure | Sol calls | Sol ledger rows | Answer | Turn time |
| --- | --- | --- | --- | --- |
| HTTP 503 | 3 (`max_attempts`) | 3 released, $0 | Gemini 3.1 Pro, `ok_fallback`, fixture passed, settled $0.0349 | 14.5 s |
| Connection dropped after the request was sent | 1 | 1 `unknown`, $0.1925 held, never repeated | Gemini 3.1 Pro, `ok_fallback`, passed, $0.0228 | 13.5 s |
| Connection refused | 3 | 3 released, $0 | Gemini 3.1 Pro, `ok_fallback`, passed, $0.0205 | 17.1 s |

### Live eval: the main_creator fixtures on Sol (`medium`)

Run in three batches of 38 to 40 fixtures (`kri1384-sol-mc-b{1,2,3}-20261113`), structural checks only (no judge). Per-fixture results: `live-eval-sol-medium.json`.

| Measure | Sol `medium` |
| --- | --- |
| Fixtures passing | **71 / 118 (60%)** (replay of the recorded answers: 118 / 118) |
| Calls | 122 (4 schema retries), all `outcome=ok` on Sol, 0 fallbacks, 0 unknown outcomes |
| Ledger | 122 `planning` rows, settled $4.39 (mean $0.039, p50 $0.044, p95 $0.053, max $0.057 per call); about $0.21 reserved per call until settled |
| Latency per call | p50 12.1 s, p95 23.4 s, max 29.0 s |
| Tokens per call (mean) | 17.9k in, 206 reasoning, 353 answer |

The 47 failures fall into five groups:

| Group | Count | Example |
| --- | --- | --- |
| Requirement not extracted (`brief_updates` empty where one is expected) | 15 | `decoy_kri1038_en_spoken_words_stale_title`: Sol returned `brief_updates: []`; the recorded answer extracts "words one at a time as they are spoken". |
| Asked a question where the fixture expects a proposal | 14 | `barcelona_shot_labels`, `kri422_per_shot_texts`, `kri1151_insert_between_speech_en/tr` |
| Text update missing | 4 | `kri1292_text_hold_tr_bir_saniye_boyunca`: no text update for "PROVOLETA 🧀" |
| Reply wording | 4 | `kri1151_talking_cutaways_*`: the reply explains the audio swap without the word "muted" |
| Other | 10 | exact hook text recorded from Gemini (`kri586_*`, `kri506_revision_candidate`), title mode, a missing creative-copy decision |

EN/TR parity (`parity-sol.json`): all 4 Main Creator pairs match, 8 of 8 decisions as intended, settled $0.22. One Sol call hit its 130 s deadline (`ProviderDeadlineExceededError`), and Gemini 3.1 Pro answered that call through the new cross-provider fallback. Its reservation is held as `unknown` ($0.195).

### Controls: is the gap Sol, the fixtures, or the effort level?

Two capped control runs on the 47 Sol failures (`controls.json`, from `scripts/compare_controls.py`):

- **Live Gemini 3.1 Pro at `high`** (the production setting). It ran 39 of the 47; the $2 run cap refused 8 before contact. Settled $3.23: $0.027 to $0.150 per call (mean $0.081), 7 to 61 s (p50 25 s).
- **Sol at `high`** on the two largest groups (29 fixtures). Settled $1.47: mean $0.051 per call against $0.039 at `medium`, and p50 about 24 s against 12 s.

| Sol `medium` failure group | Sol failures | Gemini `high` passes (of those run) | Sol `high` passes |
| --- | --- | --- | --- |
| Requirement not extracted | 15 | 7 of 8 | 0 of 15 |
| Asked instead of proposing | 14 | 6 of 14 | 0 of 14 |
| Text update missing | 4 | 3 of 3 | not run |
| Reply wording ("muted") | 4 | 2 of 4 | not run |
| Other | 10 | 3 of 10 | not run |
| **All** | **47** | **21 of 39** | **0 of 29** |

Reading:
- About half of Sol's failures also fail on live Gemini. Those fixtures pin wording or a recorded hook, or a choice the live production model no longer makes either.
- The other half are Sol-specific. The clearest gap is requirement extraction: live Gemini fills `brief_updates` in 7 of the 8 such fixtures it ran, while Sol leaves them empty at both `medium` and `high`.
- Raising Sol's effort recovered none of the 29, and made each call about twice as slow and about 30% more expensive.
- The likely lever is the prompt (how the `brief_updates` contract is taught), not the effort setting. Prompt work for Sol below tests this.
- Gemini was not run on the 71 fixtures Sol passed, so these runs give no exact Gemini pass rate. Prompt work for Sol below measures it over all 118.

Spend on the development ledger across all KRI-1384 runs: $9.73 settled, plus $0.39 held as unknown (two Sol reservations: the forced drop and the parity deadline). The largest single run was $1.72.

### Prompt work for Sol

All runs used a fresh development ledger (`nova_kri1384_solprompt_dev`, `AI_USAGE_ENVIRONMENT=development`, `live_eval`). Each batch was its own run, capped at $2. `scripts/run_live_batches.sh` runs the batches, `scripts/summarize_live_eval.py` scores them and `scripts/compare_runs.py` compares runs per fixture. The harness uses the default flags (for example `CLIP_INTENTS_ENABLED` off) unless a fixture sets `prod_profile`. Both models ran on the same harness.

**The bar.** Live Gemini 3.1 Pro at `high`, the shipped default, passed 96 of 118 (81%). It made 124 calls for $8.98 settled (`live-eval-gemini-full.json`).

| Measure | Sol `medium` before | Sol `medium` after | Gemini `high` |
| --- | --- | --- | --- |
| Fixtures passing | 71 / 118 (60%) | **95 / 118 (81%)** | **96 / 118 (81%)** |
| EN/TR Main Creator pairs | 4 of 4 match, 8 of 8 intended | 4 of 4 match, 8 of 8 intended (`parity-sol-after.json`) | not run |
| Mean settled cost per call | $0.039 | $0.036 | $0.072 |
| p50 / p95 per call | 12.1 s / 23.4 s | 13.3 s / 22.0 s | 20.7 s / 49.7 s |

From before to after, 26 fixtures went from fail to pass and 2 from pass to fail. Against Gemini, 90 pass on both, 17 fail on both, 6 pass only on Gemini and 5 pass only on Sol (`compare-sol-gemini.json`).

**What was wrong.** A rerun of Sol's 47 failures with its answers captured failed 44 of them again, so the failures were systematic, not noise (`hillclimb/sol-diag.json`). It was not a schema problem. Main Creator sends OpenAI no JSON schema (it uses `json_object` and the prompt's envelope), and Sol returned `brief_updates`, `delegates_all_choices` and `starts_fresh` every time. The answers showed three behaviours:

1. Sol read the brief contract literally. On a first turn the FULL CREATOR REQUEST CONTRACT is the creator's own message, because `render_brief_request` returns the message when nothing is recorded. The brief section says never to re-list a requirement already in the contract. So for "Add word by word captions to my video" Sol proposed karaoke captions and recorded no requirement. Whenever Sol asked a question, it also returned `brief_updates: []`, although the planner keeps a question turn's requirements.
2. Sol asked instead of proposing whenever part of a request was unavailable or approximate, for example: "can't lock each labeled shot to 1.5 seconds or apply a warm grade. Would you accept…?"
3. Sol asked for what the server finds itself: whether a clip has speech, which upload is which named shot, and the word the creator asked to be asked about later.

**One hypothesis per run.** Each run had targets (Sol failed, Gemini passed) and controls (fixtures the change could break). Per-run results are in `hillclimb/`.

| # | Change | Targets | Controls | Spend | Kept |
| --- | --- | --- | --- | --- | --- |
| H1 | Brief section (Main Creator only; the brief extractor strips it): a requirement is in the contract only when listed under "Creative brief vN", and every stated requirement is recorded whatever `action` is. | 0 / 18 → 18 / 18 | 6 / 6 | $1.19 | yes |
| H2 | Base prompt rule: a limit is not a reason to ask. Propose the closest plan and say in `summary` what will differ. Still ask where a rule says to, for hook or title wording, and for a choice between two different edits. | 0 / 7 → 4 / 7 | 7 / 7 | $0.81 | yes |
| H3 | Same rule: never ask for what the server finds itself (speech, which upload is which named shot, a word the creator wants asked later). | 0 / 3 → 1 / 3 | 6 / 6 | $0.45 | yes |

**Gemini on the new prompt.** Gemini reran the 33 affected fixtures it had passed and passed 32 (`hillclimb/gem-regress.json`). The one failure, `kri789_one_take_start_end_lines_trim`, passed 3 of 3 reruns, so it reads as noise (`hillclimb/gem-kri789.json`). The other 85 fixtures were not rerun on Gemini, so Gemini's full rate on the new prompt is not measured.

**Verdict.** Sol at `medium` passes 95 of 118, one fixture under Gemini's 96. By the stated bar (Sol at or above Gemini) the gate is **not met** in this sample, and readiness is not claimed. The one-fixture gap is inside the measured run-to-run noise: Gemini changed its verdict on 6 of 39 hard fixtures between two runs, and Sol on 3 of 47. A paired rerun of both models over all 118 would cost about $13. The 6 fixtures only Gemini passes:

- `kri586_generate_after_choice` pins the hook text "Sunrise miles", recorded from Gemini.
- `n2_voiceover_fullscreen_photo_sequence` pins the beat id `bathtub`. Sol named it `bathtub-photo`.
- `kri1151_talking_cutaways_insert_open_show_when_mentioned` needs the word "muted". Sol wrote "muting your voice".
- `kri422_per_shot_texts` asserts exactly 9 brief entries. Sol added a 10th, an `order` requirement for the numbered shot list.
- `barcelona_shot_labels` left out `render_intent_evidence` in the final run. It passed both earlier runs on this prompt.
- `sport_labels_bottom_right` dropped the per-clip sport label, which it kept in the baseline. This is one sample. It may be a regression from H2 and was not re-measured.

Spend for this work: $21.17 settled, $0 held. The final Sol run hit the development ledger's 80% guard ($20 of the $25 monthly budget) after 83 fixtures. The 35 it refused ran as a second batch set on the same prompt, with the budget raised to $30 for that run (`kri1384sp-sol-finalc`).

### Thinking row on the local stack

`scripts/thinking_row.py` ran against the local API (`uvicorn app.main:app` from this branch on `nova_kri1384_dev`, `THOUGHT_SUMMARIES_ENABLED=true`). It creates a user and a creation thread through `POST /creation-threads`, runs the real `MainCreatorAgent` on Sol with a real `ThoughtSummaryPublisher` bound, and reads `GET /creation-threads/{id}/thought-summaries` (`thinking-row.json`).

| Run | Turn | Action | GET | Summary (completed, also in reopened history) |
| --- | --- | --- | --- | --- |
| 1 | 20.4 s | `propose_strategy` | 200 | "**Organizing multimedia elements and ranking criteria**" (55 chars) |
| 2 | 20.9 s | `propose_strategy` | 200 | "**Organizing audio and photo content**" (38 chars) |
| 3 | 42.0 s | `propose_strategy` | 200 | "**Finalizing selection criteria**" (33 chars) |

Why `concise`: with `reasoning.summary` set to `auto` or `detailed`, Sol returned a reasoning item with no summary parts in 4 of 4 Main Creator calls. `concise` returned parts in 2 of 2 (one heading only, one heading plus two sentences). At `medium`, Sol reasons briefly here (about 200 to 470 reasoning tokens per call), so the row usually shows one heading, not Gemini's paragraphs.

Not exercised: the message route's format and footage prerequisites (`POST /creation-threads/{id}/messages`). The script binds the publisher the way the planner does (`thought_summary_callback=current_thought_publisher()`, unchanged code). The iOS simulator check in the parent plan was not run.

## Prod config audit (read-only; nothing changed)

Fly secret names were listed with `fly secrets list -a nova-video`. Values were not read.

- **OpenAI organization.** The text client sends no `OpenAI-Organization` header. The SDK would read `OPENAI_ORG_ID` / `OPENAI_PROJECT_ID`, and neither is a Fly secret. Sol planning therefore bills the default organization of `OPENAI_API_KEY`, the same as the suggestion agent and intent review. `GENERATED_MEDIA_BILLING_ORGANIZATION` (an `org-...` id) applies only to image and narration calls.
- **`AI_USAGE_ENVIRONMENT`.** A Fly secret exists. KRI-1142 recorded it as `production` (2026-11-02); confirm it before the flip. In production a planning reservation needs a creator. The planner passes `creator_id`, so purpose becomes `customer`.
- **`AI_COST_CONTROL_ENABLED`.** A Fly secret exists, value unread. Planning is enforced either way. When it is on, Gemini planning is reserved too, so `AI_PRODUCTION_MONTHLY_BUDGET_USD` must hold Gemini plus Sol planning.
- **Internal accounts (KRI-1146).** In production an account with an active `internal_account_grants` row is refused paid calls unless tagged as a release canary. A Sol refusal now falls back to Gemini, which answers only when `AI_COST_CONTROL_ENABLED` is off (otherwise Gemini is refused the same way, as today). Use a non-internal account for the KRI-1390 smoke turn.
- **Budgets.** `AI_OPENAI_MONTHLY_BUDGET_USD` exists and must be raised by the planning budget. Each Sol call reserves about $0.21 (max seen $0.23) until it settles 10 to 30 s later. See the cost row under Live eval for the settled cost.

## Rollout (KRI-1390, after merge and deploy; the lead runs it)

Do not run this until the quality gate above is met. The mechanics are ready. After the prompt work, Sol is one fixture under Gemini (95 against 96 of 118), inside run-to-run noise. Whether that is enough, or a paired rerun comes first, is the lead's call.

1. Before: record the planning-turn p95 from `/admin/kria/trace`. Confirm (operator-approved read) `AI_USAGE_ENVIRONMENT=production` and the `AI_COST_CONTROL_ENABLED` value. Make sure the smoke account has no active `internal_account_grants` row.
2. Size the planning budget: `P` = expected Sol planning calls per month x $0.05 (p95 settled $0.052 per call here) plus headroom. Each in-flight call also holds about $0.23 until it settles.
3. Set the secrets (API and worker read them at import):

   ```bash
   fly secrets set --app nova-video \
     AI_OPENAI_PLANNING_BUDGET_USD=<P> \
     AI_OPENAI_MONTHLY_BUDGET_USD=<current + P> \
     MAIN_CREATOR_MODEL=gpt-6.1-sol \
     MAIN_CREATOR_THINKING_LEVEL=medium
   # Only if AI_COST_CONTROL_ENABLED=true (Gemini is reserved too):
   fly secrets set --app nova-video AI_PRODUCTION_MONTHLY_BUDGET_USD=<current + P>
   fly status --app nova-video            # api and worker machine ids
   fly machine restart <api-machine-id> <worker-machine-id> --app nova-video
   ```

4. Smoke: one real creation and one follow-up on a non-internal account. Expect `agent_run agent=nova.creator.main model=gpt-6.1-sol outcome=ok`, a settled `planning` row in `ai_cost_reservations`, and a thinking row from `GET /creation-threads/{id}/thought-summaries`.
5. Watch `runtime_turn_failed`, `agent_provider_fallback` / `outcome=ok_fallback`, `agent_deadline_degraded`, and planning p95.

Rollback (no code change; Gemini is the default again):

```bash
fly secrets unset --app nova-video MAIN_CREATOR_MODEL MAIN_CREATOR_THINKING_LEVEL
fly machine restart <api-machine-id> <worker-machine-id> --app nova-video
```

The planning budget secrets can stay; with a Gemini Main Creator they meter nothing.

## Gaps

- **Quality.** After the prompt work, Sol at `medium` passes 95 of 118 and Gemini at `high` passes 96 (see Prompt work for Sol). The bar is not met in this one sample. Gemini's full rate on the new prompt is not measured; it held 32 of 33 affected fixtures, and the one miss passed 3 of 3 reruns.
- **Thinking row content.** The row fills, but Sol's `concise` summary is usually one heading. Not checked on the iOS simulator.
- **Message route.** The thinking-row check bound the publisher directly. A full `POST /creation-threads/{id}/messages` turn with uploaded footage was not run locally.
- **Local development.** Planning is always metered, so a local stack with `MAIN_CREATOR_MODEL=gpt-6.1-sol` needs the manual-QA envelope (`AI_MANUAL_QA_TEST_RUN_ID`, `AI_MANUAL_QA_MAX_COST_USD`, `AI_MANUAL_QA_RESERVATION_APPROVED`) or every Sol reservation is refused. The Gemini fallback then answers only when `AI_COST_CONTROL_ENABLED` is off. The same applies to an offline replay that runs a Sol-configured agent: it now tries a reservation first.
- **Copilot.** `EDIT_COPILOT_MODEL` on Sol would also spend `planning`, but the copilot has no fallback chain yet (KRI-1393, PR G).
- **Gemini ladder edge.** A Gemini Main Creator configured at `medium` (not the `high` default) now steps to `low` on a deadline before its fallback, as Sol does. The production default is unchanged.
- **Evidence scripts.** `scripts/*.py` here have no pregate test lane; the pregate policy is read from `main`, so they show as UNMAPPED in `/verify`.
