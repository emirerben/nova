# Existing uploads → Saved uploads

Failure modes recorded before implementation:

- A legacy row points outside the requested account's owned namespace, or its post belongs to somebody else. Skip it; require both database ownership and an owned, normalized object path.
- A proxy is catalogued as an original. Recover its stored original descriptor from the matching post receipt, validate it, and preserve the device-only boundary. Reject proxies without that receipt.
- An old cloud-render video is visible but cannot create a phone-rendered post: strict device-only dispatch requires a device-bound proxy receipt. Skip video originals without that receipt in this iOS backfill. Existing original videos remain on their posts; native visual-only support is not a substitute for footage reuse.
- A pinned generation has changed, a cloud copy expired, or a source was removed during the scan. Recheck under the shared owner fence; skip changed or unavailable candidates. Unpinned legacy sources may adopt only the verified current generation, with before/after generation checks.
- Duplicate or concurrent runs create catalogue duplicates/references, resurrect archived assets, or exceed quota. Reuse existing admission/reference seams under owner locks; respect archived assets and account capacity.
- Backfill changes an existing post's media, revision, timeline, or storage key. Add catalogue identities/references only; never rewrite or delete existing posts or cloud objects.
- A source is deleted with its first post. Keep explicit references and give the iPhone authorized creation-thread usage so its existing original migration can retain a separate copy.
- Unbounded transfers, provider calls, or long owner locks harm production. Default to a metadata-only dry run; bound candidate count and bytes, process one source per transaction, and never dispatch analysis. Full-byte hashing/probing uses existing verification, with no new cloud upload.
- A partial run is mistaken for completion. Produce per-reason counts, read back catalogue contents, and record actual account-scoped outcomes. More than 500 originals requires an explicit capacity decision.

Verification will use an isolated, migrated PostgreSQL database and real generated PNG/video/audio bytes. Exercise dry run, import, API visibility, second-post reuse, unchanged original posts, rerun/concurrency, wrong owner, stale generation, missing object, proxy receipts, archive and quota boundaries. Storage/provider fixtures must reject every upload and provider call.

## Evidence

- Recorded the user's choice to retain the 500-item limit and prefer newest uploads. Added the quota/order acceptance check first; it failed with the old footage-first order and passed after chronological ordering.
- Four new PostgreSQL journeys passed, exercising metadata-only dry run, verified PNG/H.264 bytes, concurrent import, repeat import, API readiness without AI facts, second-post photo reuse, preserved thread revision/state and footage assignments, original receipts, ownership, stale generation, missing proxy receipt, transfer budget, archive and quota. A legacy cloud-only video was reproduced as incorrectly offered (three catalogue entries instead of two), then excluded for the shipped native-only render contract.
- The 27 existing ingest, reuse and library-route checks also passed (31 total in the final focused run). Scoped Ruff and pre-PR checks passed.
- A production metadata-only scan classified available, unproven-owner, unready and unsupported uploads before byte-level deduplication. No source bytes or AI calls were consumed by the dry run. Account counts and source details remain in the private operator receipt.
- The 41 CI journey/selection checks passed after mapping the new utility and its PostgreSQL journeys to the existing library gate.
- Production apply is account-scoped, checks the uploaded utility's SHA-256 against the tested source, retains the existing 500-active-asset limit, and caps attempted verification downloads at 4 GiB. Detailed account receipts remain outside Git.
