# Native editor source clock recovery

## Current compatibility phase

This PR now prepares readers without activating a new clock for existing projects.
The following historical repair evidence does not establish deployment or original
phone acceptance. The user reports that the opening error is gone; its causal build
is unknown.

Protocol25 already belongs to caption style scope. The app keeps that protocol.
The backend reserves protocol26 for rendered-clock reads and writes. Native clock
reader support can ship first, but a later app must advertise26 before activation.

An original receipt or private phone plan alone never upgrades a project. Initial
rendered-clock projection requires the server-owned private variant marker
`_guided_editor_clock: rendered-v1`. No production writer or migration in this PR
stamps that marker. Valid saved revisions remain authoritative, including legacy
revisions when a marker is present. An invalid saved rendered-clock revision fails
closed rather than silently replacing edits with the immutable approval.

The marker is stripped from public assembly projections. The reader resolves it
from the stored variant when processing a public copy. Unknown markers refuse
projection. Timeline, Save, retry and legacy orientation mutation boundaries require
a compatible rendered-clock reader. Source bounds and ownership remain enforced.

Activation is a separate dependency. API and every worker need this schema and
reader support, the app needs protocol26, and any new-job opt-in or existing-project
migration needs an explicit durable admission boundary. Rollback after activation
must retain compatible readers. This phase changes no production flag or project.
It does not claim to fix reopening on the user's current phone by itself.

## Current executed checks

The reader-first backend matrix passed514 cases without skips. Controls cover
unmarked modern projects, legacy saved hashes, corrupt saved clocks, unknown
markers, public-copy fallback, real source overruns and protocol25 refusal before
Save, retry or orientation mutation. Independent source review found no blocking
code issue after the privacy and mutation guards were corrected.

The current native compatibility run passed118 tests with zero failures on an
iOS26.5 simulator. The native source tree remained
`d25dfadf07e284362ae258f8e22bd5e2152986a2`. It includes protocol25 header,
generated1.920-second source preview/export and strict overrun controls. These
are generated fixtures, not original-media or physical-phone acceptance. Export
assertions measure duration; preview attachments inspect pixels. Exported temporary
files were removed by existing test teardown; the result bundle retains assertions
and preview attachments. The local bundle is
`/tmp/task4-reader-compatibility-c74d783e.xcresult`.

The required timeline gate passed209 web assertions,28 desktop browser cases,
42 mobile browser cases and585 backend cases. Preship lint, metadata, mapping and
HEAD-bound request-evidence checks passed. The preship incident selector currently
excludes open-PR gates; the new incident cases were run directly above and remain
mapped as a named CI journey. Hosted checks are required on the published head.

## Historical local repair evidence

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
legacy revisions keep their prior normalization and frozen hashes. The original draft used protocol25 for the new reader. That collision is
superseded by the reader-first phase above; protocol25 remains caption-owned
and26 is reserved for future rendered-clock activation.

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
inspection require a future coordinated API/worker/protocol26 app release.
The original protocol25 rollout proposal is superseded by the compatibility phase. The original
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

Tracking is KRI-1060. PR1946 remains a compatibility prerequisite until its
current-head checks pass. The earlier publication/ticket blockers are historical;
the dedicated ticket exists. No original-project migration or phone acceptance is
claimed by this reader-first phase.
