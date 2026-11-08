# Bounded split persistence correspondence

Source reviewed at `5b46bc1ea4343b8e746fb7d0df14f1cc63b229e6`, integrated main `a518aed8c`. This companion is manually mapped TLA+, not extracted Swift verification. Authoring this document did not run TLC. The parent runner must record actual safety, individual mutation and reachability results before claiming verification. No global coverage hash or original model is refreshed by this addition.

## Scope and bounds

One section, two/three panels, three distinct occurrence tokens. Occurrences a and b share source S1; c uses S2. The finite catalogue retains slots, memberships, definitions and a dependent-lane token separately. Independent occurrence identity is preserved even for repeated sources. Definitions distinguish absent, explicit empty, two/three and their weighted layouts. Values represent admitted/canonicalized rows; client-provided source paths and raw JSON are not trusted values in this model. Numeric crop/source-time values and geometry are abstracted as unchanged payload, not enumerated or proved. There is one acknowledged Save, at most two manual/AI edits, and one pending Save. An edit remains enabled while Save awaits. Failure/retry, load and merge actions may repeat, but all state domains are finite. Witness sets record the latest relevant event, not unbounded history. Safety failure sets retain bad events.

Initial ordinary/two/three values represent already durable projects. Ordinary edits out of a split use explicit Unstacked, not an absent definitions key; ordinary authoritative fallback has its own action. The catalogue is intentionally not a generator of arbitrary malformed JSON. Orphan is the explicit invalid case. The validity predicate recognizes catalogue membership consistency, not all native layout validator rules. Merge uses one concrete conflict: clean Three, local ThreeLayout and remote Removed. Save cannot begin while a merge is pending, and resolution/adoption cannot run across a pending Save. Rendering, source pixels, actual crop/clock math, undo gestures, networking, account changes, crash recovery and coordinator lifecycle are excluded.

## Source correspondence

| Model | Concrete source boundary |
| --- | --- |
| Edit, Dirty | NativeEditorDocument.swift `snapshot(for:)` around1099 and `sectionChanged` around1155; NativeEditorSession `refreshDirtyState` around9073. Rows and definitions jointly own timeline dirtiness. Layout is a layout-only change from Two, with a coupled dependent payload token. |
| BeginSave, Wire | NativeEditorSession.swift `saveOnce` around8081-8114 validates before its first await and captures submittedDocument; `commitRequest` around9246-9271 sends definitions with timeline, including [] when removing a previous split. Missing definitions and [] have different wire meaning. |
| Acknowledge, FailSave | `editorCommit` await at8114 is between submission and response. `acknowledge` around9339 copies the submitted value into cleanDocument; `copy(.timeline)` around9407 copies rows and definitions together. Current local edits are not replaced by the captured submission. Failure does not acknowledge. |
| StageAI | Core/Services.swift `stagedChatEdit` around3265 and `mergedTimeline` around3292. Explicit split definitions make rows authoritative, so removed unmatched base members are not appended. Witness Three -> Removed isolates this behavior. |
| Merge, Resolve | NativeEditorMerge.swift around226-280 groups all dependent authored lanes when either side changes rows OR definitions. A concurrent local ThreeLayout / remote Removed change from clean Three conflicts as one bundle; resolution chooses one complete side. The model isolates this fixed conflict instance rather than proving the generic JSON merge algorithm. |
| ReceiveInvalidMerge, Adopt | NativeEditorMerge.swift around140 validates selected merged document before adoption. Orphan is malformed/unsupported data reaching this validation boundary, not a supported editing operation. |
| Load canonicalSlot/canonicalAlias | Core/Services.swift `decodedEditorDraft` around3154 creates UUID adapters; Core/Models.swift `persistedSnapshot` around343 reconstructs rows; NativeEditorSession.swift `snapshotPreservingClipMetadata` around9109-9151 restores canonical asset_id by opaque slot ID or UUID alias. Missing/null/opaque identity must not become the required compatibility UUID. |
| Load legacy/unrepresented | The same hydration helper synthesizes asset UUID only if no canonical row represents the compatibility clip. These are legacy API clips and appended compatibility clips without server rows. The concrete UUID value is abstracted to assetUUID. |
| Load fallback, FallbackOrdinary | NativeEditorDocument.swift around961-966 reads raw user_timeline fallback without manufacturing canonical asset identity. Services.swift around3015-3023 and3125-3135 adopts authoritative slots and definitions together, explicitly clearing stale definitions when ordinary slots win. The fallback asset case represents raw document fallback; it does not claim every legacy DraftSnapshot route hydrates arbitrary fallback-only input. |
| Reopen | NativeEditorDocument.swift around995-1007 encoding and clean raw-snapshot preservation. This action checks the finite durable catalogue round trip; Load separately checks asset tags at the actual adaptation boundary. The model does not claim a single composed source-media import/render execution. |

Expected canonical asset is the supplied wire tag, independent of the compatibility UUID adapter. Expected authoritative membership is the explicit Removed value, independent of base-unmatched append logic. Expected clean acknowledgement is captured submitted, independent of current; the current value after acknowledgement must equal its pre-acknowledgement value. Event checks inspect actual primed result fields rather than repeating the mutation-selected expressions. Expected merge output is one entire side, not recombination. Invalid source/adoption oracles use the catalogue's explicit Orphan witness independently of the action's Valid guard. These independent expected values are the reasons the mutations can fail.

## Runner interface

Use `SPECIFICATION Spec`, all fourteen mutation constants set to FALSE for safety and positive controls. No fairness/liveness requirements. Check these invariants together:

- RequestPreservesSubmission
- DurableMatchesSubmission
- PostSubmitEditPreserved
- ValidCommit
- ValidAdoption
- MergeIsCoupled
- AuthoritativeMembership
- AssetProvenancePreserved
- CleanLoad
- LayoutEditIsDirty
- OrdinaryFallbackClearsDefinitions

