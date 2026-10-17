# Creator media library

KRI-384 adds a creator-owned catalogue behind `MEDIA_LIBRARY_ENABLED=false`.
Collection proposals have a separate `MEDIA_COLLECTION_PROPOSALS_ENABLED=false`
capability. Backend ingest and reuse are additive. iOS exposes **Saved uploads**
inside Add media for footage, visuals/slides, voiceovers and songs when the native
`media_library` creation capability is true. Collection proposals remain a later
slice. Keep both flags off until native evidence and pilot approval are recorded.

New native uploads are catalogued automatically while the library flag is on.
Uploads made before activation are not automatically backfilled. Operators can
adopt retained, provably owned uploads with `scripts.backfill_media_library`
(below); importing them again also uses the verified intake. The picker supports filename
search, pagination, analysis retry, archive, and restoring expired cloud copies
from the exact original. Archive hides future selection and preserves posts.

Videos retain full-quality originals under account-scoped Application Support.
A new post gets an independent media ID and original-file link, with copy fallback.
Originals survive deleting the first post, relaunch, and sign-out. A different
account cannot browse them; confirmed account deletion erases the library's local
copies. Existing post-local originals are migrated lazily using server-authorized
usage and a full-file hash. Another phone must locate the matching original for
device-only videos; proxies never become render sources. Cloud-backed originals
are downloaded and verified when needed. Reuse performs no upload; cloud repair
is an explicit action and uploads only the required proxy/original.

Native proof and rollout checklist: `docs/reviews/kri-384-ios/verification.md`.
After deployment and pilot approval, enable `MEDIA_LIBRARY_ENABLED` on API and
workers and release a new TestFlight build containing the picker. Keep
`MEDIA_COLLECTION_PROPOSALS_ENABLED=false`. Turning the library flag off hides
Saved uploads after capability refresh and returns its routes to 404, while
existing post bindings and original files remain intact.

## Historical uploads

From `src/apps/api`, run `python -m scripts.backfill_media_library --email EMAIL
--report /tmp/library-dry-run.json` for a metadata-only preview. After reviewing
the scoped account, availability, ordering and byte count, repeat with `--apply`.
`--max-bytes` defaults to 1 GiB of attempted downloads per run; a run stops at the
budget or the existing 500-active-asset quota. Repeating it preserves completed
work. The limit applies after exact-byte deduplication. It scans at most 5,000
candidate identities by default (`--max-candidates`, maximum 10,000).

Candidates come from owned plan-item footage/audio and registered visual pools.
Upload events/pool timestamps determine newest-first priority, with post creation
time as the legacy fallback. Old unpinned sources adopt the current generation
only after full-byte verification and probing; a changed existing generation is
skipped. Videos require their stored device-bound proxy/original receipt for the
current phone-only creation flow; older cloud-render video originals are skipped
and remain available on their existing posts. Both the database
consumer and the normalized storage namespace must belong to the requested owner.

Each transaction takes the existing owner/reference fence, rechecks the current
consumer and source, and adds catalogue identities and usage references only.
It never rewrites posts, copies/deletes cloud objects, revives archived media or
dispatches AI analysis. Verified bytes are ready to reuse; compatible facts remain
independent and are read through the existing versioned cache. Native device-only
videos still require their matching full-quality originals on the phone. Existing
thread usage lets the shipped iOS client migrate those originals lazily.

The report separates missing, changed, unsupported, unowned, archived, quota and
transfer-budget cases. Keep account identifiers and detailed reports outside Git.
Verification contract: `docs/reviews/kri-384-backfill/verification.md`.

## Upload and repair

Authenticated iOS clients reserve through `POST /me/media-assets/uploads`, upload
with the returned create-only headers, then finalize through
`POST /me/media-assets/uploads/{reservation_id}/finalize`. Reusing a
`client_upload_id` with the same request preserves the reservation; a changed
request is rejected. Finalized reservations never yield another upload URL.
Registration commits before analysis publication. A maintenance reconciler
recovers outstanding publication receipts and abandoned processing.

New phone videos upload analysis proxies with device-declared original hashes
and byte counts recorded separately. Photos and supported songs upload originals.
Finalization downloads the pinned generation, verifies the full uploaded hash
and length, and checks the actual media. Device declarations never prove the
uploaded-byte identity. No automatic video-original backup is added.

