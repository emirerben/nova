---- MODULE MergeDecision ----
EXTENDS Naturals, Sequences

(***************************************************************************
 A bounded policy companion, not a JSON merge/renderer model. Complete native
 documents are opaque snapshot records. NativeEditorMerge's choice/result is
 trusted; native tests establish its field/serialization behavior.

 One result, one later edit, one suspended read, one queued notification; two
 editor lifetimes, owners, generations and song authorities. Repeated boundary
 Undo/Redo/rechoice remains possible without growing history or counters.

 Mutation switches bypass POLICY LAYERS. In particular ReviewMutation bypasses
 both public mutation guards and the document observer's rollback. These are
 not claims that removing one source guard defeats defense in depth.
 ***************************************************************************)
CONSTANTS Mutation, Origin, Proven, Overlap, CrossAuthority, SongChanges,
          AllowReplacement, AllowOwnerChange, QueuePoint, ReadKind

Auth(g) == [generation |-> g, song |-> IF g = 2 /\ SongChanges THEN 2 ELSE 1]
A1 == Auth(1)
A2 == IF CrossAuthority THEN Auth(2) ELSE A1
Snapshot(body, authority) == [body |-> body, authority |-> authority]
Initial == Snapshot("local", A1)
Scope(editor, owner, authority) == [editor |-> editor, owner |-> owner, authority |-> authority]
Bodies == {"local", "auto", "chosen", "later", "replacement", "observed", "partial", "mutated"}
Authorities == {A1, Auth(2)}
Snapshots == [body : Bodies, authority : Authorities]
Scopes == [editor : {1, 2}, owner : {1, 2}, authority : Authorities]

VARIABLE s
vars == <<s>>
Current == Scope(s.editor, s.owner, s.authority)
ScopeMatches(c) == c = Current
WithoutOwnerMatches(c) == c.editor = s.editor /\ c.authority = s.authority
DecisionCurrent == WithoutOwnerMatches(s.context)
                   /\ (Mutation = "IgnoreDecisionOwner" \/ s.context.owner = s.owner)
CheckpointCurrent == WithoutOwnerMatches(s.checkpointScope)
                     /\ (Mutation = "IgnoreRedoOwner" \/ s.checkpointScope.owner = s.owner)
ReceiptCurrent == Proven /\ s.context = Scope(1, 1, A1)
ReadLifetimeFence == ~s.readCancelled /\ s.readScope.editor = s.editor
\* Prompt checks its sequence and captured clean baseline (abstract authority).
\* Latest checks its captured owner/cancelled ticket, not captured generation.
ReadFence == ReadLifetimeFence
             /\ IF ReadKind = "prompt"
                 THEN s.readSequence = s.sequence /\ s.readScope.authority = s.authority
                 ELSE s.readScope.owner = s.owner
AutoEligible == ~Overlap /\ (Origin = "rendered" \/ ReceiptCurrent)
Headless(seq) == SubSeq(seq, 1, Len(seq) - 1)

Init == s = [editor |-> 1, owner |-> 1, authority |-> A1, doc |-> Initial,
    pending |-> FALSE, resultUsed |-> FALSE, context |-> Scope(1, 1, A1),
    before |-> Initial, destination |-> A2,
    checkpoint |-> FALSE, checkpointScope |-> Scope(1, 1, A1),
    checkpointBefore |-> Initial, checkpointAfter |-> Initial, review |-> FALSE,
    undo |-> <<>>, redo |-> <<>>, laterUsed |-> FALSE,
    read |-> "idle", readScope |-> Scope(1, 1, A1), readSequence |-> 1, sequence |-> 1, readCancelled |-> FALSE,
    queued |-> FALSE, queueSeen |-> FALSE, queueScope |-> Scope(1, 1, A1),
    rearmTask |-> FALSE, rearmScope |-> Scope(1, 1, A1), armed |-> FALSE,
    quiet |-> FALSE,
    badSave |-> FALSE, badPlay |-> FALSE, badExport |-> FALSE, badMutation |-> FALSE,
    badDecision |-> FALSE, badObservation |-> FALSE, badUnproven |-> FALSE,
    oldOwnerPrompt |-> FALSE, oldOwnerRead |-> FALSE,
    resolved |-> FALSE, reviewed |-> FALSE, reviewedAndRedone |-> FALSE,
    normalSave |-> FALSE, normalPlay |-> FALSE, normalExport |-> FALSE,
    normalMutation |-> FALSE, replacementExport |-> FALSE,
    drainedPending |-> FALSE, drainedReview |-> FALSE]

