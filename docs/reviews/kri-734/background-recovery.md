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

The live chat feed yields to `DeviceRenderPanel` while
`outputVerificationPending` or `repairAttempt` is present. The feed previously
treated a finished local export as finished planning and masked the existing
checking and repair messages. Ordinary preparation and export still use the
live feed; measured checking and automatic recovery show their actual status.

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

On the iPhone 13 Pro, the complete UI flow passed using retained recent account
originals in a separate QA bundle and the real API/PostgreSQL/Celery pipeline with
local object transport. The original two-repair permission was reviewed and tapped.
A private QA-only decoder fault produced a wrong picture that persists on a plain
retry. Kria hid the candidate during checking, rejected it, displayed **Repairing
your video (1 of 2)**, normalized the affected original and exported again. All
13 actual-output checks passed before **Ready**. The correction took 7.91 seconds
in this short-edit case; this is a single test, not a latency guarantee.

Every authored recipe field remained equal; only the capability set's serialization
order differed, and the canonical recipe digest stayed identical. The copied phone
movie's SHA-256 equals the checked uploaded movie's SHA-256. Two extra completion
observations returned `ignored` and did not create new chat events. No model ran.
The earlier permission expired during device-access loss and correctly stopped;
the test reviewed a fresh approval in the app, without extending database grants.
The fault hook is absent from committed product code. The private XCUITest result
bundle, four state screenshots, movie copies and full reports are retained under
`/private/tmp/kri726-qa`; private footage and call-overlay screenshots are not in git.

The full native run initially passed 1,802 tests and skipped 24, with one long
slide-video test killed by the simulator. That unchanged test subsequently
passed on a fresh task-owned simulator. The complete clean-simulator run then passed: 1,789 tests, 24 platform skips, no failures. The timeline gate also passed: 209 Jest checks, 28 desktop and 42 mobile browser cases, and 582 API checks.

## Rollout

PR #1663 deployed as `be0dcc23863c3a9d68a552392aa17fb93786c3af` with enrollment
off. After installing protocol 13 on the pilot iPhone, the existing
`PHONE_ACTUAL_EXPORT_VERIFICATION_ENABLED` flag and explicit account allowlist
were enabled for one account. API and worker settings matched, all six managed
machines retained the checked image, and the production health check passed.
New clients omit repair consent when no offer was shown, preserving older-server
approval compatibility. Disabling enrollment cannot waive an already required
check. Other accounts remain unenrolled.

On October 19, a real-account production run on the iPhone 13 Pro rejected an
injected wrong picture, normalized the affected original and passed all 13
actual-output checks before publication. The original two-repair permission
spent one attempt; recovery took 31.40 seconds in this single short-edit test.
The canonical recipe and source-receipt digests were unchanged, the repair
recorded zero model calls, and the copied phone movie matched the checked upload
byte for byte. The injection existed only in a private, exact-job-scoped build.

That run exposed the live-feed status masking described above. The follow-up
client displayed **Checking your video** with Play, Save and Share absent. A
later preparation attempt also stopped before export because its planner text
omitted the saved title's final period. The strict text guard remained intact;
this separate preparation mismatch is not repaired by source normalization.
Complete production background/resume and repair-state evidence is tracked
separately from the successful backend recovery and isolated QA proof.

This remains the first simple-cut phone adapter. KRI-723's broader preparation
migration, other render adapters, text-only repairs and camera-roll recommendations
remain separate work.
