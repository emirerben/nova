# KRI-118 lane L7: prod failure replay fixtures

`tests/fixtures/kri118/*.json` are structural, anonymized shapes reproducing
real prod job failures, used by `test_prod_failure_replay.py` to check
whether the KRI-118 fixes (lanes L0-L6) resolve them -- offline, with no
Gemini/LLM calls, no ffmpeg, no Whisper.

## How they were produced

Prod admin access via `scripts/admin.py --prod` **worked** in this session
(the worktree's `.env` was symlinked from the primary checkout via
`bash scripts/worktree-setup.sh`, which links a real `ADMIN_PROD_API_KEY`).
This is real data, not synthesized from docs.

1. Pulled the last 200 jobs: `python scripts/admin.py --prod GET "/admin/jobs?limit=200"`.
2. Counted `failure_reason` and confirmed it matched the task brief exactly:
   `guided_story_render_failed` (6), `guided_story_duration_impossible` (4),
   `phone_plan_unsupported` (3), `speech_cleanup_failed` (3),
   `guided_story_receipt_mismatch` (2).
3. For one representative job per reason, pulled
   `python scripts/admin.py --prod GET "/admin/jobs/{id}/debug"` and read
   `job.assembly_plan` (`guided_edit.approved_proposal.media` /
   `.story_beats`, or `all_candidates` for non-guided jobs) plus
   `job.pipeline_trace` for the render-stage event that recorded the failure.
4. Extracted ONLY the structural shape needed to reproduce the failure class
   deterministically: media kind/count/duration, story-beat count and
   per-beat duration/media_ids, edit_format, voiceover/speech-cleanup
   presence, and (for the phone fixture) the exact real exception message
   string. Real GCS paths, real media IDs, real user IDs, and any
   user-authored text (beat topics/thoughts, titles) were replaced with
   synthetic placeholders (`clip-a`, `beat-0`, generic topic names). No PII,
   no real storage paths, no real UUIDs are in any fixture file.
5. Job IDs are NOT stored in the fixture JSON (kept out of anything
   committed); each fixture's `notes` field instead cites the app-level file
   the raise site lives in and grep-verifies the exception type, so the
   provenance is checkable without a real job ID.

## Why each fixture stops where it does

None of these failures happen at pure-Python plan-compile time alone --
`guided_story_render_failed` and `guided_story_receipt_mismatch` are
ffmpeg-render/post-render-verification signals, `speech_cleanup_failed`
depends on a real Whisper transcript, and `phone_plan_unsupported`'s specific
prod trigger depends on per-moment source-refit timing that itself depends on
real analysis-proxy file lengths. None of that is available offline (raw
media is never committed -- see CLAUDE.md "Raw uploads ... NEVER in git").

So the replay depth varies honestly by fixture:

| Fixture | Replay depth |
|---|---|
| `guided_story_duration_impossible_1` | **Full, real replay.** `_allocate_beat_durations` is pure arithmetic (no ffmpeg) -- `validate_proposal_compiles` is re-run against the real durations and its real outcome is asserted. Confirmed 2026-09-23: this exact shape now compiles (previously failed in prod). |
| `guided_story_render_failed_1` / `guided_story_receipt_mismatch_1` | **Planning-time layer replayed for real** (the shape must still compile, matching prod reality that these plans WERE approved/dispatched); **render/verification layer asserted as a typed contract** (cannot re-run ffmpeg offline). |
| `phone_plan_unsupported_1` | **Full, real replay of the worker failure-mapping path** using the exact real exception message from prod, through the same harness `test_phone_guided_dispatch.py` already uses for synthetic cases. |
| `speech_cleanup_failed_1` | **Typed-contract replay.** The real bailout decision needs a real transcript; the `SpeechCleanupFailure` exception contract at the real raise site (grep-verified) is asserted instead. |

## Adding a new fixture

1. `python scripts/admin.py --prod GET "/admin/jobs?limit=200"`, find a job
   with the `failure_reason` you want.
2. `python scripts/admin.py --prod GET "/admin/jobs/{id}/debug"`.
3. Extract the structural shape as above; anonymize; drop the real job ID.
4. Add `tests/fixtures/kri118/<failure_reason>_<n>.json` and a matching test
   (or parametrize an existing one) in `test_prod_failure_replay.py`.
