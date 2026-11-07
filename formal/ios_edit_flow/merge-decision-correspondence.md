# Merge-decision policy companion

Status: owner-fence follow-up prepared for independent review and a bounded run.
Historical model head 8c34765d completed 32 expected outcomes with an explicit
prompt-owner source gap. The follow-up corresponds to product head
5075deb1cc36975e6aef0f224d60b4ffb7f6854e in the separate prompt-owner-fix
worktree. Product source in this verification worktree remains unchanged; no
native result is attributed to the verification-only commit.

## Scope and correspondence

`MergeDecision.tla` treats complete EditorDocument values as opaque snapshots.
The initial local document is manually dirty. The unproven-auto prohibition is
limited to that precondition: native code may adopt genuinely clean or purely
chat-staged cumulative changes without a submitted receipt. This model makes no
claim that those allowed clean/chat-only paths must prompt.

Generation and song-authority tokens represent the authority used to interpret
those snapshots. It does not prove the field merge algorithm, serializer,
rendering, transport, or real app lifecycle. Those require native/integration
coverage; the tested repair has 550 passing native tests.

| Model | NativeEditorSession correspondence | Limit |
| --- | --- | --- |
| Scope / ReceiptCurrent | mergeAuthority; submittedEditorStates; mergeChatDraft proven receipt | Two owners/lifetimes/authorities; provenance classification trusted |
| OfferResult / Apply | PendingMerge; applyMergedDocument / pending decision resolution | NativeEditorMerge result and admissible choices trusted |
| checkpoint / UndoReview / RedoReview | MergeDecisionCheckpoint; undo(); redo(); decisionIsCurrent | One decision, one later normal edit; complete documents opaque |
| HistoricalSnapshot | checkpoint.before restored in review | Revision/content represented together by snapshot token |
| NoReviewSave/Play/Export/Mutation | read-only historical-review guards and document rollback | Policy-layer mutations bypass all relevant defenses; not individual-guard necessity |
| NoStaleDecision | pending context authority and checkpoint authority checks | No theorem about all API calls or account isolation |
| BeginRead / CompleteRead | runPromptRevisionSync and latest-job observation callbacks | One suspended observation, finite invalidation token |
| RunRearm / DrainLatest | deferred authority-checked latest-job rearm after choice or Redo | One queued notification; transport/result parsing abstract |
| SpecProgress | eventual end of user review/edit/navigation, stable owner/lifetime, fair deferred task/drain | Does not force the user to resolve a conflict |

The executable-authority property forbids historical source/song authority from
entering normal executable history through rechoice. Full-document Undo is
allowed in read-only review. Rechoice and Redo must leave that boundary intact.
The read fence models cancellation, operation sequence and lifetime. For prompt
reads it also uses generation/song equality as a limited abstraction of the clean
baseline check; latest-job reads do not receive that extra restriction. The latest
owner check has its own guard-removal mutant using the same generic accepted-owner
property as the prompt assumption probe.
The independent clean-document baseline and timing/text/save gates are outside
this companion; it does not prove those source guards. Acceptance of a redundant
read is recorded without delivering a second result.

Independent normal edits and replacement-editor export have positive witnesses,
so blocking every operation cannot satisfy the complete harness. These are policy
reachability witnesses with non-review prerequisites assumed satisfied (eligible
receipt, API/player readiness). They do not prove dirty-state/save completion or
actual render/export eligibility; the existing SavedRenderReceipt model and
native/integration tests cover separate parts of those obligations.

## Historical source gap and correction

At historical native-tested repair bf1874e3b, `runPromptRevisionSync` checked
operation lifetime/sequence, cancellation and its clean baseline, but did not
compare an owner captured before the awaited read.
Latest-job observation does compare its captured owner. The account token can
change before SwiftUI teardown; no synchronous reset guarantee was found in
AuthStore, RootView, NativeEditorView.onDisappear or setObserversVisible(false).
This is a source-level gap requiring native/app replay, not a reproduced data
exposure or confirmed exploit.

The historical companion permitted an in-place owner change and its unmodified
prompt completion violated `NoOldOwnerCompletion`; that trace and exit-3 report
are retained. A deterministic native-session replay subsequently reproduced old
responses changing document/recovery state, with same-owner controls passing.

