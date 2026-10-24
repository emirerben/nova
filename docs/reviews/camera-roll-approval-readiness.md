# Camera-roll selection, approval and held-selection recovery (KRI-733 / KRI-731)

## Original incident and root cause

Choosing an existing collection idea completed with “I need to establish the
requested changes before offering approval.” No draft/approval/Job was created.
The server-generated selection message was not a generic open request, but the
special selected-story planner supplied neither a brief requirement nor its
resolved readiness manifest. Common readiness correctly held the incomplete
handoff: brief version0, no requirements, null brief/prepared evidence digests.

A zero-I/O assessment and the real HTTP/SQL/worker fixture reproduced this before
repair. Earlier fixtures explicitly disabled production brief/binding settings.
The incomplete integration arrived in #1685 after #1683 added readiness; #1747's
capacity/multiple ideas and #1772's resume changes did not repair this boundary.
Previous simulator evidence never established the full physical iPhone journey.

## Acceptance recorded before implementation

- Production brief/readiness gates remain enabled. Selecting an owned idea binds
  the accepted source windows/order, text, audio, research and caption to one
  immutable preparation and an ordinary, expiring approval. No implicit render.
- Changed story/collection/source generation, prepared windows/copy/evidence or
  approval identity refuses dispatch. Missing evidence cannot fall back to a
  generic title-word check or empty delegation. Normal readiness remains enforced.
- The accepted posting caption stays exact, including whitespace/newlines and an
  explicitly empty caption. It cannot trigger generated fallback copy and does
  not become an extra on-screen text requirement.
- An ordinary duplicate selection stays a replay. A separate creator action may
  recover only the exact completed empty-brief/pre-draft hold, without modifying
  that turn. Equivalent native attachment retries remain harmless. Changed media,
  messages, drafts, approvals, cancelled/other refusals or historical/current Jobs
  must block recovery. Concurrent/lost-response retries return one successor.
- A mixed-version server's HTTP200 response containing the original held turn, or
  an invalid successor identity, is not recovery. A valid distinct successor may
  open the saved project while planning is still asynchronous.
- Export synthetic timed footage through the production native exporter. Compare
  correct output with wrong-window, wrong-audio and wrong-order controls without
  dropping text, loosening checks, touching original media or calling providers.
- Diagnose completed-analysis/no-timed-interval sources separately from pending
  analysis. Do not retry providers or fabricate highlights to satisfy indexing.

## Implementation

The planner supplies a concrete selected-story requirement, complete prepared
execution digest and resolved manifest. Readiness recompiles the exact snapshot,
strategy and attached source generations; approval/installation recheck the same
content-plan identity. Accepted requirements get checked receipts, matching
enforced IDs and no unresolved accepted IDs. Generic missing-brief guards remain.

Caption lookup prefers the accepted story over generated/variant fallback.
Preservation includes outer/internal whitespace, blank captions and newlines;
speculative caption generation is skipped for that explicit choice.

The selection endpoint reads the immutable source event/turn even if selection
telemetry acknowledgement was lost. It exposes recovery only for the specific
historical empty-brief hold with unchanged current thread/media, no creative
brief, draft, approval, prepared execution or Job. The native review screen offers
“Try this idea again” as a separate tap. Recovery checks the expected revision,
revalidates the source snapshot, and submits one deterministic successor tied to
the original turn. The original turn stays completed. The ordinary worker must
prepare a fresh approval; recovery itself grants no render consent. Replayed
successors must retain the exact immutable recovery/story binding.

New optional recovery fields are mirrored by the shared contract generator. The
single generated browser type mirror is contract maintenance; no web-editor
implementation or browser testing is included.

## Executed integration and native evidence

The retained enabled-gate red fixture completed without approval before the
handoff fix. The recovery regression then reproduced the separate stable-event
problem: duplicate attachment/selection requests returned the original hold.

- `bash scripts/check-api.sh test tests/integration/test_camera_roll_execution.py -q`
  — **36 passed**:17 original selection/approval cases,18 recovery cases and one
  completed-analysis/no-interval diagnosis. Provider/storage transport is replayed;
  the responsible planner, readiness checker, approval and dispatch code are real.
- Recovery cases cover exact/empty/whitespace captions; repeated native attachment
  HTTP requests; concurrent retries; missing selection telemetry; changed thread,
  media, story, collection and storage generation; prepared/approved drafts;
  pending/running/completed Jobs; unrelated refusals; cancelled turns; reused
  recovery identity and disabled readiness. Successful cases reach a new approval
  and one Job after explicit consent; duplicate dispatch creates no second Job.
- Simulator app compilation succeeded with Xcode's generic iOS Simulator target.
  This is compile evidence, not a physical app or UI journey claim.

Synthetic sources were generated by `scripts/ios/camera-roll-export-proof.py
synthetic`:180-second red/blue clips invert after100s and change audio frequency
each second. The accepted recipe contains two7-second windows starting at155s,
with opening/closing text. Inputs and detailed reports are retained privately in
`.dev/qa-story-timed-native-proof-host/`; no original uploads were used.