\* A response establishes PendingMerge and its captured current authority.
\* The submitted-receipt/overlap classification is trusted input to this model.
OfferResult == /\ s.editor = 1 /\ s.authority = A1 /\ ~s.quiet /\ ~s.resultUsed /\ ~s.review /\ ~s.pending
               /\ s' = [s EXCEPT !.pending = TRUE, !.resultUsed = TRUE,
                        !.context = Current, !.before = s.doc, !.destination = A2]

\* Apply against the captured pending authority. Cross-authority RECHOICE must
\* remain a decision boundary even though the installed destination now matches.
Apply(automatic) ==
    /\ s.pending /\ DecisionCurrent
    /\ IF automatic THEN ~s.review /\ (AutoEligible \/ (Mutation = "UnprovenAuto" /\ Origin = "chat" /\ ~Overlap)) ELSE TRUE
    /\ LET before == IF s.review THEN s.before ELSE s.doc
           after == Snapshot(IF automatic THEN "auto" ELSE "chosen", s.destination)
           boundary == (before.authority # s.destination \/ s.review)
                       /\ ~(Mutation = "NakedRechoice" /\ s.review)
       IN s' = [s EXCEPT
          !.doc = after, !.authority = s.destination, !.pending = FALSE, !.review = FALSE,
          !.resolved = TRUE,
          !.badDecision = @ \/ ~ScopeMatches(s.context),
          !.badUnproven = @ \/ (automatic /\ Origin = "chat" /\ ~ReceiptCurrent),
          !.checkpoint = IF boundary THEN TRUE ELSE s.checkpoint,
          !.checkpointBefore = IF boundary THEN before ELSE s.checkpointBefore,
          !.checkpointAfter = IF boundary THEN after ELSE s.checkpointAfter,
          !.checkpointScope = IF boundary THEN Scope(s.editor, s.owner, s.destination) ELSE s.checkpointScope,
          !.undo = IF boundary THEN <<>> ELSE Append(s.undo, before), !.redo = <<>>,
          !.rearmTask = IF automatic THEN s.rearmTask ELSE Mutation # "NoRearm",
          !.rearmScope = IF automatic THEN s.rearmScope ELSE Scope(s.editor, s.owner, s.destination)]
AutoApply == Apply(TRUE)
Resolve == Apply(FALSE)
\* Resolve represents an admissible choice returned by NativeEditorMerge.
\* In particular it does not offer a local relative-song choice after source
\* replacement if the native merge engine declares that choice unsupported.

Edit == /\ ~s.quiet /\ ~s.laterUsed /\ ~s.pending
        /\ (~s.review \/ Mutation = "ReviewMutation")
        /\ s' = [s EXCEPT !.doc = Snapshot(IF s.review THEN "mutated" ELSE "later", s.authority),
                 !.undo = IF s.review THEN s.undo ELSE Append(s.undo, s.doc), !.redo = <<>>,
                 !.laterUsed = TRUE, !.badMutation = @ \/ s.review,
                 !.normalMutation = @ \/ ~s.review]
\* Review itself has pending=TRUE; a semantic mutation must bypass both layers.
ReviewEdit == /\ ~s.quiet /\ s.review /\ Mutation = "ReviewMutation"
              /\ s' = [s EXCEPT !.doc = Snapshot("mutated", s.authority), !.badMutation = TRUE]

UndoNormal == /\ ~s.quiet /\ ~s.review /\ Len(s.undo) > 0
              /\ s' = [s EXCEPT !.doc = s.undo[Len(s.undo)], !.undo = Headless(s.undo),
                       !.redo = Append(s.redo, s.doc)]
RedoNormal == /\ ~s.quiet /\ ~s.review /\ Len(s.redo) > 0
              /\ s' = [s EXCEPT !.doc = s.redo[Len(s.redo)], !.redo = Headless(s.redo),
                       !.undo = Append(s.undo, s.doc)]

\* undo() cancels old prompt/source work and latest drain, retaining its ticket.
UndoReview == /\ ~s.quiet /\ ~s.review /\ Len(s.undo) = 0 /\ s.checkpoint
              /\ ScopeMatches(s.checkpointScope)
              /\ s' = [s EXCEPT !.review = TRUE, !.pending = TRUE,
                       !.doc = IF Mutation = "PartialUndo" THEN Snapshot("partial", s.checkpointBefore.authority)
                               ELSE s.checkpointBefore,
                       !.before = s.checkpointBefore, !.destination = s.authority,
                       !.context = s.checkpointScope, !.reviewed = TRUE,
                       !.sequence = 2, !.readCancelled = s.read = "waiting" \/ s.readCancelled, !.armed = FALSE, !.rearmTask = FALSE]
