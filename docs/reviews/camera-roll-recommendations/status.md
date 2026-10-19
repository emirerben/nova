# Camera-roll recommendations: implementation status

19 October 2026. **Partial implementation, discovery off. KRI-729–732 are not
complete.** No release, paid analysis, video export or creator pilot ran.

Worktree: `/private/tmp/nova-kri-729-camera-roll`; branch
`codex/kri-729-camera-roll`; fetched base `b3a2a9edb`.

## Implemented and checked

| Ticket | Implemented foundation | Still required |
| --- | --- | --- |
| KRI-729 | Physical-device metadata benchmark; bounded native collection state with selection changes, coarse time/place grouping, correction preservation, capture-context clearing and account-scoped persistence; revisioned catalogue collection API | Connect the native selection/import/source-binding and correction UI; run resume, limited/revoked permission and unavailable-original journeys on hardware |
| KRI-730 | Durable source/version-pinned visual/speech interval index, complete cached word-timed transcript input, bounded resumable passes, text search and transcript-range reads; exact-generation/size availability checks; explicit partial/unknown/truncated coverage | Real labelled holdout; text-versus-semantic comparison; usable-frame/story ranking; bounded targeted inspection; still-image retrieval and compilation/export proof |
| KRI-731 | Existing approval/render paths left in charge; no new renderer or selection-as-approval shortcut | Recommendation generation, review/correction/selection and prepared-draft integration after the upstream execution train is available |
| KRI-732 | Draft protocol and offline aggregate report CLI, with distinct selection/approval/verified-export outcomes and conservative resource/sample gates | Freeze final protocol/caps before exposure; obtain participant/run authorization; run the pilot; independently review outputs and record a human go/no-go |

The pure native collection state is not yet wired into the production picker.
The benchmark is an isolated DEBUG screen, not the product recommendation flow.
`MEDIA_LIBRARY_ENABLED` and `MEDIA_COLLECTION_PROPOSALS_ENABLED` must both be on
for the new API. No capability or rollout defaults were changed.

## Physical measurement

The creator selected 53 assets on the connected iPhone 13 Pro: 24 photos and 29
videos. Metadata resolved in 33.72 ms, in 32-asset passes; process peak resident
memory was 75,563,008 bytes (72.1 MiB). No media data was requested, uploaded or
analysed. Original-file availability was deliberately not tested. Total system
network traffic was not measured; the zero counters refer to this screen's media
and provider operations. One metadata run is not an end-to-end performance gate.

Safe aggregate evidence, including the exact benchmark source-file hashes:
[`metadata-benchmark.json`](metadata-benchmark.json). Its timestamp is the phone's
reported clock, not a server approval/expiry timestamp. No Photos identifiers,
filenames, coordinates, transcripts or media files were copied from the device.

Initial collection ceiling: 50 assets, below the measured 53 and within the
existing Main Creator media bound. Metadata/index passes are capped at 32.
The existing catalogue quota remains 500. Search returns at most 10 moments,
examines at most 100 indexed candidates and checks at most 10 source objects,
with a five-second deadline per metadata check. These are bounded foundations;
semantic-analysis spend/latency caps still require the end-to-end baseline.

## Verification

- API: 46 collection/pilot and existing library ingest/reuse/analysis checks.
- Native: four collection-state checks, two existing original-store checks and
  one UI check passed, with no skipped tests. Final XCTest bundle:
  `.dev/native-collections-final.xcresult`; inspected baseline screenshot:
  `.dev/native-screens/ED7AD10F-2473-4586-A951-BD7C77E4C510.png`.
- Migration: fresh application, refusal to discard authored collection choices,
  empty-database downgrade, and re-upgrade verified against the disposable
  `nova_kri_729_camera_roll_test` database.
- The API checks use real PostgreSQL and HTTP with deterministic provider/storage
  substitutes. They do not establish model quality or a successful video export.

Repeat from this worktree:

```bash
bash scripts/dev/test-db.sh
bash scripts/check-api.sh test \
  tests/integration/test_media_collections.py \
  tests/integration/test_camera_roll_pilot_cli.py \
  tests/integration/test_media_library_ingest.py \
  tests/integration/test_media_library_reuse.py \
  tests/integration/test_media_library_analysis.py -q
```

Native baseline command (fresh result directory each run):

```bash
bash scripts/ios/generate-project.sh
result_dir="$(mktemp -d .dev/native-collections.XXXXXX)"
xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria \
  -configuration Debug \
  -destination 'platform=iOS Simulator,id=2B6B9FE1-9C1D-4893-9480-6BC799A10C65' \
  -derivedDataPath "$PWD/.dev/ios-build" \
  -resultBundlePath "$result_dir/results.xcresult" \
  -only-testing:KriaTests/CameraRollCollectionTests \
  -only-testing:KriaTests/MediaLibraryOriginalsTests \
  -only-testing:KriaUITests/CameraRollBenchmarkUITests \
  CODE_SIGNING_ALLOWED=NO test
```

The physical benchmark was installed as `com.kria.app.camerarollqa`, separately
from the installed Kria app, using the existing development identity/wildcard
profile. Launch argument: `-camera-roll-benchmark`. The QA screen uses a fixture
API, not a real account or production upload connection.

## Blocking integration boundary

Linear currently lists KRI-721–725, KRI-727 and KRI-728 as Backlog. This base has
the existing revisioned drafts, render contracts and KRI-674 retry path, but not
KRI-722/723's prepared execution/source-window authority. A recommendation cannot
promise exact windows and then let a downstream planner choose them again.
Implementing that execution train would expand the authorized four-ticket brief.

Resume with those prerequisite implementations/evidence available. Then connect
the collection UI and semantic/inspection service, prepare story candidates using
the common snapshot, verify native exports, and execute the consented pilot under
the frozen [protocol](pilot-protocol.md). Never substitute this text baseline's
synthetic integration checks for the held-out retrieval or requested-edit gates.
