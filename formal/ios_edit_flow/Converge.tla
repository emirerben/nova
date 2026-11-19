---- MODULE Converge ----
EXTENDS Naturals

\* The iPhone editor always shows the latest version. AI and manual edits apply on top and the
\* creator is never asked to resolve a conflict. Source map: converge-correspondence.md.
\*
\* Documents are abstract. What matters is which generation the editor sits on, who made the
\* unsaved edits, and which undo entry exists. The field-level merge inside
\* NativeEditorMerge.converge is not modelled: each converge either keeps the creator's edits
\* (`replaced` = FALSE) or lets the latest version replace them (`replaced` = TRUE).
\*
\* One record `s` holds the state. A mutated copy of this file (see verify_converge.py) can add
\* a field without editing every action.

CONSTANTS MaxGen,       \* server generations 1..MaxGen
          MaxIdent,     \* editor identities (the editor opens another edit) 1..MaxIdent
          MaxInteract   \* gestures, text entries and saves the creator starts, in total

Gens == 1..MaxGen
Idents == 1..MaxIdent

\* "none": nothing unsaved. "ai": only an AI-staged draft the server already holds.
\* "creator": the creator's own unsaved edits (possibly on top of a staged draft).
Provs == {"none", "ai", "creator"}

NoUndo == [on |-> FALSE, gen |-> 1, prov |-> "none"]
NoRead == [phase |-> "idle", ident |-> 1, clean |-> 1, gen |-> 1, head |-> FALSE]
NoConv == [prov |-> "none", owner |-> "empty", head |-> FALSE, absorbed |-> FALSE, after |-> "none"]

VARIABLE s
Init ==
  s = [ clean |-> 1,          \* generation of the last adopted (clean) document
        prov |-> "none",      \* provenance of the editor's unsaved changes
        undo |-> NoUndo,      \* at most one undo entry, with the generation it was made on
        server |-> 1,         \* latest generation on the server
        head |-> FALSE,       \* the server holds an AI draft head on the latest render
        ident |-> 1,          \* editor identity (editorOperationIdentity)
        busy |-> FALSE,       \* a gesture, text entry or save owns the document
        interacts |-> 0,
        read |-> NoRead,      \* the one in-flight read of the latest version
        convIn |-> NoConv ]   \* what the last converge was given and what it did with it

\* converge's `local` input and whose it is. The AI's staged lanes are replayed on the latest as the
\* AI's, never as the creator's; a fresh head on the latest supersedes them (`replayableEdits`).
OwnerOf(p, h) == IF p = "creator" THEN "creator" ELSE IF p = "ai" /\ ~h THEN "ai" ELSE "empty"

Stale == s.read.ident # s.ident \/ s.read.clean # s.clean

\* The latest version can overlap the replayed edits only when it holds something new. `replaced` is
\* the creator's edits replaced by the latest, or (for the AI's lanes) every lane the AI touched
\* already changed in the latest, e.g. it is a render of the same edit.
ReplaceOptions(g, h) == IF OwnerOf(s.prov, h) # "empty" /\ (g # s.clean \/ h) THEN BOOLEAN ELSE {FALSE}

ProvAfter(h, replaced) ==
  IF OwnerOf(s.prov, h) = "creator" /\ ~replaced THEN "creator"
  ELSE IF h THEN "ai"
  ELSE IF OwnerOf(s.prov, h) = "ai" /\ ~replaced THEN "ai"
  ELSE "none"

\* A replacement of the creator's edits leaves one undo entry that restores them on the new
\* generation. The AI's lanes never leave an undo entry. Otherwise an entry survives only when the
\* generation did not change.
UndoAfter(g, h, replaced) ==
  IF replaced /\ OwnerOf(s.prov, h) = "creator" THEN [on |-> TRUE, gen |-> g, prov |-> "creator"]
  ELSE IF g = s.clean THEN s.undo ELSE NoUndo

ConvergeTo(g, h, replaced) ==
  s' = [s EXCEPT !.clean = g, !.prov = ProvAfter(h, replaced), !.undo = UndoAfter(g, h, replaced),
                 !.read = NoRead,
                 !.convIn = [prov |-> s.prov, owner |-> OwnerOf(s.prov, h), head |-> h,
                             absorbed |-> (replaced /\ OwnerOf(s.prov, h) = "ai"),
                             after |-> ProvAfter(h, replaced)]]

\* ---- creator and environment actions ----

CreatorEdit ==
  s' = [s EXCEPT !.prov = "creator", !.undo = [on |-> TRUE, gen |-> s.clean, prov |-> s.prov]]

AIStage == ~s.head /\ s' = [s EXCEPT !.head = TRUE]

\* A render or publish lands. It supersedes any staged draft head.
ServerAdvance == s.server < MaxGen /\ s' = [s EXCEPT !.server = s.server + 1, !.head = FALSE]

\* The editor opens another edit: its own latest version, nothing carried over.
SwitchEditor ==
  /\ s.ident < MaxIdent
  /\ s' = [s EXCEPT !.ident = s.ident + 1, !.clean = s.server, !.prov = IF s.head THEN "ai" ELSE "none",
                    !.undo = NoUndo, !.busy = FALSE]

