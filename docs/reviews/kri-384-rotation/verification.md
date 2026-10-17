# Saved uploads: newest 500

Acceptance and failure cases recorded before implementation:

- At 500 active items a verified, unique import succeeds and archives the oldest
  catalogue entry (created_at, then id). Recently reused old uploads still age by
  upload time; manual archives do not consume slots. Other owners are untouched.
- Reservation, failed byte verification, expired/claimed receipts, duplicate
  finalization and retry do not evict saved media. Outstanding upload reservations
  remain bounded independently of the active catalogue.
- New admissions and archive updates share the owner fence and caller transaction.
  Concurrent imports stay at the cap; a rejected/rolled-back attachment restores
  the old catalogue. Repairs and compatible duplicates consume no new slot.
- Archive never deletes sources or post references. Independent photo/video posts
  continue resolving their pinned originals after automatic rotation. New Saved
  uploads selections omit archived entries. Existing retention rules still apply.

## Implementation

The shared verified-ingest seam rotates only when it must create a new asset.
It uses the existing owner advisory lock and the caller's transaction. New
creation times use PostgreSQL's wall clock after admission, so transactions that
wait on the lock cannot reorder completed imports by their earlier start times.
Reservations remain bounded at 500 independently of catalogue occupancy.
No schema, flag, iOS client, source deletion or retention-policy change is needed.

Historical backfill must remain a fill-only operation. A companion safeguard
for draft PR #1640 is prepared and verified locally: its owner-locked capacity
check prevents old migration candidates from rotating newer saved uploads.
Publication of that separate draft update awaits explicit approval after an
automatic approval rejection. Do not rerun the unsafeguarded backfill against
this runtime. No backfill script is installed or running in production.

## Verification (2026-10-16)

- Before implementation, the full-500 finalize, concurrent unique admission,
  transaction rollback, and shared-photo rotation journeys all failed on the
  existing remove-before-import 409 response (4 failures).
- After implementation, 68 focused library checks passed on migrated disposable
  PostgreSQL: ingest, reuse, reusable analysis, media-assets routes, attachment
  routes, source verification and transcription-cost contracts. None skipped.
- Four historical-backfill checks passed separately against this runtime and the
  updated script from #1640. Running tests from two worktrees in one pytest
  process causes duplicate `tests.conftest` identities; separate runs passed.
- CI selector/journey gate checks: 41 passed.
- Scoped Ruff and `bash scripts/preship-check.sh`: passed. No incident gate maps
  to these paths. Release metadata remains owned by post-merge automation.
- In-host structural review: owner isolation, reservation bounds, duplicate/repair
  behavior, immutable sources, reference retention and rollback checked. No open
  findings. Independent subagent/model reviews omitted per the user's instruction.

Commands (from the rotation worktree):

```bash
bash scripts/check-api.sh test tests/integration/test_media_library_ingest.py tests/integration/test_media_library_reuse.py tests/integration/test_media_library_analysis.py tests/routes/test_media_assets.py tests/routes/test_media_library_attachments.py tests/services/test_media_library_source_verification.py tests/pipeline/test_library_transcription_cost.py -q
python3 -m unittest scripts.ci.test_journey_gate scripts.ci.test_select_tests -q
bash scripts/preship-check.sh
```

Evidence level: local API/service journeys with real PostgreSQL and controlled
storage/provider fixtures. The photo journey verifies two independent post
projections and pinned native source identity after rotation; the editor footage
journey resolves the original-bound video source after rotation. No new native
export, physical-device upload or production rotation is claimed. No model calls
or cloud-media mutations were made for these checks. Merge/deployment and the
production-device check remain release gates.