The first export failed because the proof harness omitted compiler-pinned font
assets. The preparer now copies and verifies the exact bundled font fingerprint.
The next sandboxed export failed in active-text rasterization. The same production
engine/recipe, unchanged text and thresholds passed with Mac graphics access
outside that sandbox, with task-local Swift build/cache paths. Both failed logs
are retained. No renderer source change, fallback or title removal was used.

Host command: `KRIA_AUDIO_PARITY_DIR=<proof-directory>
CLANG_MODULE_CACHE_PATH=<task-cache> SWIFTPM_MODULECACHE_OVERRIDE=<task-cache>
swift test --package-path src/apps/ios/Packages/KriaMediaEngine
--scratch-path .dev/native-proof-build --cache-path .dev/native-proof-cache
--config-path .dev/native-proof-config --security-path .dev/native-proof-security
--disable-sandbox --filter AudioParityFixtureTests`.
Comparison: `src/apps/api/.venv/bin/python scripts/ios/camera-roll-export-proof.py
compare --output .dev/qa-story-timed-native-proof-host`.

| Synthetic actual output | Comparison | MP4 SHA256 |
| --- | --- | --- |
| Correct14-second native export | checked,25 checks | 4be845a79f5d9606a1ecb379e392f00a8d507e9d76e39bc705189f4810d03ac3 |
| First window wrongly starts at0s | rejected,25 checks | ceddfc38b9191d2e4307d10ae0f72113c1be5ecd5662d64619233dd3a95ae1aa |
| Source sound wrongly muted | rejected,25 checks | ac91e3f7aada17c7a02e7023da42d95f312e1a277250f2fbb0d54a3eac9feeda |
| Source order wrongly reversed | rejected,25 checks | 356b4beea56e230a9c98e36e3a2cce7d7a353c2a2f8fa0de65a49c9f81fc3cf0 |

Picture and audio controls fail independently in the actual-output verifier.
The correct opening frame was visually inspected: late-window yellow footage
with “Where does this crossing lead?”; the wrong-window frame is blue with the
same text. The coordinator independently inspected the retained MP4 and confirmed
both text layers remained in the host recipe. This establishes synthetic recipe
fidelity, not realistic content quality or physical iPhone fulfillment.

## Excluded-video diagnosis and remaining acceptance

The coordinator identified the actual excluded selected source read-only:8.166667s,
matching current READY clip metadata and READY transcript, exact source generation,
no notable/best moments, no transcript words/segments, and zero usable intervals.
There are no source moment rows. This is completed analysis with no timed evidence,
not a pending queue or the historical failed story-query row. The regression keeps
both facts READY and the source unindexed without provider retry or invented timing.

The current UI conflates unindexed sources with “analysis isn't ready.” This PR
has not made that source usable or repaired the wording. A faithful inclusion
path, such as explicitly creator-selected bounded footage, remains separate work;
a wording-only change would not fulfill clip inclusion.

This is a local fix. No original production selection was recovered or changed;
no physical upload → multiple ideas → approval → export → editable Save/reopen
journey has been verified here. Creative quality, actual TestFlight availability
and rollout remain open under KRI-731/KRI-733. No paid/provider calls, account
flags, production memory, original media, deployment or merge were performed.
Main4499189663c31e1274ff249a9afc53cd2cdc75d5 remains integrated additively;
other authors' freeze-frame/editor work is preserved.

## Final continuation verification

- The focused readiness/binding/plan-feed/collection/taste suite now passes
  **201 tests**, including the36 execution/diagnosis cases above (counts overlap).
  Command: `bash scripts/check-api.sh test tests/kria/test_approval_readiness_postgres.py
  tests/kria/test_natural_delegation_readiness_postgres.py
  tests/kria/test_request_binding_journey.py tests/kria/test_plan_blocks.py
  tests/tasks/test_phone_plan_blocks.py tests/integration/test_camera_roll_execution.py
  tests/integration/test_media_collections.py tests/integration/test_editorial_taste.py -q`.
- Three native client transport/decoding tests pass on a separate iOS26.5
  simulator: original-turn HTTP200 and malformed successor responses are refused;
  distinct successor/replay responses preserve asynchronous planning. The two
  refusal controls failed before adding the client guard. The review-screen
  handler also checks the successor before clearing its prompt or navigating.
  Command: `xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria
  -destination 'platform=iOS Simulator,id=<separate-test-simulator>'
  -parallel-testing-enabled NO -only-testing:KriaTests/CameraRollRecoveryTests
  CODE_SIGNING_ALLOWED=NO test`, with task-local derived data/package paths.
  Result bundles: `.dev/recovery-client-red.xcresult` and
  `.dev/recovery-client-green.xcresult`; this is client integration, not a UI tap
  or physical phone journey.
- `python -m app.cli.kria_contracts --check`: tools/mobile/OpenAPI match.
  Scoped ruff, format and `git diff --check` pass. Normal preship checks remain
  required before publication; no gate is disabled or lowered by this change.
