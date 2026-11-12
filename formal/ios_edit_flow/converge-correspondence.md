# Converge model correspondence

`Converge.tla` models one rule: the iPhone editor always shows the latest version,
AI and manual edits apply on top, and the creator is never asked to resolve a conflict.
Run it with `make verify-converge`. Exit 0 passes, 1 fails, 3 passes with source drift.

## State and actions

All state lives in one record `s`. Documents are abstract: only the generation the
editor sits on, who made the unsaved edits, and the undo entry matter.

| Model | Swift symbol | File |
| --- | --- | --- |
| `s.clean` | `cleanDocument` (its `revision.baseGeneration`) | `NativeEditorSession.swift` |
| `s.prov` (`none`, `ai`, `creator`) | `hasUnsavedChanges`, `hasOnlyChatStagedChanges` | `NativeEditorSession.swift` |
| `LocalOf(prov)` | `creatorDocument`: nil unless the creator edited | `NativeEditorSession.swift` |
| `s.undo` | `undoStack` entries, one modelled | `NativeEditorSession.swift` |
| `s.server`, `s.head` | latest render generation; the chat draft head on it | server |
| `s.ident` | `editorOperationIdentity` | `NativeEditorSession.swift` |
| `s.busy` | `!isQuietForConverge` (text entry, gesture, timing gesture) | `NativeEditorSession.swift` |
| `s.read`, `Stale` | `reloadLatestKeepingLocalEdits`, latest-job and prompt reads, fenced by identity and `cleanDocument` | `NativeEditorSession.swift` |
| `ConvergeTo` | `applyConverged(ConvergeTarget)` | `NativeEditorSession.swift` |
| `replaced` | `Converged.replacedCreatorEdits` | `NativeEditorMerge.swift` |
| `ConvergeApply` | the drain that waits in `settledForConverge`, then converges | `NativeEditorSession.swift` |
| `CreatorEdit`, `Undo` | edit mutations, `undo()` | `NativeEditorSession.swift` |
| `SaveOK`, `SaveConflict`, `SaveRefused` | `save()`: success; `baseline_conflict` then `reloadLatestKeepingLocalEdits`; any other refusal | `NativeEditorSession.swift` |
| `SwitchEditor` | opening another edit (a new `editorOperationIdentity`) | `NativeEditorSession.swift` |

`converge-source-map.json` lists the Swift members the model was read against. The
verifier hashes them and exits 3 when one changes. Re-read `Converge.tla`, then run
`python3 formal/ios_edit_flow/verify_converge.py --resync`.

## Rules

- `NoChoiceState`: some creator action (edit, Save, sync read) is always enabled. The
  model has no pending-decision state.
- `ChatOnlyNeverReplayed`: when only AI-staged edits are unsaved, converge's `local` is empty.
- `UndoOnLatestBase`: an undo entry always carries the latest adopted generation.
- `ShowsLatest`: with fair reads and no further server change, the editor ends on the
  latest generation and stays there.
- `SaveEventuallyEnabled`: unsaved changes either go away or Save becomes enabled.

## Negative controls

Each control is a copy of the spec with one edit. The verifier fails unless the real spec
passes and every control breaks its rule.

| Control | Breaks |
| --- | --- |
| an overlap parks the result in a pending decision that blocks edit, Save and sync | `NoChoiceState` |
| AI-staged edits are passed as `local` | `ChatOnlyNeverReplayed` |
| Undo restores the pre-converge document | `UndoOnLatestBase` |
| no fairness on starting a read | `ShowsLatest` |
| no fairness on ending a gesture | `SaveEventuallyEnabled` |

## Not covered

- The field-level merge inside `NativeEditorMerge.converge`. The model only says whether
  the latest version replaced some of the creator's edits. Native tests cover the merge.
- Server-side head restaging. The server only marks a draft head; a render clears it.
- The slide-post editor. It still has a conflict choice (KRI-1369).
- The "Switching discards your unsaved edits" prompt (KRI-1368).
- Network failures, retry caps, song authority and the preview rebuild.
- A Save is one atomic step. A Save that overlaps a read cancels it; the model does not
  split the request from its answer.
