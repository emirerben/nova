# Chat copilot on typed tool calls: Gemini, Sol and Sonnet (KRI-797, KRI-1393)

## Recommendation

**Move the copilot to `claude-sonnet-5-5` with `EDIT_COPILOT_OUTPUT_MODE=tools`, after a canary.** On the
request-following set it met as many requirements as the production JSON copilot (84 of 112 each),
with fewer overclaiming replies (8 against 10). It answered in a third of the time (p50 3.1 s
against 10.9 s, p95 7.9 s against 28.2 s) for less than half the cost per turn ($0.032 against
$0.076). It almost always made every call in one response (1.02 model calls per turn).

The evidence does **not** show that Sonnet edits better than Gemini. A rerun of 15 turns moved each
arm by one requirement in 22, so the three-requirement gap between Sonnet and Sol, and Sonnet's tie
with the control, are inside run-to-run noise. The recommendation rests on speed, cost and the
one-response behaviour, where the gaps are several times larger than the noise.

Do not move to Sol yet. It is the cheapest ($0.019 per turn), but it makes one tool call per
response, so a five-op request takes six model calls. It ran out of turn budget on a split-screen
request and met 79 of 112 requirements.

Gemini in tools mode is **not measured** on the full set. Its full-set recording used a schema
shape Gemini cannot fill (below), so those numbers describe that defect, not the model. The fixed
schema met 5 of 5 on the five requests the defect broke, at $0.096 per turn and p50 11.3 s, with a
second call on four of them.

Production is unchanged. No Fly secret or default model was touched. The lead flips the setting.

## The flip, when the lead decides

Set on Fly, api and worker, then restart both:

- `EDIT_COPILOT_MODEL=claude-sonnet-5-5`, `EDIT_COPILOT_THINKING_LEVEL=medium`,
  `EDIT_COPILOT_OUTPUT_MODE=tools`.
- An Anthropic planning budget: `AI_ANTHROPIC_PLANNING_BUDGET_USD` and
  `AI_ANTHROPIC_MONTHLY_BUDGET_USD` above 0. With 0 every Sonnet reservation is refused and each
  turn answers on the Gemini fallback.

A non-Gemini copilot now falls back to `gemini-3.1-pro-preview` inside the same turn (registry
`cross_provider_fallback`); in tools mode the fallback also answers with tool calls. Rollback:
unset the three copilot settings and restart. JSON mode is byte-identical to before this change.

## The numbers

Full set: the 74 authored request-following threads (30 conversation briefs, 44 per-action
threads; 85 copilot turns, 112 requirements). East Run's production capture is not re-recorded.
Every arm is scored by the same offline replay: the recorded answers go back through the real
copilot parser, the chat route's repair loop (`routes._copilot._repaired`) and the editor compiler
(`runner.replay_v1_copilot`).

| Arm | Met | Partial | Unmet | Overclaims | Turn p50 | Turn p95 | Cost per turn | Model calls per turn |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Gemini JSON (production, control) | **84** (75%) | 6 | 22 | 10 | 10.9 s | 28.2 s | $0.076 | 1.00 |
| Sonnet tools | **84** (75%) | 8 | 20 | 8 | **3.1 s** | **7.9 s** | $0.032 | 1.02 |
| Sol tools | 79 (71%) | 6 | 27 | 9 | 5.2 s | 20.9 s | **$0.019** | 2.45 |
| Gemini tools, described nesting (**invalid**) | 74 (66%) | 4 | 34 | 9 | 13.5 s | 43.3 s | $0.100 | 2.20 |

| Arm | Conversation threads (68 req.) | Per-action set (44 req.) |
| --- | ---: | ---: |
| Gemini JSON (control) | 48 met, 9 overclaims | 36 met, 1 overclaim |
| Sonnet tools | 48 met, 8 overclaims | 36 met, 0 overclaims |
| Sol tools | 48 met, 8 overclaims | 31 met, 1 overclaim |

Validity and failure counts, per arm, over the 85 turns:

| Arm | Turns that needed a second call for `reply` | First answers with an op that did not apply | Schema retries | Failed turns |
| --- | ---: | ---: | ---: | ---: |
| Sonnet tools | 1 | 0 | 1 | 0 |
| Sol tools | 69 | 1 | 0 | 0 |
| Gemini tools (invalid) | 52 | 11 | 1 | 0 |
| Gemini JSON (control) | n/a | 0 | 0 | 0 |

