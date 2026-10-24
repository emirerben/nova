# Camera-roll readiness, held selection and whole-source context (KRI-733 / KRI-731)

## Saved-card access without regeneration

The held card also needs to remain reachable after a prompt/index cache change.
Previously, **View saved ideas** saved the collection, refreshed its index and
requested the current generation cache. A cache miss could hide an already saved
story before the explicit recovery action was reachable.

`GET /me/media-collections/{id}/stories` now reads at most 20 existing owned rows
for the exact current collection revision. Optional bounded story IDs retain the
requested card order; missing or foreign IDs refuse the read. Stored copy,
moments and rejection state remain intact. This endpoint does not resolve media,
re-index, claim a model cache, generate, log exposure, or write the database. It
does not claim that the historical card represents current collection coverage.
Selection and approval still check current story/source/execution identities.

The native saved-card action uses that GET directly and omits automatic exposure
writes for historical reads. Old/malformed servers fail honestly without a POST
or generation fallback. Finding new ideas remains a separate action with fresh
partial-coverage consent; opening an old partial card grants no new consent.
This does not change research/provider caching or implement the separate #1807.

The eight isolated HTTP read cases first failed with 405, then passed with only
SELECT statements recorded and indexing/generation/source resolution forbidden.
The combined saved-read → stable selection → explicit held recovery → real
worker/approval/dispatch fixtures pass 66 cases. The expanded focused backend and
free replay suite passes 2,394 tests (15 skips, seven expected failures); counts
overlap. The new native read-only and old-server controls failed against the old
path before wiring the GET. Native recovery verification is recorded separately;
synthetic fixtures do not establish original-session or physical-phone success.

Native evidence: `.dev/saved-read-final-ui.xcresult` has 17 passing regression
cases and one failed recovery fixture case. Its retained hierarchy shows the
full-quality-original error; the fixture reused a fixed project with different
encoded source bytes. `.dev/saved-recovery-fixture-binding-diagnosis.json`
records the exact old/new hashes and retained binding. The fixture now derives
its stable project identity from the source fingerprint, preserving immutable
binding checks. The corrected native saved-card → hold → explicit recovery →
project handoff passes in `.dev/saved-recovery-bound-source.xcresult`. The new
synthetic source preparation has its own readiness wait; existing card/recovery
screen waits and product gates remain unchanged. These are complementary runs,
not a claim that the failed bundle was entirely green. Earlier read-path red
controls and both failed fixture attempts remain retained.


## Problems and causal evidence

A selected camera-roll idea completed with “I need to establish the requested
changes before offering approval.” No draft/approval/Job was created. The special
planner omitted brief requirements and the resolved readiness manifest; its
server-generated selection message was not an open creator request. Production
readiness correctly held the incomplete handoff. Earlier execution fixtures had
disabled brief/binding settings. A zero-I/O assessment and a real HTTP/SQL/worker
fixture reproduced this before repair. The incomplete integration arrived in
#1685 after #1683 added readiness; #1747 capacity/multiple ideas and #1772 resume
changes did not repair it. Stable selection identity then replayed that completed
hold forever.

A separate excluded source had completed matching clip/transcript analysis but
no timed events: 8.166667s, global description/understanding, empty notable/best
moments, no transcript words/segments and zero source moment rows. The coordinator
identified its exact source/generation/spec read-only. This was not a pending
queue or the historical failed story-query row. The extractor accepted only
localized visual moments or speech, so a valid no-events result was unusable.

This PR repairs those boundaries locally. Original account data, selected ideas,
production indexes and the original held session were not changed or retried.

## Acceptance recorded before each repair

- Keep production readiness/brief gates enabled. Bind reviewed source windows,
  order, text, sound, research and caption to immutable preparation and an ordinary
  expiring approval. Selection never grants render consent.
- Changed collection/story/source generation, prepared execution/copy/evidence or
  approval identity refuses dispatch. Missing evidence cannot use empty delegation,
  a generic title-word check, relaxed capability checks or an open-request bypass.
- Posting caption remains exact, including outer/internal whitespace, newlines
  and explicit empty text, without generated fallback or extra on-screen copy.