RedoReview == /\ s.review /\ CheckpointCurrent
              /\ s' = [s EXCEPT !.review = FALSE, !.pending = FALSE,
                       !.doc = s.checkpointAfter, !.reviewedAndRedone = TRUE,
                       !.badDecision = @ \/ ~ScopeMatches(s.checkpointScope),
                       !.rearmTask = Mutation # "NoRearm", !.rearmScope = Current]

\* Actions record an execution event, so later navigation cannot erase a breach.
Save == /\ ((~s.pending /\ ~s.review) \/ (s.review /\ Mutation = "ReviewSave"))
        /\ s' = [s EXCEPT !.badSave = @ \/ s.review, !.normalSave = @ \/ ~s.review]
Play == /\ (~s.review \/ Mutation = "ReviewPlay")
        /\ s' = [s EXCEPT !.badPlay = @ \/ s.review, !.normalPlay = @ \/ ~s.review]
Export == /\ (~s.review \/ Mutation = "ReviewExport")
          /\ s' = [s EXCEPT !.badExport = @ \/ s.review, !.normalExport = @ \/ ~s.review,
                   !.replacementExport = @ \/ (~s.review /\ s.editor = 2)]

\* This callback is captured BEFORE a possible Undo or editor replacement.
\* Prompt has no captured-owner test in source. Latest-job observation does.
BeginRead == /\ ~s.quiet /\ s.read = "idle" /\ ~s.pending /\ ~s.review
             /\ s' = [s EXCEPT !.read = "waiting", !.readScope = Current,
                      !.readSequence = s.sequence, !.readCancelled = FALSE]
CompleteRead == /\ s.read = "waiting"
    /\ LET allowed == ReadFence \/ Mutation = "StaleObservation"
                   \/ (Mutation = "LatestOwnerFence" /\ ReadKind = "latest" /\ ReadLifetimeFence)
           deliver == allowed /\ ~s.resultUsed
       IN s' = [s EXCEPT !.read = "done",
           \* Acceptance creates a pending decision under CURRENT authority, as
           \* native code does after a read. An old receipt is not thereby valid.
           !.pending = IF deliver THEN TRUE ELSE s.pending,
           !.resultUsed = IF deliver THEN TRUE ELSE s.resultUsed,
           !.context = IF deliver THEN Current ELSE s.context,
           !.before = IF deliver THEN s.doc ELSE s.before,
           !.destination = IF deliver THEN A2 ELSE s.destination,
           !.badObservation = @ \/ (allowed /\ ~ReadFence),
           !.oldOwnerRead = @ \/ (allowed /\ s.readScope.owner # s.owner),
           !.oldOwnerPrompt = @ \/ (allowed /\ ReadKind = "prompt" /\ s.readScope.owner # s.owner)]

\* Dynamic owner change need not reset PendingMerge; its captured scope fences
\* explicit choice and Redo. No general document/API ownership theorem is claimed.
ChangeOwner == /\ AllowOwnerChange /\ ~s.quiet /\ s.owner = 1
               /\ s' = [s EXCEPT !.owner = 2]
Navigate == /\ AllowReplacement /\ ~s.quiet /\ s.editor = 1
            /\ s' = [s EXCEPT !.editor = 2, !.authority = Auth(2),
                     !.doc = Snapshot("replacement", Auth(2)), !.sequence = 2,
                     !.readCancelled = s.read = "waiting" \/ s.readCancelled,
                     !.pending = FALSE, !.checkpoint = FALSE, !.review = FALSE,
                     !.undo = <<>>, !.redo = <<>>, !.queued = FALSE,
                     !.armed = FALSE, !.rearmTask = FALSE]

QueueLatest == /\ ~s.quiet /\ ~s.queueSeen
               /\ ((QueuePoint = "pending" /\ s.pending /\ ~s.review /\ ~AutoEligible)
                   \/ (QueuePoint = "review" /\ s.review))
               /\ s' = [s EXCEPT !.queued = TRUE, !.queueSeen = TRUE, !.queueScope = Current]
\* The native rearm is a deferred MainActor task, not an atomic part of choice.
RunRearm == /\ s.rearmTask
            /\ s' = [s EXCEPT !.rearmTask = FALSE,
                     !.armed = ScopeMatches(s.rearmScope) /\ ~s.pending /\ ~s.review]
