# Speech cleanup detection handoff

## Incident

Job `d2d20bd2` contained a Turkish hesitation at the start of the second
second. The source audio has a voiced island from approximately `7.406100` to
`7.977846` (571.746 ms), bounded by FFmpeg silence at `6.209660–7.406100` and
`7.977846–8.293356`. Whole-file ASR returned no word token for the island, so
the legacy lexical filler detector did not propose it. The rendered output
retained the same audio (output-time correlation `r=0.95683`).

## Detection findings

| Layer | Decision |
| --- | --- |
| ASR/AI | No lexical token covered the hesitation |
| FFmpeg/tool | Precisely bounded the voiced island with silence on both sides |
| Legacy acoustic detector | Evaluated the entire ASR gap, rejected the long/mixed gap, and never proposed the island |
| Pause detector | Removed only silence intersections, leaving the voiced island |
| Budget clamp | Spent the full removal budget on existing carriers, so a detector-only fix could still lose or slice the filler |

## Implemented fix

The branch adds mixed-gap V2 behind `SPEECH_CLEANUP_MIXED_GAP_MODE` (`off`,
`shadow`, or `apply`) and a persisted source-level canary percentage. It:

- computes silence-bounded soundful islands inside ASR-tokenless windows;
- preserves lexical and acoustic filler spans as atomic removal components;
- allocates the consent budget with filler priority and validates partition,
  word-boundary, duration, budget, and count invariants;
- keeps the legacy plan as the safe baseline when V2 fails validation;
- records bounded admin-only timing decisions for ASR, FFmpeg, detector,
  allocator, selection, and terminal publication;
- uses generation-scoped durable storage, CAS ownership, crash-safe cleanup,
  and last-good public projection for required speech renders;
- adds an admin diagnostics panel and an operator-only shadow audit script.

## Important rollout state

Production remains safe by default:

```text
SPEECH_CLEANUP_MIXED_GAP_MODE=off
SPEECH_CLEANUP_MIXED_GAP_ROLLOUT_PERCENT=0
```

Enable `shadow` first, review the admin receipt/audition output, then move a
small cohort to `apply`. Legacy and `off_v1` behavior remains unchanged.

## Main implementation files

- `src/apps/api/app/pipeline/silence_cut.py` — mixed-gap detector, atomic clamp,
  comparison result, and event payloads.
- `src/apps/api/app/services/speech_cleanup_selection.py` — mode/canary choice.
- `src/apps/api/app/services/speech_cleanup_terminal.py` — ownership-aware
  terminalization, rollback, cancel, and recovery.
- `src/apps/api/app/services/variant_generation_guard.py` — editor/renderer
  mutation barriers.
- `src/apps/api/app/services/durable_attempt_cleanup.py` and
  `src/apps/api/app/services/job_storage_deletion.py` — durable storage cleanup
  and exact-path deletion safeguards.
- `src/apps/api/app/tasks/generative_build.py` — generation reservation,
  required-speech rendering, publication, retry, and timeout handling.
- `src/apps/api/app/routes/admin_jobs.py` — admin receipt/debug serialization.
- `src/apps/api/app/scripts/audit_speech_cleanup_shadow.py` — local shadow
  audit and media-forensics helper.
- `src/apps/web/src/app/admin/jobs/[id]/SpeechCleanupDiagnostics.tsx` — admin
  diagnostics UI.
- `src/apps/api/app/migrations/versions/0092_storage_attempt_cleanup_index.py`
  — cleanup discovery index.

## Verification completed

- Focused backend regression suite: **1015 passed**.
- Frontend Jest suite: **293 suites / 3489 tests passed**.
- Ruff lint: clean.
- Changed Python files are formatted; `git diff --check` is clean.
- Full backend suite previously passed **11300 passed, 19 skipped, 2 xfailed**
  before the final ownership hardening pass; rerun it before merging if time
  permits.

## Handoff note

The branch is intentionally a single consolidated PR because the detector,
identity, storage, publication, and diagnostics contracts are inseparable. A
known lower-severity follow-up remains: the terminal reaper's oldest-first
bounded discovery can be starved by more than 50 permanently malformed rows.
The proposed keyset-pagination change was not applied because the safety gate
classified broadening automatic reaper behavior as requiring separate approval.