- **Turn time** is the copilot's own wall time for the whole turn: the first answer plus every
  reply or repair round. Planner routing, the database and rendering are outside it. The control's
  recordings have one call per turn, so its turn time is that call.
- **Cost** is the settled development-ledger cost of each tools turn, all rounds included. The
  control's recordings carry tokens only; its cost is those tokens at the registry's list price.
- **Overclaims** use the baseline's rule: the turn applied ops, a requirement was missed, and the
  reply has no "not done" marker.

### Run-to-run noise

The same 15 turns (six multi-turn conversation threads and two per-action threads, 22
requirements) were recorded a second time on each arm. The JSON control was re-recorded at this
prompt version, with the repair loop the route now runs.

| Arm | First run met | Second run met | Requirements with the same status |
| --- | ---: | ---: | ---: |
| Gemini JSON | 18 | 17 | 21 of 22 |
| Sonnet tools | 18 | 17 | 21 of 22 |
| Sol tools | 16 | 17 | 21 of 22 |

Each arm moved by one requirement in 22 between runs. Scaled to 112 requirements that is about five
requirements of noise per arm, which covers every quality gap in the full-set table except the
invalid Gemini arm.

Turn times on the subset reran within 10% for Sonnet and Sol (p50 3.0 s then 2.8 s; 5.8 s then 5.6 s).
The control's p50 went from 14.0 s to 11.9 s.

## Input tokens per turn, before and after

Same model, same turns: Gemini's first call took **28,319** input tokens in JSON mode (mean over 85
turns) and **34,251** in tools mode, 21% more, with no cache hits (1,417 cached on average). The
typed schema Gemini needs is larger again (38,847 on the five-turn check). Tools mode moves each
op's definition out of the rules into its tool, but the tool schemas cost more than the prose they
replace.

The other providers bill the static part from cache:

| Arm | First-call input tokens (mean) | Of which cached | All rounds, input tokens per turn |
| --- | ---: | ---: | ---: |
| Gemini JSON (control) | 28,319 | 0 | 28,319 |
| Gemini tools (invalid schema) | 34,251 | 1,417 | 78,006 |
| Sol tools | 30,128 | 24,963 (83%) | 73,594 |
| Sonnet tools | 50,698 | 41,108 (81%) | 51,944 |

Sol's and Sonnet's first calls are mostly cache reads because threads that share an edit's open
families share the tools and system prefix (OpenAI `prompt_cache_key`, Anthropic `cache_control`
on the system block). Offline, over the same 85 inputs, the JSON prompt is a median 110,457
characters; tools mode sends a 82,583-character system block, a 5,224-character user turn and
48,522 characters of tool definitions (46 tools at the median).

Sonnet counts the same request at 16,000 to 20,000 more tokens than the other two, and it still
costs less per turn because 81% of its input is a cache read at $0.10 per million.

## What changed how the models behave

- **Sol makes one tool call per response**, even with `parallel_tool_calls` and an instruction to
  make every call at once. A five-op request is six calls: five ops, then `reply`. The route lets a
  turn take up to 17 rounds (16 ops plus the reply) but starts a round only while a full copilot
  call still fits the turn budget. "Make the split screen last the whole video" took eight rounds,
  reached that limit, and applied nothing.
- **Gemini, with every call forced (`ANY`), refused the typed tool set** once a phone edit's
  ~45 tools passed a schema-size limit; every tool alone was accepted. The first fix described
  nested objects in text, and Gemini, which decodes arguments against the declared schema, then
  sent every nested object empty (`"patch": {}`): "play the third clip at half speed" became
  `patch_slots {selector: {}, patch: {}}` seven times. The shipped fix sends Gemini the typed
  schema under `VALIDATED`, which accepts all 91 tools, and keeps the described shape for OpenAI
  and Anthropic (byte-identical to what they were measured with). Gemini still splits `reply`
  into a second call on most edits.
- **Sonnet** puts every op and `reply` in one response. Forced tool choice is refused with
  thinking on, so it runs on `auto`. It answered in prose once in 85 turns (one schema retry) and
sent `reply` in a second call once.

Differences from the control that look like model judgment, not transport: Sol refused
"side by side" as unsupported because this edit offers top-and-bottom panels, and asked which clip
was uploaded last instead of guessing. Sonnet mislabelled one landmark correction and partially
described two dishes, and fixed a route order and a split-screen section order the control
missed.