Saved uploads keeps the newest 500 active catalogue assets per creator. A verified,
unique import atomically archives the oldest active entry by upload/admission time
(`created_at`, then `id` for ties). Reusing an old upload does not reset its age.
Rotation hides it from future library selection; sources, reusable facts, local
originals and existing post references remain intact. Existing posts keep working
under the normal storage retention rules. No physical deletion runs during rotation.

Reservations do not displace saved items. Up to 500 unfinished uploads are allowed
independently of the active catalogue; failed or abandoned imports do not rotate it.
Owner locking serializes admission and duplicate finalization. An attachment that
rolls back also rolls back its archive changes. Duplicate imports and repairs do
not consume another slot. Existing per-file and per-post limits still apply. Compatible imports coalesce
after verifying the canonical cloud copy. Matching reimports can add a fresh
source to an existing catalogue asset after cloud expiry. To repair at capacity,
pass `repair_asset_id`: original bytes must match their verified identity, and a
video proxy must preserve its stored original binding and proxy contract.
Outstanding repair uploads are also bounded.
New selections prefer the newest available source after repair. Existing posts
keep their explicit source generation; old copies become missing only after a
definitive storage check. Embedded song cover art remains supported.

## Selection and reusable facts

`GET /me/media-assets` supports cursor pages, filename search, kind, processing
state and owned collection filters. Detail includes availability, prior usage and
current expiry warnings. The source access URL endpoint signs the exact generation.
Missing or replaced objects become unavailable; transient storage errors remain
retryable. Retry and archive are explicit POST operations on the owned asset.

Thread, visual-pool and editor attachments accept library asset/source IDs.
The server resolves ownership and immutable sources. Each post retains independent
media identifiers, timelines and editor state. Visual projections require
originals; phone footage keeps an original receipt behind its analysis proxy.
Existing native uploads are registered through the same byte verification. A
duplicate consumer binds the canonical path/generation, with an expiry receipt
for the unused uploaded copy.
If duplicate attachment rejects a whole batch, post changes roll back and a
separate owner-serialized transaction records every abandoned physical upload.
Library-ID-only rejections create no upload receipts. Cleanup rechecks existing
post and editor bindings before claiming bytes. Once claimed, database reference
guards also fence legacy post writers; a failed storage deletion stays fenced and
retryable. Renewing an unused upload URL extends its cleanup deadline atomically;
claimed, deleted or already-bound targets cannot receive a fresh PUT URL.
Native visual binding checks generation, full hash and byte count. Proxies cannot
replace originals during rendering.

Video/image facts use existing analyzers and `MediaAnalysisCache` tiers.
`MediaAssetAnalysis` persists compatible facts beyond temporary cache expiry.
Identity includes creator, verified bytes, kind/role and analyzer, model, prompt
and schema versions. Durable leases coalesce concurrent work and reject late
completions. Transcripts and song beats/lyrics reuse existing transcription and
beat detection; the song seam also supports KRI-425. Library transcription uses
the existing cost ledger with duration pricing and no hidden SDK retries. Cache
hits make no provider call. A live lease remains processing; failed analysis is
retryable. Creative interpretation, corrections and take alignment stay per post.

## Retention and erasure

Archive hides future selection and preserves editable consumers. Catalogue
membership and facts do not pin cloud copies; editable-post references do.
Inactive copies receive warnings after 83 days and become eligible after 90 days
and an actual seven-day warning lead. Catalogue records/facts survive expiry.
Confirmed account deletion removes captured post projections before owner/source
cascades and retains existing durable erasure outboxes. Immediate primary-root
cleanup is best effort; delayed verified sweeps cover both owned roots and late
uploads. Sign-out/local-original behavior belongs to the iOS slice.

## Evidence

Real PostgreSQL tests cover automatic rotation at 500, concurrent admission, rollback,
duplicate finalization, ownership,
cleanup claims, repair, durable facts/version invalidation and shared-post erasure.
Reuse tests record provider counts and check independent edits, archived-source
binding, original hash rejection and two chat bindings without uploads.
Providers/storage are controlled fixtures. Preserve fingerprinted test, migration,
contract and native result bundles. Fixture counts do not replace physical-device
reuse/export before pilot activation.
