# Plan 024 — Native editor component parity

Status: IN PROGRESS
Planned at: `7c444a47e`
Date: 2026-09-08
Source audit: `.audit/mobile-editor-parity/report.md` in the `codex/kria-ios-native` worktree

## Goal

Make the native iOS editor a component editor rather than a clip editor with global treatment controls. Every persisted editor object must be represented, time-correct, selectable from its timeline lane or the preview, editable when the server capability permits it, and explicitly read-only when it does not.

The concrete regression is overlapping native preview text: the current snapshot mapper drops timing, the preview renders every text layer at every playback time, hit testing is disabled, and the timeline collapses all text into one full-duration bar. The fix must repair the document and interaction model once, then use that foundation for captions, music, SFX, overlays, visual blocks, motion scenes, camera effects, and carousel moments.

## Success criteria

- A two-text fixture with non-overlapping windows never displays both texts together.
- Selecting any supported object in the timeline seeks to its start without autoplay, selects the same preview object, and routes to the matching inspector.
- Tapping the preview selects the topmost active object; repeated taps at the same point cycle overlapping objects deterministically.
- Every server-supported editor section is decoded and represented. It is either editable or visibly read-only with the server-provided capability reason.
- A no-op load/save preserves unknown root keys, unknown section keys, unknown item fields, opaque records, array order, and omitted-vs-empty-vs-explicit-null semantics.
- Editing one lane sends only that dirty section and leaves every untouched lane byte/AST-equivalent after reload.
- All timeline objects have stable accessibility identity, selected state, readable name and timing, and a non-gesture editing path. Interactive targets are at least 44×44 points.
- The 43-source/71-slot/40-caption stress fixture stays responsive and does not rebuild AV playback composition on clock ticks.
- The web ruler/transport duration disagreement is diagnosed and fixed or converted into an explicitly documented dual-duration model with regression coverage.

## What already exists

| Existing capability | Reuse decision |
|---|---|
| `DraftSnapshot.editorDraft` and `EditorDraft.persistedSnapshot` retain the original server envelope as `JSONValue` | Refactor into one lossless codec; do not create a second independent snapshot system. |
| `NativeEditorSession` has explicit begin/update/end clip trim transactions, immutable gesture baselines, one-step undo, save state, and capability loading | Preserve these transaction semantics while replacing clip-only selection and four-section dirty tracking. |
| `NativeMiniStrip` already has a fixed near-left playhead, filmstrip surfaces, zoom, scrubbing, and clip trim handles | Keep the viewport and clip foundation; replace only the full-duration treatment-lane abstraction. |
| `EditorCommitRequest` on the API already atomically validates and persists text, captions, timeline, mix/music, SFX, overlays, visuals, motion, camera, carousel, title, orientation, lyrics, and background music | Mirror this contract on iOS. No new backend workflow or endpoint is needed. |
| Web `useEditorSelection`, `EditorCanvas`, `EditorTimelineBody`, and `editor-commit.ts` implement the intended selection, visibility, lane, and dirty-save behavior | Port semantics, not React structure. Keep SwiftUI state ownership explicit. |
| `KriaMediaEngine` already has bounded history, timeline validation, cancellation, recovery classification, and hardened export behavior | Reuse for local render/export work. Do not duplicate media-engine policy in app views. |
| Deterministic native editor and playback UI fixtures already launch without a production account | Extend these fixtures for all-lane and accessibility testing. |

## Architectural decisions

### 1. One canonical document

Add a dedicated `Core/NativeEditorDocument.swift`. `EditorDocument` becomes the only mutable editor source of truth. `EditorDraft` may remain as a temporary compatibility alias/adapter during migration, but two independently mutable copies are forbidden.

The document owns typed projections for:

- timeline clips and tombstones;
- authored text;
- caption metadata and timed cues;
- primary music, background music, music window, and mix;
- sound effects;
- media overlays;
- visual blocks;
- motion scenes and runtime compatibility;
- camera effects;
- carousel moment;
- lyrics, title, orientation, capability map, and revision metadata.

Every typed record retains its original raw dictionary. Encoding overlays changed typed fields onto the raw record. Unknown records remain in their original position.

```text
Server editor document
        |
        v
 Lossless wire codec --------------------------+
        |                                      |
        v                                      |
 EditorDocument                          Raw envelope
 typed projections                  root/section/item fields
        |                                      |
        +---------------+----------------------+
                        |
                 section transaction
                        |
        +---------------+----------------------+
        |                                      |
 timeline / preview / inspector          dirty section set
        |                                      |
        +---------------+----------------------+
                        |
                 atomic editor commit
                 changed sections only
```