- Ordinary duplicate selection remains replay. A separate creator action may
  recover only the completed empty-brief/pre-draft hold, with unchanged project
  and media, no new messages, drafts, approvals, prepared executions or Jobs.
  Concurrent/lost-response retries produce one successor and one Job after consent.
  Cancelled/other refusals and used projects cannot be blindly requeued.
- Native recovery cannot treat an old server's HTTP200 replay of the original
  held turn or a malformed successor identity as success. Valid distinct
  successors/replays remain asynchronous; no wait for rendering is required.
- Completed no-events facts may support explicitly scoped whole-source context,
  never an invented scene/highlight or guessed timestamp/location. Require matching
  completed video AND transcript facts, verified finite positive bounds, ownership,
  source/hash/generation/spec and existing quality/availability/admission checks.
  Preserve the 1–20s candidate and 60s story limits; never silently shorten a longer
  source to make it fit. Unsupported timing remains honest and creator-reviewable.
- Whole-source scope, uncertainty, bounds, identities and cached quality provenance
  must reach approval and frozen execution, not just the candidate screen. Changed
  or missing context evidence must refuse. Metadata is optional/excluded for old
  timed stories; adding context during index refresh must preserve accepted windows,
  caption and approval. A concise review note is sufficient, not a warning wall.
- Distinguish missing matching analysis from timing review, including absent duration
  while analysis is pending. A mixed collection retains the existing explicit
  “Use ready videos” choice. No silent partial consent or whole-collection blockade.
- Inspect actual synthetic native exports and controls for wrong window, sound and
  source order. Do not drop text, relax thresholds or substitute a preparation-only
  fixture for output evidence. No original phone/media operation or provider retry.

## Implementation

The selected-story planner supplies a concrete selection requirement, full prepared
execution digest and resolved manifest. Readiness recompiles and checks exact
strategy, source generations and prepared evidence. Ready accepted requirements
also get checked receipts, matching enforced IDs and no unresolved accepted IDs.
Approval/installation verify the same content-plan identity. Caption lookup prefers
accepted story copy and suppresses speculative caption generation, preserving blank
and whitespace-only choices. Generic missing-brief guards remain unchanged.

The endpoint reads immutable source events/turns even if selection telemetry was
lost. “Try this idea again” is a separate native action tied to the held turn and
expected current revision. Recovery revalidates source preparation and submits one
deterministic successor, retaining the original completed turn. It requires a new
ordinary approval. Replayed successors must have the exact recovery/story binding.
The native API and view both require a valid distinct successor before clearing the
prompt/navigation; lost-response retries retain the same successor identity.

Indexing now adds a `whole_source_context` evidence subtype inside the existing
visual/speech storage contract when there are no timed moments but matching facts
are complete. Existing timed interval extraction, index spec and identities remain
unchanged. The context contract has its own `whole-source-context-v1` marker.
Bounds come from cached source-probe duration and asset duration; at most one 30 fps
quantization frame may differ, and the conservative existing bound is retained.
The full verified source must fit 1–20s. No subwindow/highlight is fabricated.
Known probe-only/stub results, invalid/conflicting timing, missing/stale facts and
unavailable generations cannot become usable context. Existing quality/visibility
notes remain fallible evidence; darkness or stillness is not an automatic defect.
Cached context is refused if its bounds later change.

`evidence_scope` reaches retrieval, story candidates and native details. Cached
whole-source descriptions remain explicitly unlocalized/uncertain. Both relevance
and story prompts explain this distinction; their prompt versions are bumped to
2026-10-24.1 and free replay evals pass. Source descriptions do not silently become
on-screen text. The approval metadata stores typed `source_contexts` with source,
asset, moment, generation, fact/index identity, bounds, uncertainty and quality.
The selected-story digest binds it; approval/installation compare it with current
and stored evidence. The review says “Whole clip; no timed highlight detected,”
plus a short cached-quality note when applicable. Empty metadata is excluded for
legacy timed preparations, retaining their digest contract.

Native indexing separates missing analysis and unusable timing. Their union drives
ready-video counts and explicit partial consent; changed coverage requires a new
choice. The remaining clip stays visible for review. Completed valid short context
is included normally. Unsupported long/unverified sources need a usable window;
this slice adds no new window-picker/planning UI. Generated shared contracts are
updated; only a generated browser type mirror is contract maintenance. No web-editor
implementation or browser testing is included.

