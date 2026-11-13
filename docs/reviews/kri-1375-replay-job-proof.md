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