### 2. One cross-kind selection

Replace `selectedClipID` with `EditorSelection { kind, id }`. IDs are strings because the server uses UUIDs, slot IDs, caption-derived IDs, singleton music identities, and arbitrary effect IDs.

Kinds: clip, text, caption cue, visual block, motion scene, camera effect, SFX, overlay, music, and carousel.

Selection is UI state, not document state. Undo/redo restores the document and dirty state but does not unexpectedly move the playhead, zoom, open tool, or inspector route. Selection is preserved by `{kind,id}` after reload/rebase when that object still exists; otherwise it clears deterministically.

### 3. One timeline projection and interaction library

Add pure `Core/NativeEditorInteraction.swift` functions for:

- half-open visibility: `start <= time < end`;
- stable z-order and preview-stage order;
- topmost-first overlap cycling;
- time↔x mapping, clamping, and fit/zoom geometry;
- minimum 44-point hit-region expansion;
- body move and edge trim with immutable baselines;
- interval lane packing and selection remapping.

Views consume a memoized timeline projection. Playback clock updates may change active preview items and the playhead, but must not rebuild document projections or AV composition.

### 4. Capability-aware inspectors

Tool selection controls creation and browsing. Object selection controls the contextual inspector. The inspector route is either `.selection(EditorSelection)` or `.tool(NativeEditorTool)`.

Every object remains visible/selectable even when editing is disabled. Unsupported operations show the exact capability reason in a read-only inspector. Native preview fidelity may be simplified for motion/camera/carousel, but timing, selection, layer order, and persistence may not be approximate.

### 5. Dirty saves preserve server semantics

Expand the native request/response DTOs to mirror the existing API contract:

- omitted section means untouched;
- empty array means replace with empty;
- music removal uses explicit `remove_music`;
- carousel removal sends explicit `null`;
- save always sends the loaded `base_generation`;
- guided revision metadata follows the deployed capability contract;
- success clears only sections acknowledged in the response;
- `ok == false` means the edit persisted but render enqueue failed;
- 409 retains local edits, history, and selection and presents reload/rebase choices. It never silently retries stale content.

## Delivery phases

### Phase 0 — Characterization and fixture harness

1. Add shared deterministic editor fixtures: two-text, boundary/z-order, all-lanes, 71-slot stress, and unknown-section preservation.
2. Extract shared API/URL-protocol test support.
3. Pin current clip trim, playback, navigation, and save/conflict behavior before structural changes.
4. Keep UI tests account-free and network-free through launch arguments.

Gate: current editor tests remain green; fixtures launch reliably on Simulator.

### Phase 1 — Document, codec, selection, and correctness

1. Add `EditorDocument`, typed lane records, capabilities, revision metadata, and raw-envelope preservation.
2. Add the lossless canonical/legacy codec and full editor-commit DTOs.
3. Refactor `NativeEditorSession` to document-wide selection, per-section dirty state, and complete transaction history.
4. Fix preview text/caption visibility, deterministic z-order, selection hit testing, and overlap cycling.
5. Replace the single Text treatment with one timed bar per text item.

Gate: non-overlapping text never overlaps; selection synchronizes across timeline, preview, and inspector; no-op round-trip and one-lane dirty-save tests pass.

### Phase 2 — Core editing parity

1. Complete clip inspector: reorder, in/out/duration, source-window slide, look, transition, and audio capability messaging.
2. Complete authored-text inspector: content, timing, font, size, width, alignment, position, animation, colors, highlight, shadow, stroke, and behind-subject.
3. Decode/render timed caption cues; use a collapsed density strip by default and expand selectable cue bars when Captions is active.
4. Make music selectable and expose track/window/level plus original-audio mix fields.
5. Preserve one undo entry per gesture, no history for no-op gestures, and redo clearing after a new edit.

Gate: every Phase 2 object supports timeline selection, preview selection where visible, inspector edit, undo/redo, save, reload, and conflict recovery.

### Phase 3 — Visual and sound lanes

1. Add timed SFX placement bars and timing/trim/gain inspector.
2. Add timed media overlay bars, time-correct preview, direct position/scale, display mode, z-order, and removal.
3. Add visual-block bars, supported preview, timing/preset/transform inspector, and removal.
4. Replace generic unavailable sheets with browse/create/edit or exact read-only states using server capabilities.

Gate: the all-lanes fixture modifies and removes one item from each lane, saves, reloads, and leaves untouched lanes unchanged.

### Phase 4 — Advanced temporal effects