## Executed regression evidence

Retained red runs establish the original enabled-gate hold, stable-selection replay,
missing approval provenance, original-turn/malformed native recovery responses,
missing ready-video action for timing-review collections, and pending analysis
misclassified when duration was absent. Their corresponding positive paths pass.
The responsible planner/readiness/approval/dispatch code is real; only provider and
storage transports are replayed. All DB/media data in this lane is synthetic.

- Focused readiness/binding/plan-feed/collection/taste suites plus mandatory free
  `tests/evals/` replays: **2386 passed,15 skipped,7 expected failures**. No live or
  judge option; no provider/model spend. Command: `bash scripts/check-api.sh test
  tests/kria/test_approval_readiness_postgres.py
  tests/kria/test_natural_delegation_readiness_postgres.py
  tests/kria/test_request_binding_journey.py tests/kria/test_plan_blocks.py
  tests/tasks/test_phone_plan_blocks.py tests/integration/test_camera_roll_execution.py
  tests/integration/test_media_collections.py tests/integration/test_editorial_taste.py
  tests/evals/ -q`. Counts overlap with the following cases.
- **58 execution/diagnosis cases** are included:17 original selection/approval,
  18 recovery,17 completed/pending/context-bound admission, five context approval/
  changed-evidence cases and one completed-facts/no-verified-window diagnosis.
  Exact/empty/whitespace captions, source/story/collection/approval changes,
  prepared window/copy/missing evidence, cancelled/unrelated holds, pending/running/
  completed Jobs, duplicate dispatch, lost telemetry, concurrent retry and disabled
  readiness are covered. No second Job is created by retry.
- An additional targeted compatibility replay completes held selection → new approval,
  then finishes a third member's no-events facts and creates an actual whole-source
  index row. The two accepted timed moments, frozen preparation/caption and approval
  remain unchanged and dispatch succeeds. This is analysis enrichment with unchanged
  collection membership/revision, not regenerating the accepted idea.
- **Eight native simulator tests passed**:three client transport/decoding tests and
  five UI journeys covering whole-source labelling/bounds, explicit timing-review
  partial choice, pending analysis, changed coverage consent and ordinary review/
  correction. The timing-review UI control failed before the fix. Result bundle:
  `.dev/context-ui-green.xcresult`. These are not physical iPhone tests.
- Tools/mobile/OpenAPI generation checks, scoped ruff/format and diff checks pass.
  Preship is required on the committed head; no quality gate is disabled or lowered.

The shared local Postgres server hit its client limit during a preliminary matrix;
the pending fixtures also initially used an invalid fact status. Those harness
issues/logs are retained. Final verification uses a separate task-local PostgreSQL
server on 127.0.0.1:55489; no other connections or databases were changed/stopped.

## CI inventory correction

The first CI run of the inclusion commit failed in `changes / select`: the two
new UI methods were not registered in the existing creation-group inventory.
The selector correctly fell back to full coverage, causing four focused-selection
contract assertions to fail and dependent API/native gates to stop. Supported
Codex-app diagnostics supplied the actual failed job120878608090/run40096017730;
the bounded log was truncated but included all four failure tracebacks. No CLI
CI/log fallback or manual remote rerun was used.

The follow-up adds exactly those two method names to
`scripts/ios/ui-test-groups.json`. It changes no selector algorithm, thresholds,
required checks or existing inventory entries. Local selector tests reproduce the
four failures before registration; afterward all118 CI contract tests and117 iOS
orchestration/inventory tests pass. Fresh source CI is required after publication.

## Actual native output and binding

Two retained output families use the production AVFoundation exporter on the Mac,
with the native engine tree **5921e31a21c932455b679e9d124c873376ca9599**. No engine,
renderer fallback, text removal or threshold relaxation was needed. The first
harness lacked compiler-pinned fonts; it now copies and verifies their fingerprints.
A sandboxed text rasterization error then passed with Mac graphics access and the
same engine/text/thresholds. Failed logs are retained. Swift build/cache paths are
inside the task.

The initial two 180s timed-source family produced a correct 14s export (25 checks)
and wrong-window/audio/order controls, all correctly classified. Correct MP4:
`4be845a79f5d9606a1ecb379e392f00a8d507e9d76e39bc705189f4810d03ac3`.
Bundle: `.dev/qa-story-timed-native-proof-host/`.

