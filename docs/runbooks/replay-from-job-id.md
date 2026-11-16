# Replay a production job from its id (KRI-1375)

A creation bug is fixed when the same footage and request produce a finished video under
production settings with the real models. Unit tests and stubbed replays do not show that;
they passed while the founder hit the same failure again (KRI-1375 sessions 42606c80,
88c26d40). `make kria-replay-job` does the real run from a failed job id, so the PR can carry
the job id and the result.

```bash
make kria-replay-job JOB=<production job id>
```

## What it does

`src/apps/api/scripts/replay_job.py`:

1. `python scripts/admin.py --prod GET /admin/jobs/<id>/debug` and
   `GET /admin/plan-items/<item>/debug` (GET only), plus `gcloud storage cp gs://<bucket>/...`
   to copy the item's clips, pool images, pool videos and voiceover to
   `/private/tmp/nova-replay-job/<id>/inputs`. Pool assets come from the `gcs_path` and
   `gcs_generation` fields the item debug exposes (library assets live outside the item's pool
   prefix); a production API that predates those fields only yields assets under the item's pool
   prefix. Paths outside the job owner's `users/<id>/` prefix are refused. The bucket comes from `STORAGE_BUCKET` in the repo-root `.env` or `--bucket`. Nothing is
   ever written to production.
2. Writes the source manifest and debug files `scripts/replay_creator.py` reads. For a job the
   app's chat made (`runtime_version` 2 in the job debug, every creation job since the runtime-v2
   rollout), it also reads every event of that thread with
   `GET /admin/creation-threads/<thread>/events` (GET only, all pages) into `thread-events.json`.
   For an older job the creator request is `all_candidates.creator_request` on the job.
3. Creates a scratch database `nova_creator_fidelity_test_<run id>` (migrated to head; one per
   run, so two replays never wipe each other, dropped when the run ends), starts a
   Celery worker on Redis database 14 under a private `NOVA_CELERY_QUEUE_NAMESPACE`
   (`replay-<job>-<time>`; it never consumes the shared dev queues; the run deletes its own keys
   when it ends), waits until the worker finished its startup CLIP prewarm (`worker_warm_s`),
   and runs `replay_creator.py --render --prod-flags tests/prod_flags.snapshot.json` (with
   `--thread-events` for a chat job; see "Chat jobs: the creator's messages, turn by turn").
   While it waits on a turn or a render, the replay runs `tasks.reconcile_kria_turns` once a
   minute, as production's beat does (`reconcile` in the output). See "Stalls".
4. Prints an `outcome`: `video_rendered` (local path of each output, `<run dir>/storage/<object
   path>`), `device_plan_ready` (an iPhone job: the worker built and pinned the device render
   plan and the job sits `awaiting_device`; the plan is in `<run dir>/device-plan.json`; this is
   the success state of every phone-first job), `device_plan_incomplete` or `failed` (status,
   failure reason and error detail, plus `reproduction` and `production_failure_reproduced`:
   whether it is the failure production hit; see "Is it production's failure?"),
   `main_creator_question` (an older, non-chat job only: the Main
   Creator asked a clarifying question instead of proposing a plan; the question text and reason
   code are printed and the run stops at once), or `diverged_from_production` (a chat turn
   production approved ended otherwise in the replay, or the replay failed inside, or after, an
   agent production never ran for this plan item; see "Chat jobs" and "Route comparison"). A chat
   job answers its questions with the creator's own next messages. `stalled`: the replay
   stopped waiting (see "Stalls"); never read as a route result. `input_error`: the seeded
   inputs are not production's (seeded clips that do not bind as phone sources), so nothing was
   replayed; fix the input recovery, not the product. `spend_cap_reached`: the replay's agent
   runs settled more than `MAX_COST_USD` and the run was interrupted (see the cost paragraph
   below); never a route result. Exit code 0 for
   `video_rendered` and `device_plan_ready` only, 1 for any other outcome, 3 when the local
   machine could not host the run (no PostgreSQL role, fewer than 20 free connections, scratch
   database not creatable, a worker that never finished its prewarm, or a turn whose lease
   lapsed and that no worker took back), which says nothing about the job.