Product head 5075deb1 captures owner once in synchronizePromptRevision, retains
it across baseline retries, and checks it before follow-up reads, response
application and failure handling. Scheduled and foreground recovery retain an
optional scope record containing the original editor identity and owner; absence
of scope differs from an anonymous (nil) owner. Completion, app-gate failure,
stop and reset clear that scope. Four focused native tests cover 29 response and
recovery scenarios; the timer negative awaits actual captured-task completion.

The updated companion adds prompt owner equality and a PromptOwnerFence mutant
that removes only that check. Both prompt/latest owner positive checks must pass;
both matching owner mutants must violate the same generic property. The model
still has one read, so it does not prove arbitrary retry/catch execution; native
regressions and source review establish those parts. Real account-switch UI and
provider transport remain outside the local replay claim.

## Controls and execution

The standalone harness schedules six safety scenarios, twelve policy mutants,
nine positive reachability witnesses, two queue-progress checks and their two
NoRearm controls, plus the two owner positive checks (33 TLC invocations). Every mutant
must violate its named property; compile/discovery/timeout failures do not count.
Every witness must produce the named invariant counterexample. Existing formal
models, controls, hash registry and default verification command are untouched.

Run only after the parent allocates a formal lane:

```sh
JAVA=/opt/homebrew/opt/openjdk@21/bin/java \
TLA2TOOLS=/Users/emirerben/Documents/Codex/2026-11-06/task-3/nova/.dev/formal-ios-edit-flow/tla2tools-pinned.jar \
TLC_TIMEOUT=120 python3 formal/ios_edit_flow/verify_merge_decision.py
```

The runner requires existing explicit tool files and validates the original
TLC 1.8.0 SHA-256 pin through the shared helper. It does not install tools.
Artifacts are separate under `.dev/formal-merge-decision-owner/`; the default verifier's
output is preserved. An outer 15-minute batch timeout and resource coordination are required before
execution. Stop on timeout or the first unexpected outcome and report the partial
results; do not weaken properties, enlarge bounds or retry without allocation.
The 33 checks at 120 seconds each have a worst case ceiling exceeding that batch
budget, not a predicted duration. Static syntax checks are not formal evidence.

## Static review record

Independent reviewer found the original latest-owner property vacuous because its
flag was prompt-only. The final model records accepted owner mismatch for either
read kind and the harness includes a latest-owner guard-removal control.
Reviewer also found latest reads had a stronger captured-authority fence than
Swift; that restriction was removed for latest reads. Prompt authority remains a
limited clean-baseline abstraction. Manual-dirty and execution-prerequisite
assumptions are explicit above. Re-review of those corrections is separate from
TLC execution; no static finding is evidence that a model check ran.


## First execution and encoding correction

Head e6dd13b89ce327ac51ac34631302f4b45e172c3c ran at
2026-11-07 04:48:42–04:48:54 UTC. Six safety cases, ten initial policy
mutants and nine reachability witnesses passed (25 checks). TLC rejected the
first progress configuration because it cannot accept eventual quiescence as a
specification conjunct. This was an encoding error, not a property violation;
remaining progress/owner checks did not run.

The correction keeps weak fairness in SpecProgress and expresses the same
conditional obligation as `(<>quiet) => (queued ~> ~queued)`. The two progress
configurations already disable owner change and replacement, so the redundant
stable-owner/lifetime specification conjunct is removed. NoRearm must still
produce a named temporal violation with the identical assumptions. That correction was subsequently executed at 8c34765d; both progress checks
and both NoRearm controls produced their expected outcomes.

Independent reviewer confirmed the rewrite is logically equivalent specifically
for these fixed-owner/lifetime progress configurations. A queued notification can
be followed by choice/Redo and Quiesce, and NoRearm can keep it queued forever
while quiet, so the intended negative control remains nonvacuous. At the time of that review TLC acceptance and progress were unverified; the
subsequent 8c34765d run completed both progress checks and their NoRearm controls.


## Owner-fence follow-up validation

The first 32-check companion result remains historical before-fix evidence, not
proof of the new source guard. The updated 33-check harness must complete on its
new exact model head. No original hash registry or existing baseline model/control
is changed. New verification results will be stored separately from the retained
historical reports; no previous expected source-gap trace is relabeled as fixed.