1. Add motion-scene lane, runtime-hash compatibility, timing/preset inspector, and simplified time-correct preview.
2. Add camera-effect lane and timing/intensity/easing inspector.
3. Add carousel moment representation, derived timing, position inspector, and explicit removal semantics.
4. Add deterministic layer-order editing across overlapping text, visual, and overlay objects.

Gate: every server-supported object is editable or visibly read-only with a named reason; nothing is silently omitted.

### Phase 5 — Web duration discrepancy

1. Trace `durationS`, virtual timeline duration, rendered output duration, transition overlap, ruler ticks, transport maximum, and player metadata for the audited 71-slot variant.
2. Establish the canonical contract: edit-space duration drives bars/ruler only when it maps to the rendered transport; otherwise display the correct rendered duration and annotate genuinely virtual regions.
3. Add regression coverage using the 71-slot/0:30 transport shape.

Gate: ruler, scrub bounds, playhead, and transport communicate one coherent duration model for rendered and virtual preview modes.

## Agent dependency graph

```text
             +-----------------------------+
             | A: fixtures/characterization|
             +---------------+-------------+
                             |
             +---------------v-------------+
             | B: document + wire contract |
             +---------------+-------------+
                             |
             +---------------v-------------+
             | C: session + selection core |
             +---------------+-------------+
                             |
              +--------------+--------------+
              |                             |
    +---------v----------+        +---------v----------+
    | D: preview/timeline|        | E: inspectors/save |
    +---------+----------+        +---------+----------+
              |                             |
              +--------------+--------------+
                             |
             +---------------v-------------+
             | F: P2/P3 lane integration   |
             +---------------+-------------+
                             |
             +---------------v-------------+
             | G: full UI/stress/a11y gate |
             +-----------------------------+

 Independent at any time: H: web duration discrepancy
```

Because all native feature views share `NativeEditorSession`, implementation is intentionally staged. Parallel work is limited to modules with disjoint ownership:

| Lane | Modules | Depends on |
|---|---|---|
| Luna A | iOS Core document/codec and DTO tests | fixtures |
| Luna B | iOS deterministic fixtures and characterization tests | — |
| Luna C | web timeline/player duration logic and Jest tests | — |
| Luna D | iOS session/interaction reducer and tests | Luna A |
| Luna E | iOS preview/timeline views and accessibility | Luna D |
| Luna F | iOS inspectors, save/conflict UI, and integration tests | Luna D; coordinates with E |
| Luna G | iOS P2/P3 lanes and stress/UI verification | E + F |

Conflict rule: only one agent may own `NativeEditorSession.swift`, `NativeEditorMediaViews.swift`, or `NativeEditorView.swift` in a wave. Agents must not cherry-pick shared-worktree commits; the orchestrator reviews and checkpoints each logical unit.

## Test coverage map

```text
CODE PATHS                                      USER FLOWS
[ ] decode canonical + legacy document           [ ] open 71-slot project without a hang
 +-- known typed records                         [ ] timeline tap -> select -> seek, no autoplay
 +-- unknown root/section/item fields            [ ] preview tap -> same timeline selection
 +-- malformed/unsupported item -> read-only     [ ] repeated overlap tap cycles top-to-bottom
[ ] encode changed sections only                 [ ] body drag moves; edge drag trims
 +-- omitted != empty != explicit null           [ ] inspector edit -> undo -> redo
 +-- music/carousel removal semantics            [ ] save -> reload preserves every lane
 +-- raw record merge/order preservation         [ ] 409 keeps local edits and offers recovery
[ ] selection and timing                         [ ] capability-off object stays visible/read-only
 +-- start <= t < end                            [ ] VoiceOver can select/edit every lane
 +-- stable z-order and overlap cycle            [ ] Dynamic Type/Reduce Motion remain usable
[ ] geometry/performance                         [ ] ruler/transport expose coherent duration
 +-- fit/zoom/scroll time mapping
 +-- 44pt invisible hit targets
 +-- 0.1s neighboring items
 +-- cached visible-window projection
```

Unit tests cover pure codec, selection, visibility, geometry, history, and dirty-state branches. XCUITests cover cross-component selection, gestures, inspectors, save/conflict/error recovery, accessibility, and account-free fixture launch. Package tests cover media-engine behavior. Jest covers the independent web duration contract.

Required verification:

```bash
cd src/apps/ios/Packages/KriaMediaEngine && swift test
cd ../../../.. && make ios-verify
cd src/apps/web && npm test -- --runInBand
cd src/apps/web && npx tsc --noEmit
```

Targeted Xcode tests may be run with `xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria -destination 'platform=iOS Simulator,name=iPhone 17' test`. Run one final signed-device pass on a current iOS 18+ device before release.

## Performance budgets

