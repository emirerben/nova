# Background output verification and bounded source recovery

Completion now returns `202 checking` after saving a durable export outbox. It
does not download or decode media in the API request. The existing performance
render worker consumes `tasks.verify_device_export` on `celery`; the maintenance
worker only redelivers pending checks and expires abandoned queue entries.

The picture, source-window/order, duration, OCR and audio verifier is unchanged
from the benchmarked KRI-734 implementation. Its complete report remains the
publication gate. The phone withholds playback, Save and Share for unchecked
candidates, polls lightweight status reads, and resumes after interruption.
Last-good video/poster/URL data is retained on failure.

## Original approval and corrective action

The approval card offers **Allow up to 2 automatic repairs**, with a visible
30-minute permission window. The creator can switch it off. Older clients,
programmatic approvals and approvals without the option grant zero repairs.
The grant belongs to the original approval execution, draft, ownership epoch,
media snapshot and compiled recipe digest. Changed or expired authority and
unknown outcomes cannot authorize another render.

The first supported repair is source decoding normalization: complete measured
picture failures identify the affected clip/source. The iPhone rebuilds those
originals as fresh local H.264/AAC decoding copies, using the existing native
conversion path, and renders the same recipe. The original bytes remain intact
and their fingerprints are checked before conversion. No authored field changes.
Every replacement gets a new device revision/upload attempt and passes the full
actual-output verifier again before Ready. No model or creative planner runs.

A source already normalized in this grant is not tried again. A second repair
requires a newly identified source and stays within the original total of two.
Text-only, unsupported, partial and authority failures stop for an actionable
review rather than speculative rerendering. The ledger records measurements,
requirement/clip IDs, field paths, the execution change, elapsed time and outcome.
Model calls/cost are zero; infrastructure cost is not represented as zero.

## Durability and limits

- Claims are serialized under the owner and Job locks and fenced again after
  media I/O. The claim lease is 360 seconds, beyond the 300-second worker hard
  deadline. Duplicate deliveries cannot publish or spend repair permission twice.
- Infrastructure retries check the same uploaded bytes at most three times;
  these are separate from the two corrective render attempts.
- A queue outage stops checking after 20 minutes, while a live worker retains
  its whole lease. Expired repair permission blocks replacement publication.
- Source normalization is limited to a 300-second/1-GiB original, with a
  180-second conversion deadline. It runs on the iPhone, so iOS suspension may
  require reopening the app to finish; server verification continues separately.

Live device testing exposed a pre-existing KRI-674 lock inversion: Retry locked
the Job before the database trigger acquired the media-owner advisory lock,
while phone polling used owner then Job. Retry now uses owner first. The exact
PostgreSQL race fails before the fix and passes afterward. The new queue-expiry
writer follows the same ordering.

## Evidence

Actual MP4 + real PostgreSQL + HTTP integration proves broker-gap redelivery,
duplicate claim exclusion and one checked publication. Existing actual-media
negative controls continue to reject incorrect outputs and retain last-good
artifacts. Consent/expiry, unknown outcomes, repeated source failures and exhausted
permission are covered by database-backed checks. CI journey:
`kri-726-background-recovery`; private physical-device evidence remains outside git.

On the iPhone 13 Pro, the approval option and checking UI were observed, a
deliberately incorrect decoded picture was rejected, and repair #1 was issued
with the same recipe digest. The controlled fault exists only in the private QA build. The final repaired export is **pending restored
Xcode UI-testing authorization**; this is not yet a complete phone-flow pass. If the 30-minute permission expires while device access is unavailable, the test must review a fresh approval in the app; the grant will not be extended in the database.

The full native run initially passed 1,802 tests and skipped 24, with one long
slide-video test killed by the simulator. That unchanged test subsequently
passed on a fresh task-owned simulator. The complete clean-simulator run then passed: 1,789 tests, 24 platform skips, no failures. The timeline gate also passed: 209 Jest checks, 28 desktop and 42 mobile browser cases, and 582 API checks.

## Rollout

The feature is not deployed or enabled. Deploy API and workers with enrollment
off, install protocol 13 on the pilot iPhone, then enable the existing
`PHONE_ACTUAL_EXPORT_VERIFICATION_ENABLED` and account allowlist deliberately.
New clients omit repair consent when no offer was shown, preserving older-server
approval compatibility. Disabling enrollment cannot waive an already required
check. Keep this PR draft until the remaining device proof and CI are complete.

This remains the first simple-cut phone adapter. KRI-723's broader preparation
migration, other render adapters, text-only repairs and camera-roll recommendations
remain separate work.