For a phone-first job, which is every failed creation job since 2026-10-08 (the snapshot records
`IOS_DEVICE_ONLY_MODE=true`, `CLOUD_RENDER_EXECUTION_ENABLED=false`, and
`IOS_NATIVE_DEVICE_ONLY_ENABLED=true` from the job records), the end-to-end artifact is
`device_plan_ready`: the pinned device render plan in `<run dir>/device-plan.json`, never an MP4.
What the iPhone then renders is the iOS gates' job. A PR that fixes such a job states the plan
(or the failure reason that remains, only when `reproduction` is `reproduced`) as its
replay result, says which message's render it is when the replay stopped early, and pairs it
with the iOS render gate when the change touches the render.

### Chat jobs: the creator's messages, turn by turn

A chat job is not one request. The creator sent messages, the Kria runtime answered each one
(a question, a draft and an approval card, or a render), and the job is the render an approval
dispatched. Sending the compiled `creator_request` to the Main Creator in one message is a
different route: the verifier's replay of e7bf7099 that way asked a capacity question production
never asked. So a chat job is replayed the way the app drove it:

* The seeded thread is runtime v2 with its own Creator session, the format choice the creator
  made (`format_prompt`, `action_select_format`), a `media_added` event and the device intent the
  app's thread creation stamps (`with_device_intent`, an iPhone thread when the messages carry a
  client protocol, the job is `native_device_only`, or its clips are analysis proxies, which only
  the app uploads; f88555bc's messages carry no protocol). Each medium carries production's
  projection (filename, upload receipt).
* Every `user_message` up to the job's `kria_turn_id` goes through `submit_turn`, the function
  behind `POST /creation-threads/<id>/turns`, in order, with the client protocol the app build
  sent (`X-Kria-Client-Protocol`, recorded on the event), its `request_outcome_link` (parent turn
  ids mapped to the replay's) and any structured answer the event stored (`clip_selection`,
  `choice_selection`, `song_order`, `song_range`, `moment_order`, `protected_region`, `scope`,
  `manual_edits`). The replay's worker runs the turn (`run_kria_turn` on `agent-control`).
* A structured answer names the question it answers, and the server mints every question id, so
  a recorded id never names a question of the replay. The replay sends the answer to its own open
  question of the same kind (the runtime's own "latest open question" rules), and only when that
  question matches production's: the same `conflict` and the option the creator tapped (choice),
  every category and candidate the creator tapped (clip picker). A clip answer to a category the
  replay's question did not ask is left out when the rest still answers the replay's question
  (`answers_sent.remapped.clip_selection.not_asked_by_replay`): the replay's server settled that
  category itself (40684a25 measured its too-short clips), which is what a fix may change. Pool assets get a fresh id per
  replay; the creator's `asset-<id>` taps are mapped to it (clips keep production's media id).
  `answers_sent.remapped` names each production and replay question id. When the replay's open
  question differs, the outcome is `diverged_from_production` with `production_question` and
  `replay_question`: the replay's previous reply already differed, and the message is not sent
  (no model call for it). A choice the server derived from the creator's words (`delegated`,
  "you choose"; `reanswer`) is sent as the words only, so the replay's server derives it again
  against its own question (`answers_sent.rederived_from_text`).
* Each entry of `turns` carries `production_reply` and `replay_reply` (`approval`, `draft`,
  `reply`, `error`, or the question kind such as `choice_question:ask_user:lyrics_text_needed`)
  and `matches_production`. `first_divergent_turn` names the first message whose reply differed,
  or, when every reply matched, the message whose recorded answer could not be sent
  (`answer_not_sent`).
  A reply that differs on one message is not evidence about the job either way: it says the
  replay left production's route there. `input_differences` lists what the replay could not give
  the planner as production did (see "Route comparison").
* Where production recorded `approval_approved` for a turn, the replay approves its pending
  approval through `decide_approval` under the same client protocol, so the runtime stamps
  `creator_client_protocol` on the job itself (production's 49 on e7bf7099; every app-version
  gate in the worker reads it). A render shape is sent only when the creator picked one.
* A turn production approved must reach an approval in the replay too. When it ends otherwise
  (a question, a refusal, a failure), the outcome is `diverged_from_production` with the replay's
  reply in `diverged_turn`, and later messages are not sent: they answered production's reply.
