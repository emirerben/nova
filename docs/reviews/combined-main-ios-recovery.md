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

## First chat-contract execution

Pinned candidate 79fb3e443e77d036f8d9669236214d5b967a64c4 compiled and ran the focused batch on the iPhone 17 Pro iOS26.5 simulator using Xcode26.6 (17F113), from 2026-11-06 23:58:16 UTC through 2026-11-07 00:00:33 UTC. Result: 121 tests, 110 passed, 11 failed, none skipped (31 assertion failures). All25 merge-core cases and the cross-generation/history and changed-song-authority controls passed. The aggregate stage was not run.

The diagnostic traces identify a remaining identity caller bug: the same opaque slot was hydrated under the thread ID on one path and the session's initialization project ID on another. Base and remote song authority were equal; invented compatibility asset differences caused the connected-content conflicts. The correction must use one canonical target namespace across initial load, reload and fallback projection, preserving explicit real source identities. Independently, trimming serializes known null/cut schema defaults that the narrow trim/wording comparison must recognize without stripping unknown fields or nondefault transitions. These subsequent corrections are not covered by the 79fb result.

The post-run correction also distinguishes genuine prior chat-only state from manually edited state echoed in a proven reply. Only genuine chat-only cumulative results can advance automatically without a new submitted receipt. A manual-font/proven-reply/unknown-cumulative control must preserve the exact local document and history until choice. The late-429 fixture now uses separate old and replacement responses: replacement must finish before releasing the held old GET, and its later reload must not inherit the old failure deadline. The former shared response queue let replacement consume the old error and contradicted the nonblocking replacement contract.

Two new-job authoritative-merge cases still return typed-validation refusal; the identity scope correction does not explain these paths, which already used the thread namespace. Their assertions remain unchanged and synthetic pending-merge traces were added for the next allocated run. No selected-value validation, song fence or source-replacement guard was relaxed to make them pass. Post-run changes have syntax and independent source review only; final-head native evidence remains pending.

## Replan refusal root cause and boundary correction

Source tracing independently identifies the two remaining replan refusals. The real flat chat fixture sends remove_music=true. Staging cleared music_track_id but retained the preceding music_window. The selected merge state therefore contained no track plus an old window object. Strict merge validation forces typed serialization, whose writeMusic clears both fields when there is no track; explicit-value preservation correctly rejects the disappearing window. The valid 3600-second text interval, style and opaque values do not explain this rejection.

Explicit removal now clears the canonical track/window pair and removes the legacy music alias at the staging boundary. Alias-only removal participates in change detection even when both canonical fields were already null. No removal command means no such normalization. All unrelated fields and strict merge validation remain unchanged.

Tests-before-fix commit a7952f930 records the new controls with the original production adapter. Two adapter regressions should fail behaviorally there; the strict validator reject/accept control should pass. The fixed candidate still needs an allocated red/green run. New tests cover canonical and legacy shapes, explicit non-removal, unrelated opaque metadata, real flat chat plus local trim and a new generation. Existing end-to-end replan assertions are unchanged. Independent static review and syntax parsing pass; no native/formal result is claimed for this correction.

## Music-removal behavioral proof and remaining timeline assertion

Control a7952f930182b041b1961cc1dba269cf80dab011 compiled and ran3 cases from2026-11-07 00:32:01 UTC to00:34:23 UTC. Two adapter regressions failed as expected on retained-window/legacy-removal/rebase behavior; the strict validator control passed. This is behavioral red proof, not a compiler or environment failure.

Candidate15999e658547a1372f7041ce2aae4b4a00e6dc8d compiled and ran126 focused cases from00:34:52 UTC to00:37:21 UTC:125 passed,1 failed,0 skipped. All94 chat cases including both original replans and all26 merge cases passed. Namespace, generation/song Undo and saved-removal recovery controls passed. The only failed assertion compared pre-rebase raw timeline cache fields (duration_s2) with coherent post-rebase fields (duration_s1.5), although both typed durations and source identity matched. It now requires a nonnil complete serialized authored timeline snapshot before comparing after rebase; generation, retry counts, dirty state and no-loop checks stay intact.

The aggregate stage was not run. Owned runners exited65 and the lane was released with285GiB free. Temporary synthetic diagnostic functions/log calls were removed after their findings were resolved. These subsequent assertion/cleanup changes need final-head native validation;125/126 does not imply a passing candidate.

## Recovery choice correspondence and rendered-text mirrors

The historical SavedEditorRecovery fixture changes both order and timing of the same connected clips. Tests now require exact local preservation and a pending choice before accepting that authority; resolution itself cannot POST, and repeating Save while unresolved remains blocked. A distinct independent-wording fixture retains source, timing and order and must automatically rebase the local trim and retry the original Save. Replacement-lifetime and no-stale-fallback assertions remain intact.

Source tracing of that independent rendered-wording case identifies a second false conflict: Models.persistedSnapshot regenerates ios_editor.text.content alongside canonical text_elements. The narrow trim/wording predicate previously compared this derived content as an independent field change. It now accepts unchanged non-text sections directly, or requires successful mirror proof on BOTH sides: unique canonical and mirror IDs, complete matching coverage and exact canonical content. Only content is removed from the comparison; ordering and all other fields remain compared exactly. The selected remote metadata is never rewritten by this comparison.

New direct controls reject changed unknown mirror fields, mismatched/missing/null content, wrong/duplicate IDs and reordered rows. A stale but unchanged mirror remains compatible with a canonical chat edit. No source/timing/song/owner/provenance fence or merge-output validation was relaxed. Independent static review and syntax parsing pass; these post-run changes still require allocated behavioral and final-head native verification.