## What was run

| Item | Value |
| --- | --- |
| Models and effort | `gemini-3.1-pro-preview` thinking `high`; `gpt-6.1-sol` reasoning `medium`; `claude-sonnet-5-5` effort `medium`. One attempt per model, no fallback, schema retry as in production. |
| Copilot prompt | `2026-11-13-v127+copilot-tools-v1` (tools arms and the JSON rerun). The control is the committed baseline, recorded at `2026-11-11-v122` (five threads at `v126`). Between those versions the JSON prompt only gained op fragments shown where their family is open. |
| Code | Recorded at `ca60466c5`. Sol's four turns that reached the old six-round cap were re-recorded after the cap became 17 (`743ca26e4`). The Gemini fix check ran at the PR head. |
| Turn path | `EditCopilotAgent` in tools mode, then `_repaired` with a 180 s turn budget and the planner's 120 s copilot timeout, then the compiler on a synthetic phone variant. Planner routing and rendering are not in it. |
| Snapshots | Turn 0 of every thread uses the control's exact input. A later turn is asked over the edit this arm's own earlier turns produced. |
| Footage | Synthetic (`tests/fixtures/request_following/footage`). |

The control has no repair rounds because it was recorded before the repair loop existed. The JSON
rerun has them and scored within one requirement of the control on the subset.

## Spend

Development ledger, scratch database, `--usage-purpose live_eval`, `--max-cost-usd 2`,
`--approve-reservation`. Priced first with `compare_tools --price-only` (pessimistic reservations:
Gemini $42.71, Sol $23.21, Sonnet $23.21 for the 85 first answers).

| Runs | Calls | Settled |
| --- | ---: | ---: |
| Smoke (`kri797-smoke*`) | 45 | $1.46 |
| Gemini tools, full set (`kri797-gem-*`, `kri797-gem2-*`) | 189 | $9.07 |
| Sol tools, full set (`kri797-sol-a`, `kri797-sol2-a`) | 244 | $2.07 |
| Sonnet tools, full set (`kri797-son-a`, `-b`) | 87 | $2.73 |
| Noise reruns (`kri797-r2-*`; the Gemini tools rerun stopped once its schema was found invalid) | 106 | $3.18 |
| Gemini typed-schema check (`kri797-gemfix-a`) | 9 | $0.48 |
| **Total** | **680** | **$18.98** |

No run passed $2. Another ~$0.5 (a guess from token sizes) went to direct, unmetered Gemini calls
while bisecting the schema limit: each request that Gemini refused cost nothing, the accepted ones
were 1 to 11k-token prompts. The local development budget ($25, refused past 80%) leaves no room
for a full Gemini tools re-record (about $8); it is the open item below.

## Open

- **Gemini tools on the full set** with the typed schema: about $8 under a fresh budget. Until then
  Gemini tools mode has five turns of evidence.
- **Canary before the flip**: the request-following set is synthetic footage and the copilot path
  only. Production turns add the planner, prompt caches that start cold, and the phone.
- **Sol's one-call-per-response** makes multi-op edits slow; measure Sol again if OpenAI exposes a
  way to batch calls, or give Sol a larger round budget per turn.

## Rerun

```bash
cd src/apps/api
bash ../../../scripts/dev/test-db.sh
python -m tests.evals.request_following.compare_tools --arm sonnet --price-only
# One arm, $2 batches until done (budgets for OpenAI/Anthropic planning are set by the script).
bash ../../../docs/reviews/creator-model-comparison/scripts/run_tools_arm.sh \
  sonnet "$PWD/../../../.dev/kri797" kri797-son
# A rerun of a subset for noise: TAG and ONLY.
bash ../../../docs/reviews/creator-model-comparison/scripts/run_tools_arm.sh \
  sonnet "$PWD/../../../.dev/kri797" kri797-r2-sonnet r2 food_fonts_then_forbid,harbor_landmark_correction
# Score every recorded arm and the committed JSON control (offline).
python -m tests.evals.request_following.compare_tools --score --out ../../../.dev/kri797
```

Recordings stay under `.dev/kri797/` (not committed: each arm copies the threads' snapshots). The
scored numbers above are [copilot-tools-results.json](copilot-tools-results.json), per thread and
requirement.