* After an earlier approved render, the next message is sent only when both renders failed (the
  creator read that failure). When production's render failed and the replay's did not, the
  replay stops there with that render as its result: the creator's later messages, typically a
  retry ("Fix it"), answered a failure that no longer happens. The output says so in
  `stopped_early` and compares with production's render of that message
  (`compared_production_render`). A render that waits for a phone (`awaiting_device`) also stops
  the replay, because the next message would queue behind a turn only a phone can finish.
* `turns` lists each message with the replay's status, events and reply.

### Route comparison

The replay drives the same Main Creator start and confirm controllers the chat thread calls, but
a snapshot value that is only approximated can send it down a planner production does not use.
The driver reads every agent name production recorded for the plan item
(`GET /admin/plan-items/<id>/proposal-trace`, all pages, plus the job's own agent runs) and the
same slice of the replay's scratch database: runs linked to the replay's plan item, plus the runs
of the replay's last job. Two kinds of run appear in no production read, so they are listed in
`replay_uncompared_agents`, counted in `replay_spend_usd`, and never compared:

* Runs scoped to the Creator session alone. On the Main Creator route the clip-intent agents
  run without a plan item (`creator_agent.py`).
* Agents production did not store yet when the job's thread began
  (`RECORDED_IN_PRODUCTION_FROM` in `replay_job.py`, from the Fly Deploy run that shipped each
  fix). Until #1973 (2026-10-31 10:21 UTC) the chat runtime ran `nova.plan.clip_intent_planner`,
  `nova.plan.clip_request_resolver` and `nova.video.clip_question` with no plan item, and their
  runs were not stored. Until #1737 (2026-10-22 06:02 UTC) every `nova.audio.music_matcher`
  insert failed its foreign key. On 19f61092 (2026-10-19) production's trace lists only
  `nova.creator.main` and `nova.edit.copilot`, and the replay named the planner and resolver as
  agents production never ran, while production's own reply quoted the resolver's result.

It prints `route_comparison`:
`production_agents`, `replay_agents`, `replay_only_agents`, `replay_failed_agents` (an agent whose
last run failed) and `diverged_failures` (failed in the replay, never run in production). When the
replay ends `failed` with a non-empty `diverged_failures`, the outcome is
`diverged_from_production`, the replay's own failure moves to `replay_failure`, and the run is not
evidence about the production job. A replay that ends `failed` with a reason other than
production's (`production_failure_reproduced: false`) after running agents production never
ran is reported the same way: it did not fail where production did. Example, pass 2: with `EDIT_PROPOSAL_SEMANTIC_ENABLED=true` (the
2026-10-08 Fly digest comparison) job b5dadbce failed in `nova.plan.semantic_edit_proposal`, an
agent no production item has run since 2026-10-19; the snapshot now records it off. A replay
that succeeds after running agents production did not run is reported in `replay_only_agents` but
keeps its outcome: a fix is expected to get past the production failure.

`model_differences` and `prompt_version_differences` compare, per agent, the models and prompt
versions the replay ran with those production's runs for the item recorded up to the job's end
(later runs are left out: production may have switched model since). A failed replay with another
failure than production's after running an agent on a model production did not use is a
divergence too. A prompt version difference is expected (the code changed since the job); a model
difference means the replay did not run on production's model, and `input_differences.models`
repeats it. Before the run the driver prints production's models for the item next to the
Main Creator model the replay will use.

`input_differences` also counts production state the replay does not recreate:
`media_library_backed_media` (media the thread took from the media library; the library's
catalogue rows and facts are not seeded), `visual_capture_times_not_restored` (a pool visual
the binding recorded without a media id keeps its capture time only on the binding row) and
`pool_visuals_not_recorded` (pool media replayed although the binding did not record it; see
below). Clip capture times, shot ids and source filenames come from the binding rows and are
restored.

The replayed footage is exactly the media the job's brief binding recorded (its clip
assignments and visuals), copied at the `storage_generation` the binding recorded (the bytes
production analysed; the item debug often carries no generation), not whatever the plan item
holds now: pool assets added after the job
ran are left out, and a recorded media id that can no longer be recovered refuses the run before
any model call. The same refusal applies to a creator song (`replay_creator.py` does not seed
songs) or a voiceover the item no longer holds. A job with no brief binding (older than the
binding) replays every clip row the item holds and the ready pool assets created before the job
ran (as below for a binding without `visuals`). The printed coverage says
`bound_to_recorded_media: false`, and `clips_not_dated` counts the clip rows it took whole: the
item debug does not date clip rows, so a clip added after the job cannot be told apart. The same
holds for the item's voiceover.

