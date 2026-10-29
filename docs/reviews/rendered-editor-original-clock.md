# Native editor source clock recovery

## Request and scope

The creator's 29 October 2026 retest produced a playable native output and a saved
Photos copy, but the editor could not read the timeline. The requested recovery is
to reopen the existing project for font, placement and duration edits without
regenerating or changing original media. This is separate from the earlier
approval gate and from full word/order fulfillment acceptance.

## Verified boundary

A retained source descriptor, successful native recipe and private phone editor
plan agree on a 1.920-second source window. The guided editor projection instead
returns 1.933333 seconds after unconditional 30-fps normalization. This consumes
more than the original source. A text-only Save repeats that normalization.
The physical phone's failing stack and local binding duration were not collected;
the retained server divergence and this rejection class were reproduced locally.

The fix admits an explicit server-owned rendered clock from identity-matched
original or Visuals-pool receipts, preserves it through Save, and uses the same
clock for native projection, speed changes and transition reflow. Source bounds,
approval identity, stale revision and ownership checks remain enforced. Existing
legacy revisions keep their prior normalization and frozen hashes. Protocol 25
marks compatible native readers; older HTTP readers receive update-required
instead of interpreting a new clock incorrectly. API and worker must be deployed
with the new schema before protocol-25 Save is enabled in a released app.

PR1890's microsecond endpoint/cache fix is included from main. This repair makes
no further change to NativeEditorRenderCompiler's range guard.

## Executed local checks

- The initial editor read and text-only Save regression both failed before the
  fix: actual 1.933333 seconds versus expected 1.920 seconds.
- Focused backend matrix: 167 passed, 1 expected failure. It covers initial read,
  Save/reopen, client-clock ownership, actual source overrun, old/missing/25 HTTP
  protocol, non-frame-aligned rate plus crossfade, existing phone commits,
  guided revision hashes and legacy timeline equivalence.
- A private retained-data replay preserved all 28 moments' source starts,
  durations and output boundaries; repeated normalization was identical.
- Ruff and diff whitespace checks passed. Independent code review found no
  remaining blocking finding in the product delta.

## Native evidence and release boundary

The final scoped iOS26.5 simulator run passed117 tests with zero failures,
including RenderedOriginalClockTests, NativeEditorInteractionTests,
NativeEditorRenderCompilerTests, SavedDraftPreviewRangeTests and
SuggestionChipsTests. The generated source is measured at1.920 seconds. Saved
photo/video drafts hydrate, compile, produce a blue preview frame and export
MP4s with expected duration; a1.933333-second video window is rejected.
The export check measures duration; the blue-pixel check inspects preview only.
Backend Save/reopen and native constructed-draft hydration are complementary
checks, not one production end-to-end transaction. Native manual speed setter
plus crossfade interaction remains untested.

Result bundle retained locally: /tmp/task4-rendered-clock-tests-final.xcresult.
The first build's Swift expression error and first run's obsolete protocol fixture
were corrected; the final combined run has zero failures. Reliability manifest
checks:127 passed. Timeline contract checks:209 Jest tests,28 desktop browser
tests,42 mobile browser contracts and583 API tests passed. These are the
mandatory timeline gate components, run separately after an interrupted first
combined browser run. Preship lint, drift, release metadata, journey mapping and
HEAD-bound local-fix evidence checks passed. Remote CI remains to be run.

No production project repair, regeneration, paid provider call, feature activation
or deployment was performed. Original physical-phone reopen and local binding
inspection require a coordinated API/worker/protocol25 app release. The original
word/order measurement, exact original exported picture content and duplicate
creative-direction sections remain separate, unverified follow-ups.

## Legacy-state admission correction after full CI

Full API shard1 at43e15860d exposed five KRI1032 parity failures. The retained
legacy fixture has a rebuilt private editor plan but none of the modern original,
Visuals or device-render state fields. Treating every private plan as receipt-backed
made that projection unavailable. Exact-clock admission now requires modern
identity state; pure legacy records retain their prior projection. Present-but-empty
modern state still validates receipts and refuses missing originals. Complete
removal of all modern identity keys is indistinguishable from historical data;
this does not claim detection of arbitrary historical corruption.

The focused matrix passed180 tests with1existing expected failure, including all
unchanged KRI1032 mute/crop/Save and native-fixture assertions. No fixture was
regenerated and no mute/crop check weakened. Valid and missing mixed-photo
receipts are permanent regression tests. CI-unit125 and manifest127 tests passed.
Independent review found no blocking admission or compatibility defect. Fresh
remote CI remains required.

Tracking is not resolved: KRI1039 is the completed compose antecedent and KRI934
is the broader failure-screen parent. Neither is claimed as the dedicated owner
of this source-clock defect. The implementation is tracked by draftPR1946, whose
Linear-bound request-evidence gate remains blocked pending authorized tracking.
No new ticket was created, and no merge, rollout or original-phone acceptance is
claimed.
