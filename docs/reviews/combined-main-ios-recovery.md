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

## Execution boundary

The corrected integrated candidate still needs the allocated native run. Do not treat source review, selector output or these notes as red/green, aggregate, physical-device, export or rollout evidence. The previous artifacts remain bound to their actual heads. A comparable negative control should use identical current tests with Services.swift and NativeEditorSession.swift restored to the e689040d6719a99d4805da2cf6117d60aea8e263 baseline, followed serially by this implementation when the parent grants the lane.
