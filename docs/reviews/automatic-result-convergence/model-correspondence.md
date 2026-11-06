# Automatic convergence: semantic handoff to the shared model owner

Native checkpoint: `4cbe7d2a7a5b89d0decdbd4a1405d2323250aa7a` (descends from reviewed `c929d5f30eafa1e98bc26813a282ed0e3b777cf3`).
Git tree: `3658a5d94647ab7159ac75bdc0a65e656cdbadaa`.
Base: `05d9720d8b3f838f8b91963e183eb7206c34e538`.

This is an implementation snapshot with independent static review, not a native execution receipt. Backend receipt/delivery changes remain uncommitted at this handoff. No formal model, coverage registry or sync hash was edited by this task. The parent coordinator owns the shared correspondence review and any later model changes.

## Changed covered functions

`NativeEditorSession.saveOnce` now owns a UUID-scoped `EditorSaveHistory`. Post-submission undo/redo checkpoints are independent of the capped visible undo stack. Save acknowledgements, catches and teardown check journal ownership; load invalidates the journal. A successful save consumes only submitted deletion intents and rebases both retained undo and redo history. A stale completion must neither replace a newer document nor clear its busy state. The earlier model's small save count/dirty flag does not itself prove the history-cap behavior.

`NativeEditorSession.runPromptRevisionSync` supplies chat-staged edits as the remote side of a three-way merge, rather than copying dirty lanes before/after rebase. Original clean base, current local content, latest committed clean content and staged remote edits remain distinct. A genuine overlap creates a pending merge and blocks Save; a successful merge changes destination job/item/variant only after resolution. Independent changes should converge without a confirmation prompt. Song edits require a conservative authority fence; job/generation metadata is not asserted to be an asset identity.

The working tree also changes covered `ChatWorkspaceView.retryApprovedRender`: when a fresh projection no longer identifies an exact failed execution, it returns without dispatch and without telling the user to refresh manually. Existing live polling continues. The failed-execution/revision requirement for the actual mutation is unchanged. This post-checkpoint delta requires correspondence review; no hash was resynced.

The hash-only audit against registry sync `b12bed356ff155ede19908dec43ef89b4c0062df` found 28 drifts. Only the two functions above differ from this task's base05; the other 26 were already present in that base. `/tmp/nova-auto-source-and-model-drift.json` contains expected/observed/base hashes for that intermediate audit. Do not resync all 28 without reviewing inherited semantics.

## New paths beyond the old registry scope

- Latest-job notification during Save retains a lifetime/request ticket. Busy-to-idle starts an authoritative project read as a separate asynchronous step. Newer notifications supersede tickets; stale reads cannot consume or adopt them. Read failures retry within a bounded attempt budget and one five-minute read window, honoring per-thread monotonic Retry-After deadlines across tickets. An ordinary sixty-second hint resumes in the owned task without another notification. Truly excessive waits or exhausted attempts pause visibly until a later project update.
- Dirty new-job adoption attempts an authoritative editable-base merge. Missing matching editable content retains an explicit fallback. Runtime1 does not call the v2 draft route. Readiness checks and watchers feed the same paced queue.
- `NativeEditorMerge` keeps timeline-dependent content and song authority conservative, merges stable identities/independent fields, preserves unchanged remote opaque rows, validates selected typed output, and reconciles deletion intents with chosen live entities. Conflict resolution is a user transition, not an automatic remote overwrite.
- SlidePost uses a durable owner/environment/item journal, one stable request UUID and expected-current-job compare-and-swap. Read deadlines, POST attempt budget and target identity survive session recreation. Strict keyed API admission has no legacy fallback. These states are outside the phone-render model's existing scope.
- Keyed backend delivery uses a permanent first-render claim, bounded pending publication, persisted staged-result completion, terminal late-write fences and bounded finalize recovery. Policy timeout is not proof of worker death; it cancels the original receipt and never automatically recomputes uncertain work. A staged result can complete without recompute. These server states are not proven by the existing phone-render model.

## Incoming integration boundaries

Cunda PR #2126 owns direct/nested load epochs and stale load completions. Fold this task's `resetLatestJobDrain` hooks at all load entries into Cunda's canonical lifetime reset so nested loads do not invalidate one another. Retain Cunda's post-await load checks, save epoch, retry sections and user-song retry ownership. This task's new reload helper currently fences its own defer/response by load lifetime and target; align it with the incoming canonical epoch during integration.

The new `isSaving` true-to-false hook drains pending latest notifications. Cunda's stale-operation defers must remain fenced so they cannot clear a newer load's busy state and trigger an early drain. Preserve the save-owned journal identity alongside that epoch. Preserve speed's coherent request/generation/file receipt behavior and canonical acknowledgement handling; absence of a textual conflict does not prove semantic compatibility.

Include the new drain/watch tasks in canonical close/owner-change cancellation as well as load reset. An ordinary Retry-After wait can now keep one cancellable task asleep for up to the bounded window; leaving the editor must invalidate its ticket and cancel that task. Add combined close-during-wait coverage when folding these hooks into Cunda's cleanup helper.

Required next evidence: combined source integration, final native build, actual named-test execution, shared model correspondence/teeth verification by its owner, supported Linux checks and rollout verification. No native/model execution is authorized by this handoff; the shared queue hold remains in force.

## Main advancement received during implementation

The parent verified current main `e090eb668124fee58cfc74c34e662ac78e268342`, four commits beyond base05, including #2097/#2124. A local read-only diff confirms no native Swift delta. It adds media replacement/ownership behavior in creation_threads/runtime/planner/source_replacement, caption processing/configuration in generative_build and caption modules, contract metadata and generated OpenAPI. This task has not rebased onto it; existing evidence remains explicitly based on05.

At controlled integration, merge source-replacement/media ownership semantics rather than choosing entire shared files, preserve caption pipeline changes, and regenerate the combined contract. Recheck strict AI thread/item claim locking against media replacement/editor-opening order, and rerun media-replacement/caption named checks alongside new receipt/revision/AI checks. The shared model owner keeps its original M=05 reference until correspondence is reviewed; no coverage hash resync was performed here.
