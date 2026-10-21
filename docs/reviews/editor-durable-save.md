# Durable editor Save and effective rendering

Local implementation on `codex/editor-draft-save`; no production or physical-device verification.

## Failure modes (before implementation)

- A source overrun in an unchanged first clip rejects unrelated text deletion,
  text movement and second-clip trimming.
- Save acknowledges a render projection instead of the exact authored source
  window; reopening silently shortens the video or restores deleted text.
- A failed or late renderer exposes the previous generation as the saved edit.
- A feasible source-window shift changes timeline duration, text timing, rate,
  camera-audio selection, source identity or an explicit moment anchor.
- A genuinely unavailable source prevents durable editing instead of leaving
  an actionable render diagnostic.
- Relaxed creative validation admits another owner's source, unknown slot IDs,
  stale revisions, nonfinite values or oversized payloads.

## Implementation

Reuse CreatorEditDraft and the existing editor generation/revision fence.
Keep authored lanes separate from a derived render projection. Never overwrite
the authored snapshot with compiled values. Existing route ownership, source
admission, schema and concurrency checks remain mandatory.

For unanchored video windows whose requested span fits the immutable source,
render the nearest in-bounds window with unchanged duration and rate. Preserve
the exact requested window in the editor and expose the effective mapping.
Do not invent frames or silently change an explicit source/moment constraint.

Timeline contract (six decisions):

1. Native `NativeEditorTimelineProjection.totalDuration` owns editor output
   geometry; `TimelineMath.totalDuration(of:)` owns compiled native playback.
   Server guided revisions use `build_guided_segments` and
   `effective_transitions`; authored recipes use `timeline_end_s(clips)`.
   The source-window projection changes none of their output placements.
2. No ripple: text, captions, drawings, visual blocks, camera effects, SFX and
   media overlays retain authored timestamps. Continuous music stays on its
   existing clock.
3. Scrubbing, ruler and transport retain the canonical editor projection's
   duration. A source-window adjustment cannot change their bounds.
4. No new resize behavior. Existing editor steps, frame rounding and adjacent
   transition caps remain owned by the functions above.
5. No new undo operation. Derived rendering never rewrites the document or
   adds history entries; a source selection remains one existing transaction.
6. Paired server/native tests must prove effective source mapping, frame count,
   transitions, text and camera-audio parity; export uses the same recipe as
   preview and remains generation-fenced.

Anchor tracing: the existing `set_clip_in` and `trim_clip_start` operations
write only numeric windows and lose explicit-selection provenance. Add an
optional `source_start_locked` field through those producers, wire models,
saved snapshots and native source selection. Omission on later edits inherits
the saved value. Historical speech/moment constraints require conservative
handling; do not treat the absence of this new field as proof that a
speech-aligned or explicitly constrained source can move.

## Required evidence

Save/reopen/export journey with the Troya-shaped invalid first window and valid
second-clip trim, text deletion and movement. Negative security/concurrency
cases, unresolvable-source durable draft, stale render completion, native
load/save without clamping, and actual local rendered media inspection.


## Verification and boundaries

- Protocol 2 saves a complete immutable snapshot after normal validation and
  before dispatch. Protocol 1 retains its existing refusal behavior.
- Authored edits and non-narrated guided edits without a creator-song track
  use the new source projection. Other archetypes retain their existing render
  contracts; this is not a general rewrite of narration or song alignment.
- Known explicit source selections are sticky across older-client omission,
  null and false. Historical narration/source-span and tail-hold projections
  keep their existing geometry rather than entering the new mapping.
- Source identity admission remains mandatory. An admitted cloud object whose
  bytes expired can be saved with a reconnect recovery message. Invalid phone
  source receipts still fail admission; no missing/foreign receipt is invented.
- The separate legacy `cs-…` slot-ID migration (PR1719/KRI-799) is an integration
  dependency, not implemented here. Unknown real-looking IDs still return 409.

Local evidence in `.dev/` (not committed): `final-api-current.log` has 209 passing focused
server checks, including 12 PostgreSQL Save journeys and real FFmpeg rendering;
This combined run includes the protocol 2 receipt checks. The shared timeline
verification gate log `timeline-gate-coordinator.log` has 209 web unit tests,
70 browser checks and 583 backend tests passing. `preship-final.log` passes scoped
lint and 82 selected reliability checks, but could not fetch the remote ref
under the sandbox. Journey mapping was skipped because the diff is uncommitted; publication and CI are tracked separately from this local verification.

Native execution: `.dev/ios-v4-tests.log` and
`.dev/durable-save-native-v4.xcresult` record 343 selected tests passing with zero
failures (ChatDraftStaging, NativeEditorRenderCompiler, NativeEditorSession,
DrawingParity). The Troya case traverses the real session projection before
compilation and retains the raw 0.9 s in-point while rendering 0.36–3.36 s at 1x.
Earlier failed artifacts remain alongside the final result. Their broad guard
regression was corrected without changing existing narration/rate/hold inputs
or assertions. DrawingParity's existing expression was split into typed
intermediate values solely to avoid a Swift type-check timeout.

Final scoped lint/format and `.dev/preship-final.log` pass. No physical-device
research, Photos export or production end-to-end claim is made. This report records local verification before draft-PR publication.