The inclusion family uses an actual 8.166667s synthetic short file and a 180s file.
The recipe uses the long file 155–162s, then the ENTIRE short file 0–8.1666666667s at 1x.
Actual output is 15.166667s, H.264/AAC,1080x1920. Bundle:
`.dev/qa-whole-source-native-proof/`.

| Actual synthetic output | Result | MP4 SHA256 |
| --- | --- | --- |
| Correct whole-source inclusion | checked,27 checks | 8ec9e2102be438a7357312d327a4cf75e0f9a830f6206cf9a3cca9bc022e1b8c |
| Wrong first source window | rejected,27 checks | 5fc3d2e9ab61723cc2db3efd785fba02b0e1a2eb88a6d78013f4c81d6912e922 |
| Source sound wrongly muted | rejected,27 checks | 3f5d4b266b0a44ef50a8b3a1e27de92419a1d05d3cacce9f0504a9b0689bfa7b |
| Wrong source order, valid windows | rejected,27 checks | 8c01990923656149a80dcd8935023ffbffb4451fd46bdfe900cb5bea36e9213e |

Wrong-order controls preserve valid bounds on the short source, so rejection comes
from actual wrong picture/audio, not an exporter admission error. Both text layers
are retained. Actual frames were inspected: yellow opening with the reviewed text,
then red short-source footage; wrong-order output opens red with the same text.
The coordinator independently inspected the MP4s and actual source durations.

Source hashes: short **7ff451c6be7ff559e775c4607ad86605b32e6c9f0c34d1ead52b618890739c37**
(87158 bytes); long **10bc5d3d5585ea3791dd63ca4d0b0a58d15f5c85c8de847d2536e61d9c8b7194**
(1875648 bytes). The actual exported recipe file SHA256 is
**30178966e84f12aef523ba7e4a39d24aed27760d5645855deafb558ea16f7924**.
The current compiler produces the identical canonical recipe:
**8d53bb52c4f56512baf65d5ae0e5996a14fb5c2e6ce2b26642dd271b766f04db**.
Canonicalization changes only JSON object-key order and the required-capabilities
set order; every ordered clip/text/audio list, identity and value is retained.

A fresh metadata-bound HTTP/SQL approval fixture also proves adding/dropping the
provenance outside the snapshot changes no native recipe bytes (the stripped copy
is compiler-only and is never approved/persisted; the approval control refuses it).
Receipt: `.dev/qa-whole-source-bound-copy/metadata-render-equivalence.json`.
Binding receipt: `.dev/whole-source-render-binding.json`. Thus no duplicate exports
were needed for the later metadata-only repair. Independent fixture runs use new
identities; this is composed integration/compiler/output evidence, not a claim that
one physical app journey or original account session was exercised.

Host export command: `KRIA_AUDIO_PARITY_DIR=<proof-directory>
CLANG_MODULE_CACHE_PATH=<task-cache> SWIFTPM_MODULECACHE_OVERRIDE=<task-cache>
swift test --package-path src/apps/ios/Packages/KriaMediaEngine
--scratch-path .dev/native-proof-build --cache-path .dev/native-proof-cache
--config-path .dev/native-proof-config --security-path .dev/native-proof-security
--disable-sandbox --filter AudioParityFixtureTests`.
Compare: `src/apps/api/.venv/bin/python scripts/ios/camera-roll-export-proof.py compare
--output .dev/qa-whole-source-native-proof`. Inputs come from the real frozen fixture,
with synthetic media generated by the same harness and an 8.166667s FFmpeg cut.

## Limits and remaining acceptance

This is a local fix. The original production hold/index/media are untouched. No
physical iPhone upload → several useful ideas → approval → export → editable
Save/reopen journey has been verified here. Real creative quality, TestFlight
availability and rollout remain open under KRI-731/KRI-733. Unsupported long or
unverified sources still require a usable creator window; no highlights or timing
are fabricated to fill that gap. No paid/provider calls, account flags, production
memory, merge or deployment were performed. Main4499189663c31e1274ff249a9afc53cd2cdc75d5
is integrated additively; other authors' freeze-frame/editor work is preserved.
