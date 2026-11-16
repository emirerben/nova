# KRI-1375 replay proof

Evidence for `make kria-replay-job` with live models. Every run reads the job and item debug
with `scripts/admin.py --prod GET`, copies the footage the job's brief binding recorded
read-only from the bucket, and runs the Main Creator route and the real worker locally under
`src/apps/api/tests/prod_flags.snapshot.json`.

Commit ids in the sections before pass 7 name the commits as they were then. The branch was
rebased since (2026-11-16, onto main 1700e3f77); a commit's subject finds its rebased copy.

## Pass 7: how often the proof job passes, and on which Main Creator model

What this command shows is that a replay is faithful and reports honestly. It does not show
that a production failure reproduces: six of the chat jobs replayed so far diverged before
their failing render, and 1cb33402 never failed as production did (see the runbook's "Fidelity
limits").

The round-2 verifier reran the proof job, 1cb33402, on the same code and model as run 13 and got
a failure where run 13 got a device plan. One success was being read as proof. This pass:

* Adds `--main-creator` (`MAIN_CREATOR=` in make). `production`, the default, plans on the model
  production planned the job on; `snapshot` on today's setting. The output names the model and
  why (`main_creator`).
* Takes a chat job's turn from the thread's `render_queued` event when the job debug has no
  `kria_turn_id`.
* Treats an empty planning-error message (a bare `assert`) as no cause.
* Has the PR template ask for the number of replays and what each produced.

### 1cb33402, every replay on code that has the route checks

Production (2026-10-25, Gemini 3.1 Pro Main Creator): message 1 got an approval card, and its
render failed `phone_plan_unsupported` with `CreatorRenderContractError` "This edit is missing
confirmed on-screen text."

| Run (UTC, 2026-11-16) | Main Creator | Outcome | Route | Settled |
| --- | --- | --- | --- | --- |
| 13, 07:39 | `gpt-6.1-sol` (snapshot) | `device_plan_ready` | production's agents (`nova.creator.main`, `nova.plan.speech_excerpt_planner`) | $0.0866 |
| verifier, 08:04 | `gpt-6.1-sol` (snapshot) | `failed`, `phone_plan_unsupported`, `SpeechMontageClarification` ("I couldn't work out which of your lines to play just now..."), `reproduction_unproven` | production's agents; the speech excerpt planner ended `terminal_schema` | $0.0874 (worker log) |
| 10:26 | `gemini-3.1-pro-preview` (production) | `spend_cap_reached` after message 1's Main Creator call (cap 0.20) | not reached | $0.2050 |
| 10:29 | `gemini-3.1-pro-preview` (production) | `device_plan_ready` | production's agents; `model_differences` empty | $0.1958 |
| 10:31 | `gpt-6.1-sol` (snapshot) | `device_plan_ready` | another route: `nova.edit.copilot` wrote a `guided_story` title, no speech excerpt planner | $0.1258 |
| 10:33 | `gpt-6.1-sol` (snapshot) | `device_plan_ready` | the same other route as 10:31 | $0.1147 |

Pass rate on today's code: on the snapshot's Sol Main Creator, 3 of 4 runs reached a device
plan (one of them on production's route) and 1 failed. On production's Gemini Main Creator, 1 of
1 completed run reached a device plan; one more stopped at the spend cap before its render.
Every run that got a reply to message 1 got an approval card, as production did.

What this does and does not show:

* On production's model and production's agents, today's code got past the failure once. One
  run is a sample. The prompt is today's (`nova.creator.main` prompt `mc-5bf660a3e6b8`, production
  `2026-10-25-v94`), so this does not say which change fixed it.
* The failure did not reproduce in any run, so no run here is a `reproduced` result.
* On Sol, the result varies between runs: a device plan on production's route, a failure in the
  speech excerpt planner, and a device plan on another route.
* A Gemini 3.1 Pro Main Creator call settled $0.205 (76 s) against Sol's $0.07, so a
  `--main-creator production` replay of an older job needs a cap above 0.20.

Commands: `make kria-replay-job JOB=1cb33402-12ee-4510-98b1-6c5c7558d23e MAX_COST_USD=0.30`
(production model) and the same with `MAIN_CREATOR=snapshot`, code at 4e486fb64 (the runs used its
pre-rebase copy 0f423658f, the same code; later commits change docs only).

### The 11 chat jobs without a turn id (no model calls)

`replay_job.py <id> --fetch-only` on the 11 jobs the verifier listed (prod GET and storage reads
only). Each took its turn from the thread's `render_queued` event. Eight reach the replay, and
`recorded_turns` accepts each recovered turn (approved, and it queued that job): 0c9f0a82 (message 7 of
9), 150d30b4 (8 of 9), 1b2ce28c (3 of 4), 1e2d4937 (1 of 15), 3afe19cd (1 of 4), 8e3986c5 (4 of
5), e4398b51 (5 of 5), be180d9f (9 of 9). Three are refused later on an older limit: 7b14c641,
38caaebd and ef8e35b8 each have an id-less binding clip that two item clips of the same length
match equally. 1e2d4937 and 3afe19cd render on message 1, so they are the best candidates for a
future reproduction.

### Offline proof (no model calls)

`tests/scripts/test_replay_job.py`, committed red in 07d27df92 before the fix (6 failing):

* `test_a_replay_plans_on_the_main_creator_model_production_ran_for_the_job`: production's model
  comes from its last non-fallback run before the job ended, the thinking level is the code
  default, replay_creator's and the worker's environments both carry it, the committed snapshot
  is untouched, and `snapshot` still gives Sol at `medium`.
* `test_a_chat_job_without_a_recorded_turn_id_replays_up_to_the_turn_that_queued_it` and
  `test_a_chat_job_whose_queuing_turn_is_unknown_is_refused_naming_the_turn` (7b14c641's thread
  shape).
* Two bare-assert cases in `test_only_the_cause_production_recorded_makes_a_replay_its_failure`.

## Pass 6, round 2: only production's exact recorded cause is a reproduction

The round-2 verifier probed the round-1 classifier with real error classes and messages from 25
production job debugs. Seven shapes read `reproduction: reproduced` with no gap:

* P1, P2. One error class, another message. `CreatorRenderContractError` is 51f7124f's "This text
  cannot fit clearly in the requested area" and 40684a25's "The eligible fonts cannot draw all
  text". `CreationTextCompositionError` is every `creation_text_failed` job (3c312af4 "incomplete:
  Since the English translation...", e7bf7099 "could not compile: Text changed...").
* P3. A production job from before planning errors were recorded (0701fcc9, 9734ed83, 1e79ff4f)
  was compared on its reason alone, though its `error_detail` names the cause.
* P4, P5. The replay's failing agent is one production's trace cannot show: not stored yet when
  the job ran, or run on the Creator session.
* P6. Production recorded a planning error and the replay none.
* P7. The agent failed only in an earlier production job of the item; production's outcomes were
  any run's, the replay's its last run's.

Root cause: the comparison listed known differences and called everything else a match. Every
field it checked (the reason, the error class) is a category many failures share, and evidence
it could not check counted as agreement. The fix turns that around:

* The cause must match exactly. With a production planning error, the replay must record the
  same class and the same message. Without one, the replay's `error_detail` must equal
  production's, unless production's is only the app's generic sentence for the reason
  (`generic_error_detail`, which `replay_creator` now writes into the receipt) or missing. Then
  the verdict is `reproduction_unproven`.
* An agent production's trace cannot show whose last replay run failed is listed in
  `failed_uncompared_agents` and is a gap.
* Production's side uses each agent's last run, like the replay's.
* A `diverged_from_production` explanation names production's own failure and says production
  may have stopped before those agents (round-2 minor finding on 1e79ff4f).

### The verifier's probes, before and after (no model calls)

`probe_classify.py` from the verifier, adapted only to pass production's error detail (P3 uses
0701fcc9's):

```
== previous head 76cdb2a85
P1 creation_text same class different cause: outcome=failed reproduction=reproduced reproduced=True
P2 render contract same class different gate: outcome=failed reproduction=reproduced reproduced=True
P3 no prod planning event, different error_detail: outcome=failed reproduction=reproduced reproduced=True
P4 failure in unrecorded agent: outcome=failed reproduction=reproduced reproduced=True
P5 failure in non-comparable agent: outcome=failed reproduction=reproduced reproduced=True
P6 replay recorded no planning error: outcome=failed reproduction=reproduced reproduced=True
P7 failure only in an earlier prod job: outcome=failed reproduction=reproduced reproduced=True
== fixed head
P1 creation_text same class different cause: outcome=failed reproduction=reproduction_unproven reproduced=False
P2 render contract same class different gate: outcome=failed reproduction=reproduction_unproven reproduced=False
P3 no prod planning event, different error_detail: outcome=failed reproduction=reproduction_unproven reproduced=False
P4 failure in unrecorded agent: outcome=failed reproduction=reproduction_unproven reproduced=False
P5 failure in non-comparable agent: outcome=failed reproduction=reproduction_unproven reproduced=False
P6 replay recorded no planning error: outcome=failed reproduction=reproduction_unproven reproduced=False
P7 failure only in an earlier prod job: outcome=failed reproduction=reproduction_unproven reproduced=False
```

Each fixed probe's gaps name its own cause. For example, P2: "Production's plan failed with
CreatorRenderContractError ('This text cannot fit clearly in the requested area. Choose another
placement or shorter text.'); the replay's with CreatorRenderContractError ('The eligible fonts
cannot draw all text. Choose another font.')." P4 also gets "nova.plan.clip_intent_planner failed
in the replay (terminal_schema), and production's trace for this item cannot show it...".

### Run 13: 1cb33402 on the fixed code, `device_plan_ready`

Command: `make kria-replay-job JOB=1cb33402-12ee-4510-98b1-6c5c7558d23e MAX_COST_USD=0.45`, code
commit 817278276, 2026-11-16 07:39 UTC. Driver exit 0. Settled spend $0.0866 over four agent
runs.

* Production (2026-10-25): message 1's render failed `phone_plan_unsupported`, planning error
  `CreatorRenderContractError` "This edit is missing confirmed on-screen text." The creator's
  second message ("Add it") then rendered.
* The replay seeded all 8 recorded sources (`bound_to_recorded_media: true`). Message 1 got an
  approval card, as in production (`matches_production: true`). The approved render, job
  5cff0c02, ended `awaiting_device` with a complete pinned device plan (`device_plan_ready`).
* Route: `replay_agents` and `production_agents` are both `nova.creator.main` and
  `nova.plan.speech_excerpt_planner`; `replay_only_agents`, `failed_only_in_replay` and
  `failed_uncompared_agents` are empty. The clip-intent planner and resolver are uncompared
  (production did not store them before 2026-10-31).
* `model_differences`: the Main Creator ran on `gpt-6.1-sol`, production's on
  `gemini-3.1-pro-preview`.
* The receipt's job carries `generic_error_detail` (null, as the job did not fail).

This is one sample, not proof that the plan now reaches the phone. The verifier's rerun on the
same code and model (20261116T080433Z) ended `failed` (`phone_plan_unsupported`,
`nova.plan.speech_excerpt_planner` `terminal_schema`, `SpeechMontageClarification`). Pass 7
below reports the pass rate over more runs. The other model is a named difference as well.

### Run 14: 07efa44c, diverges at message 1

`make kria-replay-job JOB=07efa44c-051f-43c2-99d9-ed567408034e MAX_COST_USD=0.40`, same commit,
07:41 UTC. Driver exit 1, settled $0.0857 over six agent runs. Production's message 1 got the
`simplification_requires_choice` question; the replay's got
`ask_user:unsupported_audio_visual_combination`, so the creator's tapped answer did not fit and
the replay stopped (`diverged_from_production`, `first_divergent_turn.message_index: 1`). It
never reached message 2's render, whose text-fit failure was the target. Main Creator on Sol
again, production's on Gemini 3.1 Pro.

Paid calls this round: $0.1723 (runs 13 and 14). Scratch databases dropped, no replay process
left, and no Redis key of either run's namespace remains.

### Live runs re-classified on the fixed code (no model calls)

The saved receipt and `route_comparison` of two live runs, classified again:

* 1e79ff4f, the verifier's run 20261116T071547Z: still `diverged_from_production`,
  `failure_reason_differs`. The explanation now reads "...Production's job failed with
  phone_plan_failed: 'gemini provider outcome unknown after 90.0s' before running them or on
  another route... If production's failure is a timeout or a provider error, production stopped
  earlier than the replay; otherwise check the flag snapshot...".
* 1be34fc8 run 12 (20261116T065633Z): still `reproduction_unproven` with the same two gaps.

### Offline proof (no model calls)

`tests/scripts/test_replay_job.py`, committed red in 1ee659bb7 before the fix (12 failing):

* `test_only_the_cause_production_recorded_makes_a_replay_its_failure`: nine pairs of real
  production causes. Another message under one class, a planning error on one side only, another
  error detail, a generic detail and no detail are `reproduction_unproven`, and the gaps print
  both messages. The same text-fit gate twice (07efa44c, 51f7124f) and 0701fcc9's refusal again
  (as f88555bc's) stay `reproduced`.
* `test_a_failure_production_cannot_show_is_not_production_failure`: P4 and P5 are unproven; a
  session-scoped agent that failed and then got through does not block a reproduction.
* `test_an_agent_that_failed_only_in_an_earlier_production_job_still_failed_only_in_replay`: P7.
* `test_a_session_scoped_agent_failure_is_not_called_a_route_divergence` (renamed): such a
  failure is still never a divergence, and is now unproven instead of reproduced.

The four Postgres replay test files (11 tests) pass on a scratch `_test` database migrated to
head.

## Pass 6 fix: a matching failure reason is not a reproduction (1be34fc8)

The pass-6 verifier replayed 1be34fc8 (the "10 November" Turkish and English text, production
2026-11-10, `phone_plan_failed`). Production's eight agent runs all ended `ok`, and its plan failed
with `ResearchUnavailable` ("This explanation needs a longer clip or shorter wording") after the
last caption binding. The replay took the `nova.research.text_layers` route and failed its second
`nova.research.public_facts` search (`terminal_schema`). It still printed
`production_failure_reproduced: true`, because the two reasons were the same catch-all string.

### Run 12: 1be34fc8 on the fixed code, `reproduction_unproven`

Command: `make kria-replay-job JOB=1be34fc8-6dd8-4d1c-878b-368a5ad45656 MAX_COST_USD=0.45`, code
commit 47a6e8856, 2026-11-16 06:56 UTC. Driver exit 1 (outcome `failed`). Settled spend $0.2153
over six agent runs.

* Message 1 got an approval card, as in production (`matches_production: true`). The approved
  render, job 8be64fa3, ended `processing_failed` with `phone_plan_failed`.
* This time the replay took production's route: `replay_only_agents: []`. It never reached the
  caption binding. Its first `nova.research.public_facts` search ended `terminal_schema`, on
  `gemini-2.5-flash` like production's.
* `planning_failure` (the replay job's last `phone_plan_failed` event): `TerminalSchemaError`,
  "nova.research.public_facts: schema — Search produced no usable cited evidence".
  `compared_production_render.planning_failure`: `ResearchUnavailable`, "This explanation needs a
  longer clip or shorter wording".
* Verdict: `reproduction: reproduction_unproven`, `production_failure_reproduced: false`. The
  gaps name the agent and both errors:
  * "nova.research.public_facts failed in the replay (terminal_schema), but every run of it
    production recorded ended ok."
  * "Production's plan failed with ResearchUnavailable (...); the replay's with
    TerminalSchemaError (...)."
* `model_differences`: the Main Creator ran on `gpt-6.1-sol`, production's on
  `gemini-3.1-pro-preview` (the snapshot's current Main Creator model).

The same output, classified by the code before the fix (540326a61), reads
`production_failure_reproduced: true`:

```
pre-fix 540326a61 {"outcome": "failed", "failure_reason": "phone_plan_failed", "production_failure_reproduced": true, "reproduction": null}
fixed {"outcome": "failed", "failure_reason": "phone_plan_failed", "production_failure_reproduced": false, "reproduction": "reproduction_unproven"}
```

Afterwards no scratch database, no Redis key of the run's namespace and no replay process was
left.

What this says about the job, not the tool: on the current code, 1be34fc8's public-facts search
failed in both live replays (the verifier's and run 12), where production's two searches passed.
That is outside KRI-1375.

### Offline proof (no model calls)

`tests/scripts/test_replay_job.py`, committed red in 540326a61 before the fix:

* `test_a_matching_failure_reason_is_production_failure_only_on_production_route`, built from
  1be34fc8's production runs and planning error, through `production_agents`, `summarize`,
  `compare_routes` and `classify_failure`. The verifier's replay is
  `failure_reason_matched_route_differs` and names text_layers, public_facts and both error
  classes. The same route with a research failure production did not have, another planning
  error class, or a cut-off trace is `reproduction_unproven`. The same route and error, a
  research failure in both, or a production job with no planning error stay `reproduced`.
* `test_a_failure_inside_an_agent_production_never_ran_is_not_reproduced`: a divergence no
  longer carries `production_failure_reproduced: true`.

## Pass 5, round 2: e7bf7099 on the current code

Paid calls this round settled $0.2749 in total, from the scratch databases' agent runs and one
worker log: run 9 $0.1050, the aborted run $0.0471, run 11 $0.1228. All three ran live on
2026-11-15 with `gpt-6.1-sol` as the snapshot's Main Creator.

### Run 11: e7bf7099 on the final code, `device_plan_ready`

Command: `make kria-replay-job JOB=e7bf7099-7006-4d3f-9f05-c8daeb83546d MAX_COST_USD=0.27`, code
commit 0c27a352d (later commits change docs only), 17:27 UTC. Exit 0.

The source job (production, 2026-11-14 07:04) ended `processing_failed` with
`creation_text_failed`. Its chat has three creator messages: (1) find the lyrics and add them as
text, (2) the lyrics themselves, approved, whose render (d6c58d7b) failed `creation_text_failed`,
and (3) "Fix it", approved, whose render is e7bf7099 and failed the same way.

* Message 1, client protocol 49: the replay answered in words ("Could you paste the lines you
  want added to the video?"), where production asked with a choice card
  (`ask_user:lyrics_text_needed`). `first_divergent_turn` names message 1. Message 2 carries no
  structured answer, so it was sent.
* Message 2: an approval card, as in production (`matches_production: true`). The replay approved
  it under protocol 49. Job c1214872 ended `awaiting_device` with one pinned `guided_story`
  variant (4.0 s), whose text layers hold the creator's lyric lines ("Vazgeçmem dünyalatı" ...
  "Seninle Fenerbahçe").
* Production's render of message 2 failed and the replay's did not, so the replay stopped there
  (`stopped_early`, `compared_production_render` d6c58d7b). "Fix it" answered a failure that no
  longer happens.
* `route_comparison`: no replay-only agent, no failed agent, `diverged_failures: []`,
  `replay_uncompared_agents: []`. Production's trace for this item lists the clip-intent agents
  (the job ran after #1973), so they were compared.
* `model_differences`: the Main Creator ran on `gpt-6.1-sol` for message 1 and on
  `gemini-3.1-pro-preview` for message 2. The ledger refused message 2's Sol reservation under
  the 0.27 cap (`AiBudgetExceededError`, then `agent_provider_fallback`). The cap was set to keep
  this round's paid calls under $0.35. The runbook now says how much cap two Sol messages need.

Afterwards no scratch database, no Redis key of the run's namespace and no replay process was
left.

### Run 9: the same job on 6d932aaa9, no model difference

Command: the same with `MAX_COST_USD=0.30`, 17:15 UTC, exit 0, `device_plan_ready`. Both Main
Creator calls ran on `gpt-6.1-sol` and `model_differences` was empty. Message 1 got a choice
card of another kind (`ask_user:lyrics_alternative`: paste the lyrics or have an original chant
written). Message 2 matched production's approval, and job de98768f ended `awaiting_device`
with the same pinned 4.0 s `guided_story` plan and lyric lines. The code after 6d932aaa9 changes
only two things. The route comparison of jobs from before 2026-10-31 (e7bf7099's production
trace is complete for the agents involved, so `unrecorded` is empty for it, read with the final
code). And what happens to `replay_creator` when the driver is stopped.

### The aborted run: a stopped driver left its replay running

A run started with `MAX_COST_USD=0.24` was stopped by SIGTERM at 17:24 UTC, because that cap is
under a Sol reservation. The driver dropped its database and stopped its worker, but the new
`run_under_spend_cap` loop had not killed `replay_creator`. The orphan sent message 1, and the
worker billed one Sol call ($0.0471) before it shut down. `subprocess.run`, which the loop
replaced, kills its child on any exception. `test_a_stopped_driver_takes_its_replay_down_with_it`
was committed red in 37adfa30e, and the fix is in 0c27a352d.

### 19f61092's route comparison, recomputed on production data (no model call)

`production_agents` on the final code, reading production's trace with GETs only: the job's
thread began 2026-10-19 16:01 UTC, production's trace lists `nova.creator.main` and
`nova.edit.copilot` only, and `unrecorded` holds `nova.audio.music_matcher`,
`nova.plan.clip_intent_planner`, `nova.plan.clip_request_resolver` and
`nova.video.clip_question`. Fed the agents the verifier's replay of message 1 ran (Main Creator,
clip-intent planner, resolver), `compare_routes` now gives `replay_only_agents: []` and lists
the planner and resolver in `replay_uncompared_agents`. Before this round it named both as
agents production never ran. They were not stored in production before #1973 deployed
(2026-10-31 10:21 UTC); today's replay stores them on the item.

The verifier's other finding on that job stands as data, not as a defect: today's code answers
message 1 with a clip question where production asked a simplification choice, so the creator's
tap has nothing to answer. The runbook's Fidelity limits now say why the replay does not answer
for the creator, and that such a job cannot carry a fix PR's proof.

### Offline proofs this round (no model calls)

Committed red before each fix (bfa9b3a87, 993dcd905, 37adfa30e):

* `tests/scripts/test_replay_route_scope_postgres.py`: on real agent-run rows, a run linked to
  the item or to its current job is compared. A session-only run or a run of an earlier render is
  not, and spend still counts all four.
* `tests/scripts/test_replay_job.py`:
  * A session-scoped `clip_request_resolver` failure leaves a reproduced failure `failed` with
    `production_failure_reproduced: true`.
  * An agent production did not store yet proves no divergence before the deploy and does after
    it.
  * A job with no binding keeps the pool photo created before it and leaves out the later one
    (`clips_not_dated` counts the clip rows).
  * The spend watchdog interrupts the run past the cap, survives a failed spend read, and
    reports `spend_cap_reached`.
  * A stopped driver kills its replay.

## Pass 5: f88555bc and 50f67a06 after the input fixes

Both runs used code commit dd7ac30ba (the commits after it change docs only), on 2026-11-15,
live. Settled cost across both was $0.1515, from the scratch databases' agent runs. A spend
watchdog was set to stop each run before the round's $0.35 cap.

### Run 7: f88555bc, the clip now binds as a phone source

Command: `make kria-replay-job JOB=f88555bc-ddd3-4320-b078-c58f17a275be MAX_COST_USD=0.35`
(16:25 UTC). Cost: $0.0595, one `nova.creator.main` call on `gpt-6.1-sol`.

The source job (production, 2026-10-21) ended `processing_failed` with `phone_plan_unsupported`.
It had one analysis-proxy clip and four creator messages, two of them choice taps. Its speech
cleanup is `required_v1`, so the admin debug served its binding row with no media id and no
`manifest_identity`.

Before this round, the verifier's live run on the same job stopped at message 1 with zero agent
runs. The seeded clip did not bind, and the runtime answered "Add at least one clip and I can shape
the edit around what is actually there." The driver reported that as `diverged_from_production`.

What happened now:

* The matched row got `manifest_identity` equal to its media id. The seed's
  `bind_phone_sources` check passed, and the worker log has no "Add at least one clip" and no
  `unverified_phone_sources`.
* Message 1 reached a draft and an approval card (`draft_applied`, `approval_requested`). The
  plan cites the take's own timecodes: "from “Hi, I'm Karolina, I'm from Ecuador” (0:12) to right
  after “I'm gonna be here all week” (0:50)".
* Production asked a choice question at message 1 (`order_basis`: `attachment_order` or
  `unordered`). So `first_divergent_turn` names message 1 (`choice_question:order_basis` against
  `approval`). Message 2's tap had no question to answer and was not sent.

Result: `outcome: diverged_from_production`, exit 1. `input_differences.models` names the Main
Creator: production used `gemini-3.1-pro-preview` for this item, and the replay used `gpt-6.1-sol`,
which is the snapshot's model since production switched on 2026-11-14. This run says nothing
about the production failure. It shows the replay now plans on the job's footage.

### Run 8: 50f67a06, the pool photo the old code dropped is replayed

Command: `make kria-replay-job JOB=50f67a06-2c78-4691-831c-5754e3bf9f74 MAX_COST_USD=0.29`
(16:27 UTC). Cost: $0.0920 over six agent runs: `nova.creator.main` on Sol,
`nova.plan.clip_intent_planner`, `nova.plan.clip_request_resolver`, three
`nova.video.clip_question` calls on `gemini-2.5-flash`, and the live analysis of the pool photo.

The source job (production, 2026-10-21) ended `processing_failed` with `phone_plan_unsupported`.
Its binding has no `visuals` key. Its own plan names pool photo `asset-6ae61e1a-…`: two brief
scopes `clip:asset-6ae61e1a-…`, an `exact_texts` render requirement, and two resolved clip
intents. Before this round `--fetch-only` copied only the video (`pool_images: 0`) and still said
`bound_to_recorded_media: true`.

What happened now:

* Coverage was `replayed_visuals_and_clips: 2`, `pool_images: 1`, `pool_visuals_recorded: false`,
  `pool_visuals_inferred: 1`, `pool_visuals_added_after_job: 0`, and
  `input_differences.pool_visuals_not_recorded: 1`. The photo was created at 12:52, before the job
  at 13:05. It was seeded as the replay's `asset-9726a560-…` and analysed there.
* Message 1 reached an approval card: six moments, the longest 2.0 s, plus "Couldn't: Freeze the
  frame for one second on Atakan's bite". Production asked a clip question
  (`order:his-thumbs-up`) instead. Message 2's clip tap had no clip question to answer and was not
  sent.

Result: `outcome: diverged_from_production`, exit 1, with the same Main Creator model difference
(`gemini-3.1-pro-preview` in production, `gpt-6.1-sol` here).

### 4620fd7c is refused before any model call

`--fetch-only` stops with "Pool image 2aa3ede6-… has no storage path in the item debug and is not
under users/…/plan/81e6999d-…/pool/; it is a media-library asset and this production API predates
the gcs_path field". This is the known precondition below. It needs this PR's
`admin_plan_items.py` change deployed.

### Offline proofs this round (no model calls)

These tests were committed red in 22906029e, before the fix in dd7ac30ba:

* `tests/scripts/test_replay_seeded_sources_postgres.py` runs the real `collect_inputs`, seed and
  `resolve_item_creator_context` on Postgres. It uses f88555bc's shape (redacted row,
  `required_v1`, `native_device_only`, no client protocol on the messages).
  * Red: `dispatch_render` came back `unverified_phone_sources`, the verifier's offline probe.
  * Green: `phone_source_audio` is available, and the thread carries the native-only device
    intent.
  * A seeded row that does not bind raises `ReplayInputError`. A cloud clip with no receipt still
    seeds.
* `tests/scripts/test_replay_job.py`:
  * With no `visuals` key, a pool photo created before the job is kept and one added after is left
    out.
  * A pool asset the job's plan names but the item no longer holds refuses the run.
  * A receipt marked `input_error` prints `input_error`.
  * A message whose answer could not be sent is named in `first_divergent_turn`.
* `tests/scripts/test_replay_recorded_answers_postgres.py` uses 40684a25's shape on the real
  `submit_turn`. The replay's clip question asks only `order:the-obelisco`, so the tapped
  `exclude:anything-too-short-to-use` is left out and recorded in `not_asked_by_replay`. Before
  the fix the whole answer was refused.

## Run 6 (pass 3, round 2): e7bf7099 on production's Main Creator model

Command: `make kria-replay-job JOB=e7bf7099-7006-4d3f-9f05-c8daeb83546d MAX_COST_USD=0.35`
(2026-11-14, 15:10 UTC, live, settled cost $0.1062 from the scratch database's agent runs:
`nova.creator.main` on `gpt-6.1-sol` $0.0473 and $0.0533, `nova.plan.clip_intent_planner` and
`nova.plan.clip_request_resolver` on `gemini-2.5-flash` $0.0056 together). Same source job as run 5.

What changed since run 5: the snapshot records production's Main Creator model and effort
(`gpt-6.1-sol`, `medium`; `job-data:main-creator-sol-2026-11-14`), and the replay runs its
paid-call ledger as a capped development manual-QA run, so a local Sol call is reserved instead of
refused. Before the run the driver printed production's models for the item:
`nova.creator.main` `gpt-6.1-sol`, the rest `gemini-2.5-flash` (`nova.edit.copilot`
`gemini-3.1-pro-preview`). `route_comparison.model_differences` is empty: every agent the
replay ran used the model production used for this item. The only difference is the Main
Creator's prompt version (`mc-edead08c6c40` in production, `mc-53ad0915085a` here: the prompt
changed since the job).

What the replay did (`turns`):

* Message 1 under client protocol 49: the reply asked for the lyrics in words ("Could you paste
  the lines you want on the video?"). Production asked with a choice card
  (`ask_user:lyrics_text_needed`), so `first_divergent_turn` names message 1
  (`production_reply` `choice_question:ask_user:lyrics_text_needed`, `replay_reply` `reply`).
  Run 5, on Gemini, went to `nova.research.public_facts` here instead.
* Message 2 (the lyrics, no structured answer): `approval`, as in production
  (`matches_production: true`). The replay approved it under protocol 49; job 4ed9173b ended
  `awaiting_device` with `creator_client_protocol: 49` and one pinned `guided_story` variant
  (4.0 s) whose text elements carry the lyric lines ("Vazgeçmem dünyalatı" ... "Seninle
  Fenerbahçe").
* The replay stopped there (`stopped_early`): production's render of message 2 (d6c58d7b) failed
  `creation_text_failed`, the replay's did not, so "Fix it" (e7bf7099's own message) answered a
  failure that no longer happens.

Result: `outcome: device_plan_ready`, exit 0, on production's model. This is the run that the
run-5 write-up predicted and did not make. The worker reported its prewarm after 5 s
(`worker_warm_s`); no reconcile sweep was due in the 50 s the turns took. Afterwards the scratch
database was gone and no Redis key of the run's namespace remained. No phone rendered the plan.

## Offline proofs this round (no model calls)

* Recorded taps, against the real runtime on Postgres
  (`tests/scripts/test_replay_recorded_answers_postgres.py`, shapes from production jobs
  07efa44c and 98eac78f): a tapped `simpler_version` reaches the replay's own
  `simplification_requires_choice` card; a card of another kind is reported as a divergence with
  both questions and nothing is sent; a tapped `asset-<id>` pool clip is sent as the replay's
  asset; "you choose" is derived again against the replay's card. Red control: the branch head
  before this round sent production's question id to the same seeded card and the runtime
  refused it (`choice_selection_stale`).
* Keyless end to end, `07efa44c-051f-43c2-99d9-ed567408034e` (every model key removed, so no
  provider accepted a call): message 1 got no plan, and message 2's tapped `simpler_version`
  was not sent. Outcome `diverged_from_production` with
  `production_question.conflict: simplification_requires_choice`, `replay_question: null`, and
  `first_divergent_turn` naming message 1. `input_differences.media_library_backed_media: 5`.
* Lapsed lease (`tests/scripts/test_replay_lease_postgres.py`): the replay's sweep republishes a
  `planning` turn whose lease lapsed, the way production's beat does, and a timeout on such a
  turn ends `stalled` with `lease_lapsed: true`, never `diverged_from_production`.
* Ledger envelope: with the replay's environment, a `gpt-6.1-sol` planning reservation in a
  migrated scratch database is accepted; without it the ledger refuses it
  (`paid_test_attribution_required`), and with a cap of $0.10 it refuses it
  (`estimated_cost_exceeds_approval`).

## Run 5 (pass 3): e7bf7099, the creator's three messages, turn by turn

Command: `make kria-replay-job JOB=e7bf7099-7006-4d3f-9f05-c8daeb83546d` (2026-11-14, 13:27 UTC,
live Gemini, settled cost $0.2157 from the scratch database's agent runs: Main Creator $0.077 and
$0.093, `nova.research.public_facts` $0.037, three flash agents $0.008).

Source job (production, 2026-11-14 07:04): `processing_failed`, `creation_text_failed` ("I
couldn't prepare the text while keeping your approved wording"), one clip, a runtime-v2 chat with
three creator messages: (1) "Find the lyrics to the song ... and add it as text to this video",
answered by a question; (2) the lyrics themselves, approved, whose render (job d6c58d7b) failed
`creation_text_failed`; (3) "Fix it", approved, whose render is e7bf7099 and failed the same way.
Pass 2's verifier replayed this job through the Main Creator with the compiled request and got a
capacity question production never asked.

What the replay did (`turns` in the receipt):

* Message 1 under client protocol 49: no plan, the reply says the lyrics could not be sourced
  (production asked whether to paste them). The replay's Main Creator went to
  `nova.research.public_facts`, which failed `terminal_schema`; production's did not (see Gaps).
* Message 2: `draft_applied` and `approval_requested`, as in production. The replay approved it;
  the job (68387484) ended `awaiting_device` with `creator_client_protocol: 49` stamped by the
  runtime, production's own value, and one pinned `guided_story` variant (4.0 s). Its recipe
  carries the six lyric lines verbatim as six text layers ("Vazgeçmem dünyalatı" ... "Seninle
  Fenerbahçe"). Production's render of the same message failed to compose that text.
* Message 3 ("Fix it") was queued behind message 2's turn, which observes its device job until a
  phone renders it. The run was stopped by hand at that point (receipt `interrupted`); the scratch
  database, the Redis keys and the worker were gone afterwards.

Result: production's failure does not reproduce on this head at the first render of the same
message: the replay plans the lyrics where production failed. e7bf7099 itself, the "Fix it"
retry, is not reached, because what it answered no longer happens. That run motivated the stop
rule now in the replay: after an earlier approved render that did not fail where production's
did, or that waits for a phone, the replay stops and reports that render (`stopped_early`,
`compared_production_render` d6c58d7b). The run itself predates the stop rule and its summary
printed `diverged_from_production` from the turn-1 research failure; a rerun of the same job with
the rule would end `device_plan_ready` at message 2, and was not made to keep under the $0.35
cap of this round. The free keyless rerun on the final code (no model keys) reproduces the turn
flow up to message 2, where it ends `diverged_from_production` because the keyless Main Creator
cannot plan, which is the divergence path working.

Gaps:

* Production's Main Creator runs on `gpt-6.1-sol` (its agent runs, 2026-11-14); this run used the
  code default `gemini-3.1-pro-preview`. Run 6 repeats it on Sol.
* No phone rendered the device plan.

## Run 4 (pass 2, round 2): b5dadbce, device plan on this head

Command: `make kria-replay-job JOB=b5dadbce-02bb-4299-8522-16c230d3d9aa` (2026-11-14, 07:28 UTC,
live Gemini, settled cost $0.099: Main Creator $0.059, edit copilot $0.028, clip metadata, clip
intent planner and edit proposal about $0.012 together).

Source job (production, 2026-10-19): `processing_failed`, `phone_plan_unsupported`, error
`EditProposalSnapshot duration_s >= 3 (input 2.987)`, one 4.3 s clip, creator request "3 second
video, static title". The brief binding recorded the clip (`bound_to_recorded_media: true`).

Result: `outcome: device_plan_ready`, exit 0, about 2 minutes. The worker pinned one variant
(`guided_story`, `duration_s` 3.0) and the job sat `awaiting_device`; the plan is the artifact
(`<run dir>/device-plan.json`), never an MP4 for a phone-first job. So the production failure is
not reproduced on this head: the 2.987 s proposal that production rejected now plans at 3.0 s.
That is the answer a fix PR wants to be able to give ("this job id now plans"), and it is data
about the code since 2026-10-19, not a claim about the production job.

Route comparison (`route_comparison` in the output): production ran `nova.creator.main` for this
plan item before it failed; the replay ran `nova.creator.main`, `nova.plan.clip_intent_planner`,
`nova.plan.edit_proposal`, `nova.edit.copilot` and `nova.video.clip_metadata` because it got past
the point where production stopped. No replay agent failed (`diverged_failures: []`).

Why this run and not the earlier verifier run: the pass-1 snapshot recorded
`EDIT_PROPOSAL_SEMANTIC_ENABLED=true` from the 2026-10-08 Fly digest, and the same job then ended
`semantic_edit_planning_failed` ("creator caption was dropped") inside
`nova.plan.semantic_edit_proposal`. No production plan item behind the 60 most recent content-plan
jobs (30 items, 2026-10-28..2026-11-14) has a `nova.plan.semantic_edit_proposal` or
`nova.plan.edit_proposal` run in `GET /admin/plan-items/<id>/proposal-trace`, and CLAUDE.md lists the
flag as `false`, so the snapshot now records it off (`job-data:no-semantic-planner-2026-11-14`).
The driver also reports such a failure as `diverged_from_production` now.

Cleanup check after the run: the scratch database was dropped and no Redis key of the run's
namespace remained.

## Run 3 (pass 2): c7c8fc75, ends on the Main Creator's question

Command: `make kria-replay-job JOB=c7c8fc75-3c33-46dc-b957-3a92093100a1` (2026-11-14, 05:52 UTC,
live Gemini, settled cost about $0.23).

Source job (production, 2026-10-29): `processing_failed`, `creation_text_failed`, one clip.
Replayed media: the one clip the brief binding recorded (`bound_to_recorded_media: true`).

Result: `outcome: main_creator_question`, exit 1, in about 2 minutes. The Main Creator
(`nova.creator.main`) did not propose a plan; it asked, reason `clip_intent_unresolved`: "I ticked
the clips I found for "my Cabildo photo". Tap any others that show it too, or untick one that
doesn't." In production the creator answered that card in the chat and the job followed; the chat
turns are not replayed, so the replay stops at the question and prints it. Before this fix the
same job polled for the full 30 minute timeout and printed "Timed out waiting for a Main Creator
plan" (pass 1 verifier, two jobs). The failure production hit (`creation_text_failed`) is not
reproduced by this run.

## Earlier runs (pass 1)

Job 89fefe4d (`creation_text_failed`, 2026-11-10, 15 clips) reached the proposal worker and failed
with `creator caption was dropped`; job fba2c864 (`creation_text_failed`, 2026-11-07, one clip)
ended `device_plan_ready`. Both ran on the item's current media. Since then the driver takes only
the media the brief binding recorded: for 89fefe4d that excludes two pool photos added two days
after the job ran, so the 89fefe4d result is not claimed for the current build. `--fetch-only`
on 89fefe4d, cecdc8d9 and c7c8fc75 now reports `bound_to_recorded_media: true` with exactly the
recorded 15, 11 and 1 clips.

## Limits

* Runs 1 to 4 replayed chat jobs through the Main Creator with the compiled request; run 5 and
  later replay the creator's messages turn by turn. A job whose request depended on a chat
  clarification could end at `main_creator_question` on that older route.
* Library pool assets cannot be fetched until the item debug's `gcs_path` field is deployed
  (3d28fa04 and 4620fd7c are refused for that reason). The binding match for rows without media
  ids was checked on the real 4620fd7c debug (1 clip by duration, 7 pool visuals by kind) but not
  replayed end to end, for the same reason.
* With the semantic planner off the replay's guided proposal still ran `nova.plan.edit_proposal`
  (a replay-only agent: none of the 30 production items shows one). Production's own proposal for
  this job failed before any agent ran, and the one successful guided item sampled (dde0488b) used
  the deterministic `prepared_collection_story` path, so whether production's successful guided
  items skip that agent is not established. `route_comparison` prints it for every run.
* Model output varies between runs; a different result on the same input is data.
* The snapshot records today's Main Creator model (`gpt-6.1-sol`). A job from before
  2026-11-14 planned on `gemini-3.1-pro-preview`, so its replay runs on another model.
  `model_differences` says so, and runs 7 and 8 both diverged at message 1 under that difference.
  The replay does not yet run each job on its own production model.
* Run 6 (`device_plan_ready`) predates pass 5. Runs 9 and 11 repeat it on the current code.
* `reproduced` asks that every agent whose last replay run failed also failed in production, not
  only the agent that ended the run. A fail-open agent that fails only in the replay therefore
  reads `reproduction_unproven` even when the plan then fails where production's did; the gap
  names it. A replay that stopped early is compared with production's earlier render without its
  planning error, which is not read.
