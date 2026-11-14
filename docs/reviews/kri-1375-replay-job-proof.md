# KRI-1375 replay proof

Evidence for `make kria-replay-job` with live models. Every run reads the job and item debug
with `scripts/admin.py --prod GET`, copies the footage the job's brief binding recorded
read-only from the bucket, and runs the Main Creator route and the real worker locally under
`src/apps/api/tests/prod_flags.snapshot.json`.

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

* A job whose request depends on a chat clarification may end at `main_creator_question`; no
  replay of the recorded answers exists yet.
* Library pool assets cannot be fetched until the item debug's `gcs_path` field is deployed
  (3d28fa04 and 4620fd7c are refused for that reason). The binding match for rows without media
  ids was checked on the real 4620fd7c debug (1 clip by duration, 7 pool visuals by kind) but not
  replayed end to end, for the same reason.
* With the semantic planner off the replay's guided proposal still ran `nova.plan.edit_proposal`
  (a replay-only agent: none of the 30 production items shows one). Production's own proposal for
  this job failed before any agent ran, and the one successful guided item sampled (dde0488b) used
  the deterministic `prepared_collection_story` path, so whether production's successful guided
  items skip that agent is not established. `route_comparison` prints it for every run.
* Kria runtime thread turns before the creation request (draft, questions, approval wording) are
  not replayed; only the Main Creator start/confirm controllers and the worker are.
* Model output varies between runs; a different result on the same input is data.
