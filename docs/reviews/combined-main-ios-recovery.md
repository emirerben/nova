# Combined-main native recovery repair

Contributes integrated-head build and test coverage to KRI-1230; this is not a claim that the whole feature is fulfilled.

## Compilation

Main's standalone iOS run 42289925662 could not compile its test target. EditorCommitSpy's main-actor owner property did not satisfy the API's synchronous nonisolated requirement; two slide actors lacked the 24 base API witnesses without defaults. The repair keeps mutable editor state on the main actor, puts the synchronous owner behind a Mutex, and adds explicit unsupported-throwing witnesses to the slide actors. It adds no production success defaults or concurrency suppression.

## Failures exposed after compilation

Candidate 228b8e0d4359d16043c471c9727254a4e3568c6f compiled the full test target and ran 383 cases: 365 passed and 18 failed. That is prior-candidate evidence, not a passing result for this correction.

The slide race fixtures used a count of Task.yield calls as if it were a synchronization barrier. They now wait for expectations fulfilled only after the transport continuation is installed, with bounded async timeouts. Account-switch coverage now checks that old private presentation clears, its undelivered journal identity survives, and replacement-account state must be fetched again. Existing race/result assertions remain.

The editor tests exposed unstable hydration of valid opaque canonical slot IDs. The old adapter made a new UUID on every read and used it as synthetic asset metadata. The three-way merge interpreted that invented metadata as a remote timeline change, falsely coupling text, trim and song changes. The old random fallback predates this repair; the automatic-refresh merge path exposed its effect.

## One consolidated identity adapter

The #2137 owner prepared independent controls in bb89d2c6340a7d8866cd0faa54a81e83784e21c8 and a source proposal in 6bbe42c8f8c64d6a18a2eb52d102c509073f7ca1. This branch retains the seven controls unchanged and consolidates one implementation rather than applying two competing adapters.

Both draft decoding and session fallback projection use EditorCompatibilityClipIdentity. A versioned SHA256 namespace, project UUID and canonical key derive a UUIDv8 compatibility identity. Valid UUID identities retain precedence. A nonempty canonical slot_id wins over an opaque compatibility id. Explicit opaque asset IDs use a separate source namespace: shared sources stay shared within the project, and actual source replacement changes identity. Anonymous rows remain anonymous; positional coincidence does not invent a canonical entity.

Compatibility UUIDs are not receipts, generations, ownership epochs or access authority. Source resolution still uses the established source index, media IDs and fingerprint checks. Genuine source replacement must continue to conflict with dependent local edits.

The original hydration regression covers opaque slot/source repeats, project separation, shared and replaced sources, valid UUID preservation and anonymous bounds. The owner's controls additionally cover reorder stability, empty-ID handling, real session document rehydration and a genuine source-conflict control.

## Fixture contracts

The persistent-conflict fixture now supplies a coherent authoritative timeline matching its draft. Retry-count, selection, undo and unsaved-trim assertions remain. The transient-load test follows the intended slow automatic-observation policy and checks recovery plus hidden/owner fences using injected time and state predicates. Timeline preservation compares the complete serialized authored lane, including opaque fields, rather than stale cached raw mirrors of fields that have since been edited.

## Execution evidence and remaining repair

Historical control 99352402000c9b2c17bef228ac7d1e3c3af28454 ran the eight identity controls with the same tests and the two production identity files restored to the e689040d6719a99d4805da2cf6117d60aea8e263 baseline. Five failed behaviorally and three passed. Candidate c717da7941067da67a76b0adf19b7b242eeae9c5 passed all eight controls and all 88 slide cases, but its complete selected batch failed: 507 cases, 483 passed, 24 failed, none skipped. This establishes the bounded identity correction, not an aggregate passing candidate.

The batch exposed additional chat reconciliation, replacement-load, preview and saved-song cases. The replacement load now separates epochs instead of waiting for an obsolete suspended transport. Preview conflicts that require a choice present an actionable failure. Those later changes have not yet run natively.

The reviewed chat contract requires a three-way merge of submitted state, current local state and the chat result. Independent fields combine automatically; overlapping changes after submission require a choice. A missing submission receipt does not authorize copying a complete remote lane over local edits. Undo must restore the complete pre-application local document, and cross-authority conflict choices must remain reversible without replaying stale song or source authority. The lane-copy checkpoint 964f50e4862a3ebb9215661f579bbe3333b35992 was not published. This correction replaces it; bounded independent source review and syntax parsing pass, while native compilation/runtime validation remain pending.

Native resources remain centrally allocated. New source and test changes require an explicitly allocated final-head run; prior results remain bound to their exact heads. Physical-device, real-server replacement-render/export and rollout acceptance remain unverified.

## Chat merge acceptance and authority boundaries

The correction compares the scoped submitted document, the current local document and the chat result. Existing wording and font fields on the same text entity can merge independently; a narrow trim/wording exception requires unchanged row identities/order, source identity, text intervals, all other fields, deletion intent and song authority. Changed intervals, replaced sources, inserted/deleted entities and unknown fields retain the conservative connected-content decision.

Dirty content without a matching submitted receipt requires explicit choice. A clean editor can adopt a same-target result without inventing concurrent local work. Both automatic application and either same-authority choice retain the complete pre-application local document as one Undo step.

A changed-generation decision uses an authority-bound checkpoint instead of putting an old relative document into normal executable history. Undo presents the exact old document read-only; Save, chat export, playback, preview building and video export must remain blocked until a guarded rechoice or Redo. Replacement lifetime/owner/target and a newer clean/song authority invalidate stale decisions. Source review and focused regressions must cover refreshes already suspended before Undo, not just reads started afterward.

The phone-render skill's existing saved-receipt companion bounds saved-generation exports and replacement-editor writes. It does not model this new read-only merge-review state or prove the new merge algorithm. No global coverage hashes are resynced. Its existing checks and native merge/export regressions need an allocated run; a model extension or an explicit bounded correspondence review remains separate from claiming those new states formally verified.
