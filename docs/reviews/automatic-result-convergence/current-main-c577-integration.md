# Current-main integration checkpoint

2026-11-06. Reviewed parent `58236eaeb6d59df66c1173629ddfa5ef9b9d9ae5` merged with verified main `c577a5fa5004f8edb84e607e7f6ff9a59920ed23` without conflicts. Main contains PR 2121 merge `0e869642bec7f92ed27c94d26ff47d1d68adfa0d`, prompt fingerprints PR 2133, ending-text PR 2138 and release metadata. The earlier main 4bfa integration already contains landed Cunda and speed. No unpublished reset/model branch is stacked.

Independent bounded static review found no concrete integration blocker: the39 incoming paths do not edit convergence sessions, receipts, delivery/write fences or source-replacement routes. Preview changes stay in the camera pipeline. Closing-text changes preserve the distinction between ending words and whole-video pins. No shared formal files or coverage hashes were rewritten.

Compatibility qualification: changed agent source files change `executor_code_digest`. A fully captured but not-yet-claimed old envelope may fail compatibility and reject before paid execution under existing policy. Missing legacy evidence remains unknown, not rejected. An already-claimed original token may still complete. This is not rollout proof: the old-envelope/new-code and late-token PostgreSQL controls must execute on the combined source before operational readiness. Do not reset claims or reconstruct/resend old input.

## Executed on the staged combined tree

- `python3 -m unittest discover -s scripts/ci -p 'test*.py'`: 168 passed 46.359 s. Log `/tmp/nova-auto-main-c577-ci.log`.
- Offline Python runner clears environment, disables dotenv, uses fake keys and denies socket connections. `src/apps/api/.venv/bin/python /tmp/slide-ai-offline-tests.py -q` with the following files: `tests/agents/test_prompt_fingerprint.py`, `tests/pipeline/test_says_done_b_whole_video_text.py`, `tests/pipeline/test_clip_text_tiktok_look.py`, `tests/services/test_slide_post_compose.py`, `tests/services/test_slide_post_chat_edit.py`, `tests/routes/test_slide_post_ai_requests.py`, `tests/services/test_slide_post_ai_prepared.py`, `tests/services/test_slide_post_ai_requests.py`, `tests/tasks/test_task_time_limits.py`, `tests/routes/test_saved_user_song_state.py` (all prefixed `src/apps/api/`). 187 passed 44 warnings in 4.83 s. JUnit `/tmp/nova-auto-main-c577-offline.xml`, log `/tmp/nova-auto-main-c577-offline.log`.
- `git diff --check`: passed. No native or database launch.

Prior 153 boundary, 493 task/one explicit DB skip and 61 route replay counts belong to 58236ea; historical 59 PostgreSQL/process controls belong to 72c248d5. They are not final-source evidence here.

## Remaining execution and release gates

The 781 app/39 package/8 UI convergence selection remains source-only and unexecuted. Inherited PR 2121 preview stop/restart, async segmentation, looping, scaler, tuning and recording cases also need combined-source native coverage; they are outside that finite convergence manifest. No compilation/discovery/device claim is made. Parent owns serial native/model allocation; reset currently has the slot. No competing PostgreSQL/process run was started. Need old-executor-envelope compatibility, original-token late completion, final-source receipt/convergence persistence, native/package/UI, shared formal correspondence/TLC, Linux decoder/aggregate, migration 0133–0138/mixed API-worker-Beat and effective production-flag inventory. Physical output/export/Photos and rollout remain unverified.

At 58236ea hosted run 42269109022, changes/select and journey-mapping passed; API0/API1, journey-offline/library and iPhone validation were still running at inspection. Metadata/staging checks have successful replacement runs; cancelled earlier metadata jobs are not test failures. No failed jobs were rerun. New integrated-source hosted status must be checked separately after publication.

Draft only. KRI935 is historical archived provenance; broad active tracking remains unresolved. KRI1233 is reset-only and is not this work's owner. No merge, operational migration, flag/configuration activation, rollout, deployment, paid provider call or private Cunda mutation occurred.
