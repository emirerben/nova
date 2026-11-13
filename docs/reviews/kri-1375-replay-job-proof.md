# KRI-1375 replay proof

Command: `make kria-replay-job JOB=89fefe4d-137a-409c-b846-760161dc5063` at the PR head.

Source job (production, 2026-11-10 18:53 UTC): `processing_failed`, failure reason
`creation_text_failed` ("I couldn't prepare the text while keeping your approved wording").
Plan item `fac3dbd4-f61b-4c2d-b5c0-a387d18cfa2a`, iPhone creation thread, 15 clips and 2 pool
images, request asks for a word-by-word middle text plus an English translation at the bottom.

Replay inputs: the job and item debug via `scripts/admin.py --prod GET`, the 17 sources copied
read-only from the bucket (iPhone analysis proxies; the originals stay on the device), the
creator request recorded on the job, and `src/apps/api/tests/prod_flags.snapshot.json`.

Result (two full runs, 2026-11-13 19:48 and 20:01 UTC, live Gemini models, same outcome both times):

| Stage | Outcome |
| --- | --- |
| Pool and clip analysis (`nova.video.clip_metadata`) | 17 of 17 ok |
| Main Creator plan (`nova.creator.main`, gemini-3.1-pro-preview) | ok, plan confirmed |
| Proposal worker (`nova.plan.semantic_edit_proposal`, gemini-2.5-flash) | `terminal_schema`: `creator caption was dropped` after one schema retry |
| Replay exit | no job dispatched, no video; `semantic_edit_planning_failed`: "Kria couldn't preserve every requested part of this edit" |

The replay fails on the same request and the same family as production (the creator's approved
text is not kept), one stage earlier than the production job (production failed assembling the
phone plan with "The approved bottom text is not on screen together with the approved middle
text"). The difference is expected: the replay drives the Main Creator route, not the Kria
runtime thread turn, and models vary between runs.

Earlier replays on the same job were blocked by the driver, not the product, and were fixed in
this PR: the replay driver did not wait for the asynchronous planner, passed no request object,
and dropped the iPhone upload receipts the planner needs (the admin item debug omits them; the
job's brief binding keeps them).

Cost of the proof (settled `agent_run` costs across every attempt): about $0.46.

## Second proof: the device-plan success path (job fba2c864)

Command: `make kria-replay-job JOB=fba2c864-cdc1-4e35-ad74-740348f2781e` (2026-11-13 20:59 UTC,
2 min 15 s, one 11 s clip, live Gemini models, well under $0.05).

Source job (production, 2026-11-07 21:09 UTC): `processing_failed`, `creation_text_failed`.
Replay result: `outcome: device_plan_ready`. The Main Creator plan was confirmed, the worker built
and pinned the iPhone render plan (variant `guided_story`, 4.0 s, intro text "The prompt I run at
the end of every Claude session") and the job sits `awaiting_device`, exit code 0. The plan is
written to `<run dir>/device-plan.json`. Production failed this request; the replay did not, so the
failure is not reproduced by this run (model output varies between runs).

An earlier build of the driver hung on exactly this success: `awaiting_device` was not a
terminal status, so it polled until the 1800 s timeout and printed a null status with a timeout
reason. `awaiting_device` is now terminal and reported as `device_plan_ready`.
