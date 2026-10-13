# Creator media library

KRI-384 adds a creator-owned catalogue behind `MEDIA_LIBRARY_ENABLED=false`.
Collection proposals have a separate `MEDIA_COLLECTION_PROPOSALS_ENABLED=false`
capability. Backend ingest and reuse are additive. The iOS library, local-original
migration and collection proposals are subsequent delivery slices. Keep both
capabilities off until native evidence and pilot approval are recorded.

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

The limit is 500 active catalogue assets per creator. Live new-import reservations
consume capacity; owner locking serializes admission and duplicate finalization.
Existing per-file and per-post limits still apply. Compatible imports coalesce
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

Real PostgreSQL tests cover quota races, duplicate finalization, ownership,
cleanup claims, repair, durable facts/version invalidation and shared-post erasure.
Reuse tests record provider counts and check independent edits, archived-source
binding, original hash rejection and two chat bindings without uploads.
Providers/storage are controlled fixtures. Preserve fingerprinted test, migration,
contract and native result bundles. Fixture counts do not replace physical-device
reuse/export before pilot activation.