BeginInteract == ~s.busy /\ s.interacts < MaxInteract /\ s' = [s EXCEPT !.busy = TRUE, !.interacts = s.interacts + 1]
EndInteract == s.busy /\ s' = [s EXCEPT !.busy = FALSE]

Undo == s.undo.on /\ ~s.busy /\ s' = [s EXCEPT !.prov = s.undo.prov, !.undo = NoUndo]

\* ---- reading the latest version ----

ReadStart ==
  /\ s.read.phase = "idle"
  /\ s' = [s EXCEPT !.read = [phase |-> "flight", ident |-> s.ident, clean |-> s.clean,
                              gen |-> s.server, head |-> s.head]]

\* The answer arrives. A read whose editor moved on is dropped.
ReadReturn ==
  /\ s.read.phase = "flight"
  /\ s' = [s EXCEPT !.read = IF Stale THEN NoRead ELSE [s.read EXCEPT !.phase = "back"]]

DropStale == s.read.phase = "back" /\ Stale /\ s' = [s EXCEPT !.read = NoRead]

\* A gesture or text entry outlasted the wait bound: nothing changes, the edits stay, and the next
\* read tries again (`convergeQuietBound`, `.deferred`).
GiveUp == s.read.phase = "back" /\ ~Stale /\ s.busy /\ s' = [s EXCEPT !.read = NoRead]

\* Applying waits while a gesture or text entry owns the document.
ConvergeApply ==
  /\ s.read.phase = "back" /\ ~Stale /\ ~s.busy
  /\ \E replaced \in ReplaceOptions(s.read.gen, s.read.head) : ConvergeTo(s.read.gen, s.read.head, replaced)

\* ---- Save ----

SaveEnabled == s.prov # "none" /\ ~s.busy

\* Success on the current base. It cancels the in-flight read, which then restarts.
SaveOK ==
  /\ SaveEnabled /\ s.clean = s.server
  /\ s' = [s EXCEPT !.prov = "none", !.undo = NoUndo, !.head = FALSE, !.read = NoRead]

\* A baseline mismatch reads the latest version and converges onto it. A retry then succeeds.
SaveConflict ==
  /\ SaveEnabled /\ s.clean # s.server
  /\ \E replaced \in ReplaceOptions(s.server, s.head) : ConvergeTo(s.server, s.head, replaced)

\* Any other refusal shows an error and changes nothing.
SaveRefused == SaveEnabled /\ s' = s

Next ==
  \/ CreatorEdit \/ AIStage \/ ServerAdvance \/ SwitchEditor \/ BeginInteract \/ EndInteract \/ Undo
  \/ ReadStart \/ ReadReturn \/ DropStale \/ GiveUp \/ ConvergeApply
  \/ SaveOK \/ SaveConflict \/ SaveRefused

\* Fair reads: a started read returns, applies once the creator is quiet, and restarts.
\* Gestures are bounded (MaxInteract) and always end.
Spec == Init /\ [][Next]_s
        /\ WF_s(ReadStart) /\ WF_s(ReadReturn) /\ WF_s(DropStale) /\ WF_s(ConvergeApply) /\ WF_s(EndInteract)

\* ---- properties ----

TypeOK ==
  /\ s.clean \in Gens /\ s.server \in Gens /\ s.clean <= s.server
  /\ s.prov \in Provs /\ s.head \in BOOLEAN /\ s.ident \in Idents /\ s.busy \in BOOLEAN
  /\ s.undo.on \in BOOLEAN /\ s.undo.gen \in Gens /\ s.undo.prov \in Provs
  /\ s.convIn.owner \in {"empty", "ai", "creator"} /\ s.convIn.after \in Provs
  /\ s.read.phase \in {"idle", "flight", "back"}
  /\ s.interacts \in 0..MaxInteract

\* No state waits on a creator decision: some creator action (edit, Save or a sync read) is always
\* enabled. The model has no pending-decision state.
NoChoiceState ==
  \/ ENABLED CreatorEdit
  \/ ENABLED (SaveOK \/ SaveConflict \/ SaveRefused)
  \/ ENABLED ReadStart

\* AI-staged lanes are never passed to converge as the creator's edits: they are replayed as the
\* AI's (so no "your edits were replaced" notice and no creator Undo step) or superseded by a head.
AIEditsNeverCreators == s.convIn.prov = "ai" => s.convIn.owner # "creator"

\* An edit the chat reported as done never silently disappears: when the latest has no fresh head,
\* the AI's lanes stay staged unless the latest already changed every lane they touched.
AIEditNeverSilentlyDropped ==
  (s.convIn.prov = "ai" /\ ~s.convIn.head) => (s.convIn.after = "ai" \/ s.convIn.absorbed)

\* An undo entry always sits on the latest adopted generation.
UndoOnLatestBase == s.undo.on => s.undo.gen = s.clean

\* With fair reads and no further server changes the editor ends on the latest generation.
ShowsLatest == <>[](s.clean = s.server)

\* Unsaved changes either go away or Save becomes enabled.
SaveEventuallyEnabled == [](s.prov # "none" => <>(s.prov = "none" \/ SaveEnabled))
====
