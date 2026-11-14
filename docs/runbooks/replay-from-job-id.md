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
   when it ends), and runs
   `replay_creator.py --render --prod-flags tests/prod_flags.snapshot.json` (with
   `--thread-events` for a chat job; see "Chat jobs: the creator's messages, turn by turn").
4. Prints an `outcome`: `video_rendered` (local path of each output, `<run dir>/storage/<object
   path>`), `device_plan_ready` (an iPhone job: the worker built and pinned the device render
   plan and the job sits `awaiting_device`; the plan is in `<run dir>/device-plan.json`; this is
   the success state of every phone-first job), `device_plan_incomplete` or `failed` (status,
   failure reason and error detail, plus `production_failure_reproduced`: whether it is the
   failure production hit), `main_creator_question` (an older, non-chat job only: the Main
   Creator asked a clarifying question instead of proposing a plan; the question text and reason
   code are printed and the run stops at once), or `diverged_from_production` (a chat turn
   production approved ended otherwise in the replay, or the replay failed inside, or after, an
   agent production never ran for this plan item; see "Chat jobs" and "Route comparison"). A chat
   job answers its questions with the creator's own next messages. Exit code 0 for
   `video_rendered` and `device_plan_ready` only, 1 for any other outcome, 3 when the local
   machine could not host the run (no PostgreSQL role, fewer than 20 free connections, scratch
   database not creatable), which says nothing about the job.

For a phone-first job, which is every failed creation job since 2026-10-08 (the snapshot records
`IOS_DEVICE_ONLY_MODE=true`, `CLOUD_RENDER_EXECUTION_ENABLED=false`), the end-to-end artifact is
`device_plan_ready`: the pinned device render plan in `<run dir>/device-plan.json`, never an MP4.
What the iPhone then renders is the iOS gates' job. A PR that fixes such a job states the plan
(or the failure reason that remains, only when `production_failure_reproduced` is true) as its
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
  client protocol). Each medium carries production's projection (filename, upload receipt).
* Every `user_message` up to the job's `kria_turn_id` goes through `submit_turn`, the function
  behind `POST /creation-threads/<id>/turns`, in order, with the client protocol the app build
  sent (`X-Kria-Client-Protocol`, recorded on the event), its `request_outcome_link` (parent turn
  ids mapped to the replay's) and any structured answer the event stored (`clip_selection`,
  `choice_selection`, `song_order`, `song_range`, `moment_order`, `protected_region`, `scope`,
  `manual_edits`). The replay's worker runs the turn (`run_kria_turn` on `agent-control`).
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
agent runs of the replay's scratch database, and prints `route_comparison`:
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

The replayed footage is exactly the media the job's brief binding recorded (its clip
assignments and visuals), not whatever the plan item holds now: pool assets added after the job
ran are left out, and a recorded media id that can no longer be recovered refuses the run before
any model call. The same refusal applies to a creator song (`replay_creator.py` does not seed
songs) or a voiceover the item no longer holds. A job with no brief binding (older than the
binding) replays everything the item holds, and the printed coverage says
`bound_to_recorded_media: false`.

Bindings written since 2026-11-06 name no media: a clip row keeps its kind, duration and upload
receipt, a visual row only its kind and capture time. Those rows are matched to the item's media by
shape. A clip matches the item clip of the same kind whose duration agrees within 0.15 s (two
equally close candidates, or none, refuse the run); the matched row receives that clip's media id
so its upload receipt is restored. Visuals match the item's ready pool assets by kind, and the
item must hold exactly as many of each kind as the binding recorded, otherwise which ones the job
used is unknown and the run is refused. Coverage reports `matched_without_media_id`.

Precondition for jobs whose pool holds media-library assets: the production API must serve
`gcs_path` and `gcs_generation` on `GET /admin/plan-items/<id>/debug` (this PR's
`admin_plan_items.py` change). Until that is deployed, such jobs are refused with a message that
names the asset; pool assets under the item's own pool prefix still work.

Costs real model calls (Main Creator per message, text agents, clip analysis only for a clip the
brief binding recorded no analysis for). The clip analysis production planned with is restored
from the brief binding, so the replay does not analyse a clip twice. `route_comparison` prints
`replay_agent_runs` and `replay_spend_usd` (the scratch database's agent-run costs); the worker log
is `<run dir>/worker.log`.

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
* Model names (`main_creator_model`, `gemini_model`, ...) and protocol minimums hold the code
  default; a Fly override of either goes unnoticed until someone records it with `set`. Known
  gap: production's agent runs record `nova.creator.main` on `gpt-6.1-sol` (2026-11-14,
  `MAIN_CREATOR_MODEL`, KRI-1390), while the snapshot and so the replay run it on the code
  default `gemini-3.1-pro-preview`. Recording it is not enough: a local Sol call needs the
  manual-QA ledger envelope (`docs/reviews/kri-1384/sol-main-creator.md`, Gaps), or every
  reservation is refused and the turn falls back to Gemini. Until both are done, a replay's Main
  Creator answers on another model than production's.
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
  (all recorded media is seeded up front), and a structured answer whose question id the
  replay's question does not share (the runtime decides; `turns` lists what was sent).
* The replay stops at an earlier render that waits for a phone or that did not fail where
  production's did (see "Chat jobs"), so the job id's own render is not always reached.
* A device-render job (`native_device_only`) ends in a server-side plan, not an MP4. The replay
  reproduces everything up to and including that plan (`device_plan_ready` on success, the
  failure reason when the plan cannot be built). What the phone then renders is not replayed;
  use the iOS gates for that.
* Model output varies between runs; a different result on the same input is data, not a flake.
  State both outcomes in the PR.

## In a PR

A creation-bug fix PR names the production job id it replayed and what the run produced (see
`.github/PULL_REQUEST_TEMPLATE.md`).
