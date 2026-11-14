# KRI-1375 replay proof

Evidence for `make kria-replay-job` with live models. Every run reads the job and item debug
with `scripts/admin.py --prod GET`, copies the footage the job's brief binding recorded
read-only from the bucket, and runs the Main Creator route and the real worker locally under
`src/apps/api/tests/prod_flags.snapshot.json`.

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
* Library pool assets cannot be fetched until the item debug's `gcs_path` field is deployed.
* Model output varies between runs; a different result on the same input is data.