DrainLatest == /\ s.queued /\ s.armed /\ ~s.pending /\ ~s.review
               /\ s.queueScope.editor = s.editor
               /\ s' = [s EXCEPT !.queued = FALSE, !.armed = FALSE,
                        !.drainedPending = @ \/ QueuePoint = "pending",
                        !.drainedReview = @ \/ QueuePoint = "review"]

\* A progress assumption, NOT forced conflict resolution: the user eventually
\* finishes reviewing/choosing and stops editing/navigating. Queue arrival after
\* this point is excluded; existing queued work must still be serviced.
Quiesce == /\ s.resultUsed /\ ~s.pending /\ ~s.review /\ ~s.quiet /\ s.read # "waiting"
           /\ s' = [s EXCEPT !.quiet = TRUE]
Next == OfferResult \/ AutoApply \/ Resolve \/ Edit \/ ReviewEdit
        \/ UndoNormal \/ RedoNormal \/ UndoReview \/ RedoReview
        \/ Save \/ Play \/ Export \/ BeginRead \/ CompleteRead
        \/ ChangeOwner \/ Navigate \/ QueueLatest \/ RunRearm \/ DrainLatest \/ Quiesce
Spec == Init /\ [][Next]_vars
\* Progress configurations disable replacement/owner changes. TLC cannot take
\* eventual-quiescence as a specification conjunct: keep that assumption in
\* the checked implication instead, preserving the same conditional obligation.
SpecProgress == Spec /\ WF_vars(RunRearm) /\ WF_vars(DrainLatest)

BooleanFields == {"pending", "resultUsed", "checkpoint", "review", "laterUsed",
    "readCancelled", "queued", "queueSeen", "rearmTask", "armed", "quiet", "badSave",
    "badPlay", "badExport", "badMutation", "badDecision", "badObservation", "badUnproven",
    "oldOwnerPrompt", "oldOwnerRead", "resolved", "reviewed", "reviewedAndRedone", "normalSave", "normalPlay",
    "normalExport", "normalMutation", "replacementExport", "drainedPending", "drainedReview"}
TypeOK == /\ \A f \in BooleanFields : s[f] \in BOOLEAN
          /\ s.editor \in {1, 2} /\ s.owner \in {1, 2} /\ s.authority \in Authorities
          /\ s.doc \in Snapshots /\ s.context \in Scopes /\ s.checkpointScope \in Scopes
          /\ s.readScope \in Scopes /\ s.rearmScope \in Scopes /\ s.queueScope \in Scopes
          /\ s.before \in Snapshots /\ s.checkpointBefore \in Snapshots /\ s.checkpointAfter \in Snapshots
          /\ s.destination \in Authorities /\ s.sequence \in {1, 2} /\ s.readSequence \in {1, 2}
          /\ s.read \in {"idle", "waiting", "done"}
          /\ s.undo \in UNION { [1..n -> Snapshots] : n \in 0..2 }
          /\ s.redo \in UNION { [1..n -> Snapshots] : n \in 0..2 }
HistoricalSnapshot == s.review => s.doc = s.checkpointBefore
NoReviewSave == ~s.badSave
NoReviewPlay == ~s.badPlay
NoReviewExport == ~s.badExport
NoReviewMutation == ~s.badMutation
ExecutableAuthority == ~s.review => s.doc.authority = s.authority
NoStaleDecision == ~s.badDecision
NoStaleObservation == ~s.badObservation
NoUnprovenOverwrite == ~s.badUnproven
\* EXPECTED RESIDUAL for prompt + in-place owner change. This is not an asserted
\* app exploit: whether that lifecycle is reachable requires external mapping.
NoOldOwnerCompletion == ~s.oldOwnerRead
NoOldOwnerPromptCompletion == ~s.oldOwnerPrompt
QueuedObservationConverges == (<>s.quiet) => (s.queued ~> ~s.queued)

NeverResolved == ~s.resolved
NeverReviewedAndRedone == ~s.reviewedAndRedone
NeverNormalSave == ~s.normalSave
NeverNormalPlay == ~s.normalPlay
NeverNormalExport == ~s.normalExport
NeverNormalMutation == ~s.normalMutation
NeverReplacementExport == ~s.replacementExport
NeverDrainedAfterPending == ~s.drainedPending
NeverDrainedAfterReview == ~s.drainedReview
====