Some bindings arrive with no media named. The admin job debug strips media ids, and every value
that names a media file (`manifest_identity`, `storage_generation`, paths, file names), from a
job whose speech cleanup is still `required_v1` with no finished variant
(`_mask_active_speech_rerender` in `app/services/public_assembly_plan.py`): f88555bc and 4620fd7c
arrive that way, 50f67a06 and 40684a25 (`off_v1`) do not. A clip row then keeps its kind,
duration and upload receipt, a visual row only its kind and capture time. Those rows are matched
to the item's media by shape. A clip matches the item clip of the same kind whose duration agrees
within 0.15 s (two equally close candidates, or none, refuse the run); the matched row receives
that clip's media id and the same value as `manifest_identity` (what the clip row writer stores),
so its upload receipt binds. Visuals match the item's ready pool assets by kind, and the item must
hold exactly as many of each kind as the binding recorded, otherwise which ones the job used is
unknown and the run is refused. Coverage reports `matched_without_media_id`, and
`copied_unpinned` counts sources copied at the latest generation because neither the binding nor
the item debug pinned one (a creation-thread proxy is written once, so it has one generation).

After seeding, the replay binds its analysis-proxy clips with `bind_phone_sources`, the check the
manifest makes before it offers the iPhone render. Production's turn dispatched, so its receipts
bound; seeded rows that do not bind would leave the planner without footage (the runtime answers
"Add at least one clip" without a model call). The run stops there with outcome `input_error`
and the binding's reason, before any model call: a statement about the replay's inputs, never a
route result.

A binding without a `visuals` key (written before KRI-1069, or by a turn that snapshots clips
only; 50f67a06, f88555bc, 40684a25, fba2c864 and e7bf7099 have none) does not say which pool media
the plan saw, and "none" would drop footage production used. The replay then keeps the item's
ready pool assets created before the job ran (the manifest lists every visible pool row) and
leaves out the later ones; coverage says `pool_visuals_recorded: false` with
`pool_visuals_inferred` and `pool_visuals_added_after_job`, and `input_differences` repeats the
inferred count. Every pool asset the job's own plan names (`asset-<id>` in a brief scope, a render
requirement or a resolved clip intent) must be among the replayed sources, with or without a
binding; one the item no longer holds refuses the run.

Precondition for jobs whose pool holds media-library assets: the production API must serve
`gcs_path` and `gcs_generation` on `GET /admin/plan-items/<id>/debug` (this PR's
`admin_plan_items.py` change). Until that is deployed, such jobs are refused with a message that
names the asset; pool assets under the item's own pool prefix still work.