Set exactly one constant TRUE per mutation run. Check only its promised invariant, so unrelated breakage cannot count as its tooth.

| Constant | Required failing invariant | Source-derived triggering action |
| --- | --- | --- |
| OmitDefinitions | RequestPreservesSubmission | BeginSave of Two |
| DropEmptyDefinitions | RequestPreservesSubmission | BeginSave of Unstacked |
| IgnoreLayoutDirty | LayoutEditIsDirty | Edit(Layout), initially clean Two |
| SplitMergeBundle | MergeIsCoupled | Merge combines local layout with remote dependent payload |
| AppendRemovedMembers | AuthoritativeMembership | StageAI from Three |
| AckCurrent | PostSubmitEditPreserved | Acknowledgement incorrectly baselines the newer current value |
| OverwriteCurrentOnAck | PostSubmitEditPreserved | Acknowledgement overwrites the newer current value with submission |
| AllowInvalidSave | ValidCommit | Edit(Orphan), BeginSave |
| AllowInvalidAdoption | ValidAdoption | ReceiveInvalidMerge, Adopt |
| OverwriteCanonicalAsset | AssetProvenancePreserved | Load canonical missing/null/opaque identity |
| DropLegacyAsset | AssetProvenancePreserved | Load legacy or unrepresented clip |
| RetainFallbackDefinitions | OrdinaryFallbackClearsDefinitions | FallbackOrdinary from Two |
| DirtyOnLoad | CleanLoad | Load manufactures unsaved state |
| WrongSourceOnLoad | CleanLoad | Load changes the known source token |

A fifteenth mutation run enables only OmitDefinitions but checks DurableMatchesSubmission at acknowledgement. The separate request-boundary run remains, so an earlier unrelated failure cannot count as durable-state coverage.

Positive controls are deliberately false invariants. Run each independently with all constants FALSE; each must fail. Final exact names:

`NeverSplitSave`, `NeverSplitReopen`, `NeverUnstack`, `NeverLayoutSave`, `NeverConcurrentEdit`, `NeverAIRemoval`, `NeverMergeResolution`, `NeverMergeAdoption`, `NeverCanonicalSlotLoad`, `NeverCanonicalAliasLoad`, `NeverFallbackLoad`, `NeverLegacyLoad`, `NeverUnrepresentedLoad`, `NeverOrdinaryFallback`.

These ensure successful paths remain possible. They are bounded reachability witnesses, not eventual-progress claims. CleanLoad records a load-only clean/source observation; its dirty/source mutations are independent, and it does not prove arbitrary edit-history correctness. Repeated source tokens likewise establish domain coverage, not a renderer source-selection proof.

## Admitted server persistence boundary

`authored_editor.py:253` overlays definitions only when supplied. `generative_jobs.py:13306-13314` replaces the admitted rows, retains saved definitions when the request omits them, replaces supplied definitions, and represents explicit-empty clearing durably by omission. Therefore Wire preserves the request presence distinction, while Canonical normalizes an explicit-empty definition token to absent durable definitions. DurableMatchesSubmission compares the actual acknowledged durable result with that independently specified canonical form, not literal request JSON. The clean native baseline remains the captured submission.

`editor_empty_drafts.py:478-540` stages rows and effective definitions in one immutable draft; `kria/drafts.py:407-450` inserts the snapshot revision in the caller transaction. `plan_items.py:8453-8461` commits the staged draft before dispatch and returns EditorCommitResponse at8548. Dispatch failure can still acknowledge a durable Save. The model assumes the database transaction is atomic. It excludes crash/response-loss behavior, empty-document Save and concurrent clients. `split_screen_editor.py:9-49` and `authored_editor.py:247` establish protocol/capability/layout and generation admission before this successful-commit abstraction.

## Commands and source binding

Run `python3 formal/ios_edit_flow/verify_split_persistence.py --check-source` before the companion. Its separate source map pins reviewed files and scoped obligations, not global lifecycle coverage. There is no automatic resync command. With the same independently verified TLA2TOOLS and JAVA settings as the original gate, run `python3 formal/ios_edit_flow/verify_split_persistence.py`. The runner checks eleven invariants, fifteen mutation runs and fourteen reachability controls and writes `.dev/formal-ios-edit-flow/split-persistence-report.json`.

The concrete protocol floor remains the existing lifecycle floor abstraction. `src/apps/api/.venv/bin/python formal/ios_edit_flow/verify_split_protocol.py` runs the actual helper on a typed recipe at protocols42/43 and an isolated AST mutant that removes only the new split-floor branch. The identical assertion must reject that mutant; other capabilities and inputs remain unchanged. Source and fixture pins prevent silent drift. Socket, subprocess and shell operations are denied inside this offline control. It proves the helper boundary, not HTTP repinning or full lifecycle correctness.

## Relation to existing models and open obligations

Successful render-starting acknowledgement can be abstracted to PhoneRender EdSave and SavedRenderReceipt Commit. Rejected local Save, edits, encoding and merge selection stutter with respect to their render/request variables. Empty or render-not-started commits require the existing broader save audit; do not pretend every durable commit pins a render. The companion deliberately separates submission and acknowledgement, preserving the actual await interleaving.

This is an explicit bounded correspondence argument, not a machine-checked refinement theorem across modules. Existing lifecycle/receipt invariants, bounds, historical failures and individual teeth remain mandatory. Inherited 36 function drifts, durable Stop/Resume, account ownership, coordinator await/run boundaries and shared repair are not discharged. Root's scoped full-file source map must state these limited claims; neither a source digest nor passing finite companion justifies globally resyncing coverage.json.