- Pure geometry/hit testing for 1,000 bars: under 50 ms in repeated unit measurements.
- Timeline gesture-to-selection p95: under 100 ms on the reference simulator.
- No more than 20% CPU, memory, or scroll-latency regression from the Phase 1 stress baseline.
- Playback clock updates must not rebuild AVPlayer composition or copy the full raw document.
- Captions stay collapsed except while their lane/inspector is active; only visible/selected bars materialize as SwiftUI controls.
- Use stable IDs, cached lane geometry, and interval-indexed active-item lookup. Keep Canvas for static filmstrip/background painting.

## Failure modes and required handling

| Failure | Required behavior | Coverage |
|---|---|---|
| Malformed or future record | Preserve it as opaque/read-only; never delete it on save | Codec unit test |
| Duplicate/missing item ID | Derive a stable scoped identity without mutating untouched wire data | Fixture unit test |
| Invalid/zero/negative timing | Clamp only for display or mark read-only; do not silently rewrite on no-op save | Timing unit test |
| 409 baseline conflict | Retain local document, dirty state, undo history, and selection; offer explicit reload/rebase | Persistence + UI test |
| Save succeeds but render enqueue fails | Show “saved, render did not start” recovery and keep returned generation | Persistence + UI test |
| 401 during save | Refresh once and retry once; terminal auth failure returns to sign-in without discarding durable server state | API integration test |
| Network/500/malformed response | Keep dirty edits and expose retry; never report saved | API/UI test |
| Capability flips after load | Keep object visible, disable mutation, show new server reason after refetch | Session/UI test |
| Motion runtime mismatch | Preserve/select the scene read-only and name the mismatch | Document/UI test |
| Tiny bars collide | Keep painted widths truthful; resolve through expanded hit regions plus deterministic cycling | Geometry + UI test |
| Clock update causes view churn | Projection cache remains stable; only active-item/playhead state updates | Performance test |
| Web rendered duration differs from edit duration | Use explicit dual-duration model; never imply the ruler is final output length | Jest regression |

No failure may be silent, destructive, or erase an untouched lane.

## Implementation Tasks

- [x] **T1 (P1, human: ~1 day / CC: ~45 min)** — Test foundation — Add deterministic two-text, boundary, all-lanes, stress, and unknown-section fixtures plus reusable API stubs.
  - Surfaced by: Test review — existing tests do not characterize all lanes or raw preservation.
  - Files: `src/apps/ios/Tests/**`, fixture launch wiring.
  - Verify: targeted `KriaTests` and `KriaUITests` fixture launch.
- [x] **T2 (P1, human: ~3 days / CC: ~2 h)** — Document contract — Add the canonical typed editor document, capability/revision models, raw-preserving codec, and complete commit DTOs.
  - Surfaced by: Architecture — current `EditorDraft` and mapper drop timing, styling, and entire lanes.
  - Files: `src/apps/ios/Kria/Core/**`, `src/apps/ios/Tests/KriaTests/EditorDocumentTests.swift`.
  - Verify: canonical/legacy/full-lane/no-op/one-lane round-trip tests.
- [x] **T3 (P1, human: ~2 days / CC: ~90 min)** — State spine — Replace clip-only selection and four-section dirty tracking with cross-kind selection, interaction math, complete dirty state, history, and conflict-safe rebase.
  - Surfaced by: Architecture/code quality — tool and clip state currently compete and cannot identify non-video objects.
  - Files: `NativeEditorSession.swift`, `NativeEditorInteraction.swift`, session/interaction tests.
  - Verify: selection, visibility, overlap, geometry, history, and conflict tests.
- [x] **T4 (P1, human: ~3 days / CC: ~2 h)** — Preview and timeline — Render active timed layers, enable preview hit testing/cycling, and replace treatment bars with cached typed lanes and accessible hit regions.
  - Surfaced by: Regression — all native text currently renders for the full video and only the first item appears in the timeline.
  - Files: `NativeEditorMediaViews.swift` plus extracted preview/timeline views and tests.
  - Verify: two-text regression, selection synchronization, 44pt targets, 71-slot stress.
- [x] **T5 (P1, human: ~4 days / CC: ~3 h)** — Core inspectors and persistence — Ship complete clip/text/caption/music inspectors, selection routing, partial saves, reload, and conflict/error recovery.
  - Surfaced by: Product parity — P1 objects lack complete property editing and durable recovery.
  - Files: `NativeEditorView.swift`, `NativeEditorComponents.swift`, inspector views, services/session/UI tests.
  - Verify: edit/undo/redo/save/reload/conflict flows for every P1 object.