Costs real model calls (Main Creator per message, text agents, clip analysis only for a clip the
brief binding recorded no analysis for). The paid-call ledger runs in the scratch database as a
development manual-QA run (`AI_MANUAL_QA_TEST_RUN_ID` = the run's namespace) capped at
`MAX_COST_USD` (`--max-cost-usd`, default 1.00, at most 2.00). The ledger reserves Sol (OpenAI)
planning against it. Gemini calls are not reserved while `AI_COST_CONTROL_ENABLED` stays off, as
in production, so the driver also reads the scratch database's settled agent-run cost every 5 s
and interrupts the run once it is over the cap (`spend_cap_reached`, the spend that stopped it in
`failure_reason`). A call counts once it settled, so a run can pass the cap by the call that
crossed it. A provider call that writes no agent run is not counted: Whisper transcription of a
clip's speech (billed per minute) is one. A Sol Main Creator call reserves $0.22 to $0.25 until
it settles (about $0.05), and the reservation must fit under the cap on top of what earlier
messages settled. When it does not, the ledger refuses it (`AiBudgetExceededError` in the worker
log) and the Main Creator answers on its Gemini fallback, which `model_differences` then shows.
e7bf7099's two Sol messages fit under 0.30 and not under 0.27 (2026-11-15), so allow about
$0.25 plus $0.05 per earlier message. The clip analysis production planned with is restored
from the brief binding, so the replay does not analyse a clip twice. `route_comparison` prints
`replay_agent_runs` and `replay_spend_usd` (the scratch database's agent-run costs); the worker log
is `<run dir>/worker.log`.

### Is it production's failure?

A failed replay whose reason matches production's is not yet production's failure.
`phone_plan_failed` and its siblings are catch-all reasons: on 1be34fc8 production's eight agent
runs all succeeded and its plan failed with `ResearchUnavailable` ("This explanation needs a
longer clip or shorter wording"), while the replay took today's `nova.research.text_layers` route
and failed its second `nova.research.public_facts` search (`terminal_schema`). Both read
`phone_plan_failed`. So a failed replay carries `reproduction`:

* `reproduced`: the reason matches, production's trace is complete, the replay ran no agent
  production did not, every agent whose last replay run failed also has a failed run in
  production's trace (`failed_only_in_replay` is empty), and when both jobs recorded a
  planning error (`planning_failure`, the job's last `phone_plan_failed` pipeline event; the
  production one is in `compared_production_render`) the error class is the same. Only then is
  `production_failure_reproduced` true.
* `failure_reason_matched_route_differs`: the reason matches, but the replay ran agents
  production never ran for the item.
* `reproduction_unproven`: the reason matches on production's route, but an agent failed in
  the replay that production got through, the planning error class differs, or production's
  trace was cut off.
* `failure_reason_differs`: another failure than production's (see above for when that is a
  divergence).

`reproduction_gaps` says, in one sentence each, which agents and errors stand in the way.
Neither of the two middle verdicts says the failure is gone or that it remains; a fix PR cannot
use them as its replay result. A job whose trace holds no `phone_plan_failed` event (a render
failure, or a production job like f88555bc with an empty trace) has no `planning_failure`; then
only the route and the agents are compared. A replay that stopped
early is compared with production's render at that message, whose planning error is not read.

### Stalls

A replay that times out waiting on a turn, a render or a Main Creator plan ends `stalled` with the
turn's status, whether its lease lapsed, and its `abandoned_claims`. It is never compared with
production's route: in verifier run 1 on 1cb33402 a turn that sat `planning` on a lapsed lease
for the full timeout read as `diverged_from_production` because two planner agents were missing.
Two things keep the local worker from losing a turn production would have finished. The driver
waits for the worker's CLIP prewarm before the first message (that run's first turn lost its 15 s
lease while torch and CLIP loaded), and the replay sweeps `tasks.reconcile_kria_turns` every
minute, so a lapsed lease is republished as production's beat does. A `stalled` run with a lapsed
lease exits 3.

## Safety

* Production contact is a GET through `scripts/admin.py --prod` and a read-only
  `gcloud storage cp` from the bucket to a local path.
* The replay subprocesses get a clean environment: `PATH`, `HOME`, and the `GEMINI_API_KEY`,
  `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` model keys from `.env` (no `ADMIN_*`, `INTERNAL_*`,
  `RESEND_*` or other keys). No production database, storage, Redis or
  admin credential reaches them.
* The scratch database is created only on the local dev server's port 5432 (`.env`
  `DATABASE_URL`), and `replay_creator.py` refuses any other port: `fly proxy 15432:5432 -a
  nova-db` (`export-template-text-snapshots.md`) puts the production cluster on localhost too.
* `replay_creator.py` refuses anything but a local PostgreSQL database named
  `nova_creator_fidelity_test` or `nova_creator_fidelity_test_<run id>` with no query string
  (libpq takes `host=` from the query, so a query could redirect the connection), `STORAGE_PROVIDER=local` with `E2E_FIXTURES=true`, Redis
  `localhost:6379/14`, and an output directory below `/tmp` separate from the inputs.

## Production flag snapshot

