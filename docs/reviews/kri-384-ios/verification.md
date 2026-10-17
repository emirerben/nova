# iOS saved uploads — verification contract

Failure modes recorded before implementation:

- A reused proxy gets mistaken for a full-quality original; export must resolve and verify the original's complete SHA-256 and byte count.
- Deleting the first post, relaunching, signing out, or switching accounts loses/exposes shared originals. Shared originals live in account-scoped Application Support; only account deletion erases them.
- A stale page/search result attaches the wrong item; cancel search tasks and fence responses by query.
- Repeated taps, a revision conflict, or a lost response duplicates an attachment. Keep one pending attachment identity through retries; reread revision on conflict.
- Source expired/deleted, wrong role, full post, unsupported visual type, and library off must produce actionable states and no upload or hidden cloud fallback.
- New video uploads fail to retain originals in the library. Adopt the local original after the authoritative attachment; migration uses only server-owned library usage and verified hashes.
- Shared media edits/removals mutate another post. Bind a new media ID per destination and keep server post/editor state separate.

Automated evidence: native filesystem journeys verify hash checks, reuse after deleting the first post, relaunch, and owner isolation; native picker UI verifies search/empty/error/retry/selection. Existing real-Postgres backend reuse journeys prove immutable ownership and independent post bindings. Build and focused native tests run in the task worktree.

Before pilot activation: on a physical iPhone upload a video and photo, attach them to a second post from Saved uploads, edit/export both independently, relaunch, delete the first post and export the second. Confirm no second PUT for reuse. Test missing-original relink with matching and mismatched files, account switching and account deletion. Keep flags off until this evidence and rollout approval are available.

## Local evidence — 2026-10-16

- Existing backend upload/reuse/facts routes and integration journeys: **40 passed**. Additional library gates and creation-thread regressions: **229 passed**, with a migrated, worktree-specific PostgreSQL database.
- Contract drift check, scoped Ruff, and `scripts/preship-check.sh`: passed. UI selection tests (25), journey gate tests (6), and CI selector tests (34): passed.
- Dedicated iPhone simulator: **9 passed**, including the three existing attachment-flow regressions, in `/private/tmp/nova-ios-library-final.xcresult`.
- Physical **iPhone 13 Pro**: all six focused tests passed in `/private/tmp/nova-ios-library-device-final.xcresult`. These exercise the production API encoder, picker and attachment coordinator against an offline server fixture, account-scoped filesystem reuse, and the production native exporter with a generated video. They are not a production-account upload test.
- Physical exports retained in the result bundle: H.264/AAC, 1080×1920, durations **1.000 s** and **0.500 s**, checked with ffprobe. The second export follows deletion of the first post's original directory. Full-file identity checks reject changed originals and keep a different account from resolving the shared file.
- Device picker screenshot: [saved-uploads-device.png](saved-uploads-device.png).
- No live model/provider calls; settled test spend **$0**. Production flags were not changed.

Repeat native verification from the repository root after `make ios-generate`:

```sh
cd src/apps/ios
xcodebuild -project Kria.xcodeproj -scheme Kria -skipPackagePluginValidation \
  -derivedDataPath /private/tmp/nova-ios-library-build \
  -destination 'platform=iOS Simulator,id=<dedicated-simulator-id>' \
  -parallel-testing-enabled NO CODE_SIGNING_ALLOWED=NO \
  -only-testing:KriaTests/MediaLibraryOriginalsTests \
  -only-testing:KriaTests/MediaLibraryExportJourneyTests \
  -only-testing:KriaUITests/SavedUploadsUITests \
  -only-testing:KriaUITests/AttachmentFlowUITests \
  -resultBundlePath /private/tmp/nova-ios-library-final.xcresult test
```

Remaining pilot checks: a real-account upload → second-post reuse → edit/export in TestFlight; Files-based relink and expired-copy restore through the device picker; HEVC/HDR and long videos. Existing uploads made while the flag was off must be reimported to enter the catalogue. These gaps do not represent paid live-model evidence. Approval is still required to merge/deploy and activate the library; collection proposals remain off.