- [ ] **T6 (P2, human: ~4 days / CC: ~3 h)** — Visual and sound lanes — Add SFX, overlays, and visual blocks with timing, preview, inspectors, capability-aware creation/removal, and all-lanes preservation.
  - Surfaced by: Audit — native declares Visuals/Overlays unavailable and has no SFX item model.
  - Files: native document, timeline, preview, inspector, and integration tests.
  - Verify: modify/remove each P2 object and reload unchanged neighboring lanes.
- [ ] **T7 (P2, human: ~3 days / CC: ~2 h)** — Advanced lanes — Add motion, camera, carousel, layer ordering, and honest simplified preview/read-only fallback.
  - Surfaced by: Audit — advanced temporal objects are silently absent today.
  - Files: native document, timeline, preview, inspectors, and tests.
  - Verify: every server-supported object is selectable and editable or explicitly read-only.
- [x] **T8 (P2, human: ~1 day / CC: ~45 min)** — Web duration contract — Diagnose and correct ruler/transport divergence with a 71-slot regression fixture.
  - Surfaced by: Audit screenshot — ruler reaches 0:59 while rendered transport reports 0:30.
  - Files: web virtual timeline/player/timeline components and Jest tests.
  - Verify: targeted Jest plus `npx tsc --noEmit`.
- [ ] **T9 (P1, human: ~2 days / CC: ~2 h)** — Release gate — Run full iOS/package/web suites, simulator stress/accessibility metrics, and one signed-device pass; fix all regressions.
  - Surfaced by: Test/performance review — screenshots alone cannot verify VoiceOver, Dynamic Type, Reduce Motion, thermal, memory, or gesture latency.
  - Files: tests and any defect fixes only.
  - Verify: `make ios-verify`, `swift test`, targeted XCUITests, web Jest/tsc, device checklist.

## NOT in scope

- New backend editor endpoints or a new save protocol: the existing atomic editor-commit route already carries the required sections.
- Pixel-identical native reproduction of server-rendered Skia/FFmpeg motion, camera, or behind-subject output: native must be time-correct and honest about simplified fidelity; final render remains authoritative.
- Replacing AVFoundation/KriaMediaEngine or introducing a third-party timeline framework: existing platform and package seams are sufficient.
- Production/App Store rollout, feature-flag activation, or data migration: this plan produces verified branch code; release remains a separate explicit operation.
- Unrelated web editor redesign: only the observed duration-contract discrepancy is included.

## Engineering review findings

### Architecture

1. **[P1] (confidence 10/10)** `TextLayer` and the snapshot mapper discard timing/style/lane data. Decision: canonical typed document with raw-field preservation.
2. **[P1] (confidence 10/10)** `selectedClipID` cannot coordinate timeline, preview, and inspectors for non-clip objects. Decision: one cross-kind selection.
3. **[P1] (confidence 9/10)** a single large parallel rewrite would create conflicts across three shared SwiftUI files. Decision: dependency-ordered agent waves with single-file ownership.

### Code quality

1. **[P1] (confidence 10/10)** full-duration `treatmentLane` duplicates distinct text/caption/music semantics and hides item identity. Decision: lane registry plus typed bars.
2. **[P1] (confidence 9/10)** current mixed document/UI/save state encourages section drift. Decision: pure codec/interaction files and one transaction coordinator.
3. **[P2] (confidence 9/10)** capability booleans lose operation-specific reasons. Decision: decode boolean-or-object capabilities and render exact read-only explanations.

### Tests

The coverage map above identifies nine required test groups: fixture launch, codec, timing, selection, geometry, history/dirty state, persistence/conflict, cross-lane UI/accessibility, and stress/performance. The existing trim/playback/navigation tests are retained as regression gates.

### Performance

1. **[P1] (confidence 8/10)** rebuilding all lane controls on every clock update will make the 71-slot case stutter. Decision: cached projection, visible-window materialization, and separate clock state.
2. **[P2] (confidence 8/10)** linear overlap scanning and overlapping 44pt targets can make microcuts ambiguous. Decision: interval-indexed candidates plus deterministic cycling.
3. **[P2] (confidence 8/10)** whole-document JSON copying per gesture will inflate memory and history. Decision: section transactions and bounded snapshots.

The user explicitly requested the complete P0–P3 scope. All findings above are folded into the implementation tasks; no architectural decision remains open.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | Not run |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | Running under Codex; nested pass skipped |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 9 issues, 0 critical gaps; all folded into T1–T9 |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | CLEAR | Prior product-design audit produced the source report and phased interaction model |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | Not run |

**VERDICT:** ENG + DESIGN CLEARED — ready to implement in dependency-ordered Luna waves.

NO UNRESOLVED DECISIONS