`src/apps/api/tests/prod_flags.snapshot.json` holds the effective production value of every
boolean `Settings` field, every `*_user_ids` allowlist, every `*_rollout_percent`, every `*_model` name, every
`*min_client_protocol` and `phone_render_verified_features`. `tests/_prod_profile.py` (the `prod_profile` fixture),
`scripts/kria/score_route.py`, `score_29_ekim.py`, `score_assos.py` (the shared set plus
`crossfade`), `score_kri1365_edit_route.py` and `replay_job.py` read it. Hand lists that remain,
none of them production-faithful:

* Copies of a production verified-feature list: `scripts/ios/kri-1009-talking-title.py`,
  `scripts/ios/qendresa-caption-shape-e2e.py`, and
  `tests/kria/test_kri1009_talking_top_text_title.py`, which pins the list production held on
  2026-10-28 on purpose (a recorded incident).
* iOS E2E scripts that verify exactly the capabilities their recipe needs, by design (the device
  check, not production's config): `cta-ending-render-e2e.py`, `kri-492-length-e2e.py`,
  `kri-524-creation-e2e.py`, `phone-background-windows-e2e.py`, `phone-montage-render-e2e.py`,
  `phone-n2-fullscreen-e2e.py`, `phone-photo-render-e2e.py`, `qendresa-13-tight-pauses-e2e.py`,
  `qendresa-clip-text-look-e2e.py`, `qendresa-list-labels-e2e.py`,
  `talking-label-endcard-render-e2e.py` (all under `scripts/ios/`).

Each value is validated against its `Settings` type (strict, from JSON), by `set` and by
`check`: a bare string is not a list and `"true"` is not a boolean.
`scripts/prod_flag_snapshot.py` writes it. Each entry carries its evidence:

| Source | Meaning |
| --- | --- |
| `default` | The code default. Production may override it; that was not read. Unconfirmed. |
| `legacy-fly-digest-2026-10-08` | The flag shared `PHONE_RENDERING_ENABLED`'s Fly secret digest on that date (the former hand list). A digest comparison, no value read. Later changes are not in it. |
| `live:phone-render-config` | Read from production with `GET /admin/kria/phone-render-config` (names, no secret). |
| `decisions-md:<date>`, `runbook:<name>` | A value the repo's own docs record as applied in production (for example `IOS_DEVICE_ONLY_MODE=true` and `CLOUD_RENDER_EXECUTION_ENABLED=false`, DECISIONS.md 2026-11-02). Only as current as the doc. |
| `job-data:<what>-<date>` | Inferred from production job records read with `GET /admin/jobs/<id>/debug` (for example every sampled job carries `creator_brief_binding`, so `KRIA_BRIEF_BINDING_ENABLED` is on). |

What is approximated:

* A false flag whose entry says `default` may be on in production. `python
  scripts/prod_flag_snapshot.py unverified` lists the boolean flags still unconfirmed.
* Model choices (`*_model`, `*_thinking_level`) and protocol minimums hold the code default
  unless recorded. `main_creator_model` is `gpt-6.1-sol`, read from production's agent runs
  (`job-data:main-creator-sol-2026-11-14`); `main_creator_thinking_level` `medium` is inferred
  (agent runs do not record the effort; the KRI-1390 rollout command sets it with the model, and
  those runs' tokens and latency match Sol at medium). The replay runs with both; the test
  profile leaves model choices out (`tests/_prod_profile.py`), since tests replace the model.
  Another agent's model override is caught after a run by `model_differences`; record it with
  `set` once a job shows it. Production's agent runs are the evidence for a model:
  `GET /admin/plan-items/<id>/proposal-trace` lists `model` per run.
* Allowlists are Fly secrets. The snapshot holds the code default, not production membership.
  The gates differ in what an empty list means: `phone_render_user_ids` and
  `kria_runtime_v2_phone_user_ids` read empty as everyone, while the brief-binding, approval-
  readiness, suggestion-chip, steer-now, creative-brief, clip-facts and editorial-taste
  allowlists only ever add accounts (empty admits nobody). A job whose account sits inside an
  allowlist in production but not in the replay takes a different path; where a job record
  proves the cohort is on (brief binding), the global flag is set instead.
* Values changed after 2026-10-08 that no doc records are missing. `check` fails when a doc
  line says a setting is "ON in prod" and the snapshot has it off (CLAUDE.md,
  agents/DECISIONS.md, `docs/runbooks`, `docs/pipelines`); other phrasings, such as an applied
  `fly secrets set` in a decision record, are only caught if someone runs `set` for them.

Recording the documented values moved the test profile too: production is device-only
(`IOS_DEVICE_ONLY_MODE=true`, `CLOUD_RENDER_EXECUTION_ENABLED=false`) with brief-binding writers
on, and about 45 existing tests across seven files were written against the cloud lane or the
unbound draft path. They take the `prod_profile_pre_cutover` fixture (or
`apply_prod_profile(..., hold_at_default=PRE_CUTOVER_SETTINGS)`), which holds those three
settings at the code default; they are the known places the "production profile" is not
production. Converting a file to the full profile means deciding what the device-only path should
assert there.

`IOS_NATIVE_DEVICE_ONLY_ENABLED=true` (`job-data:native-device-only-2026-11-15`) is inferred, not
documented: every one of the 24 production jobs read for this ticket carries
`assembly_plan.native_device_only=true`, which dispatch copies from a thread intent that only
that flag stamps. Recording it changed one test fake (`test_kri598_voiceover_planner.py`), whose
mock database had no answer for the thread-state read the flag adds.

Refresh without reading a Fly secret value:

```bash
cd src/apps/api
python scripts/prod_flag_snapshot.py refresh --live      # re-reads phone-render-config, keeps recorded evidence
python scripts/prod_flag_snapshot.py set KRIA_FOO_ENABLED=true \
  --source "claude-md:2026-11-13" --describe "CLAUDE.md says ON in prod"
python scripts/prod_flag_snapshot.py check                # exit 1 if Settings and snapshot disagree
```

Evidence order: a newer production observation (a live read, job data, a digest comparison)
outranks a doc line written before it. A doc line outranks an observation only when it records a
change applied after it. `EDIT_WIDE_LOOKS_ENABLED` shows the rule: CLAUDE.md listed it "off" from
its dark ship (#857, 2026-08-18), the 2026-10-08 digest showed it on, so the snapshot keeps it on
(`reconciled:edit-wide-looks-2026-11-14`) and the CLAUDE.md line now says both.

Record a flag with `set` when a repo source states production's value (CLAUDE.md, DECISIONS.md,
a runbook that names the applied Fly secret) or when someone with secret access tells you; put
that source in `--source`. `tests/test_prod_flags_snapshot.py` fails when `Settings` gains a
flag the snapshot has not seen, so a new flag cannot silently run at its default in a
"production" test. Never paste a secret value into the snapshot.

## Fidelity limits

* A chat job is replayed message by message from the footage in storage (iPhone creations
  upload analysis proxies, not the originals); an older job drives the Main Creator route with
  the creator request recorded on the job. Not replayed: the app's admission checks on the
  routes (minimum protocol, native client), the speech-cleanup fields of the approval body (the
  replay approves as a build without the cleanup card), messages sent while a turn was still
  running (each message waits for the previous turn), media attached after the first message
  (all recorded media is seeded up front), and media-library catalogue rows. A structured
  answer reaches the replay only when the replay asked the same kind of question (see "Chat
  jobs").
* The replay stops at an earlier render that waits for a phone or that did not fail where
  production's did (see "Chat jobs"), so the job id's own render is not always reached.
* The replay never answers a question the creator was not asked. When today's code answers an
  early message with a question production did not ask, the creator's next tap has nothing to
  answer and the run ends `diverged_from_production` there (19f61092, f88555bc and 50f67a06 on
  2026-11-15, all at message 1). Answering for the creator from their later messages would be
  the stand-in evidence this command exists to replace. Such a job cannot carry a fix PR's
  proof: replay a later job that failed the same way, or write "n/a" with the reason.
* A device-render job (`native_device_only`) ends in a server-side plan, not an MP4. The replay
  reproduces everything up to and including that plan (`device_plan_ready` on success, the
  failure reason when the plan cannot be built). What the phone then renders is not replayed;
  use the iOS gates for that.
* Model output varies between runs; a different result on the same input is data, not a flake.
  State both outcomes in the PR.

## In a PR

A creation-bug fix PR names the production job id it replayed and what the run produced (see
`.github/PULL_REQUEST_TEMPLATE.md`).
