---- MODULE SplitScreenPersistence ----
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS OmitDefinitions, DropEmptyDefinitions, IgnoreLayoutDirty,
          SplitMergeBundle, AppendRemovedMembers, AckCurrent, OverwriteCurrentOnAck,
          AllowInvalidSave, AllowInvalidAdoption, OverwriteCanonicalAsset,
          DropLegacyAsset, RetainFallbackDefinitions, DirtyOnLoad, WrongSourceOnLoad

\* Occurrences a/b share source S1; c has source S2. Occurrences are never assets.
Source == [o \in {"a", "b", "c"} |-> IF o = "c" THEN "S2" ELSE "S1"]
Bundle(slots, members, defs, lanes) ==
    [slots |-> slots, members |-> members, defs |-> defs, lanes |-> lanes]
Ordinary == Bundle(<<"a", "b">>, {}, "absent", "ordinary")
Two == Bundle(<<"a", "b">>, {"a", "b"}, "two", "two")
Layout == Bundle(<<"a", "b">>, {"a", "b"}, "weightedTwo", "layout")
Three == Bundle(<<"a", "b", "c">>, {"a", "b", "c"}, "three", "three")
ThreeLayout == Bundle(<<"a", "b", "c">>, {"a", "b", "c"}, "weightedThree", "threeLayout")
Removed == Bundle(<<"a", "b">>, {"a", "b"}, "two", "removed")
Unstacked == Bundle(<<"a", "b">>, {}, "empty", "unstacked")
Orphan == Bundle(<<"a", "b">>, {"a", "b"}, "empty", "invalid")
Values == {Ordinary, Two, Layout, Three, ThreeLayout, Removed, Unstacked, Orphan}
Slots(v) == {v.slots[i] : i \in 1..Len(v.slots)}
Valid(v) == /\ v.members \subseteq Slots(v)
            /\ IF v.defs \in {"absent", "empty"} THEN v.members = {}
               ELSE /\ v.defs \in {"two", "weightedTwo", "three", "weightedThree"}
                    /\ Cardinality(v.members) <= (IF v.defs \in {"three", "weightedThree"} THEN 3 ELSE 2)
\* Concrete geometry validation is deliberately outside this finite catalogue.

VARIABLES current, clean, submitted, request, durable, phase, saves, edits,
          mergeResult, mergePending, mergeConflict, failures, witnessed,
          loadKind, loadAsset, loadedAsset, loadedSource, loadedDirty
vars == <<current, clean, submitted, request, durable, phase, saves, edits,
          mergeResult, mergePending, mergeConflict, failures, witnessed,
          loadKind, loadAsset, loadedAsset, loadedSource, loadedDirty>>

Dirty == current.slots # clean.slots \/ current.members # clean.members
         \/ (~IgnoreLayoutDirty /\ current.defs # clean.defs)
Wire(v) == [v EXCEPT !.defs = IF OmitDefinitions THEN "absent"
                            ELSE IF DropEmptyDefinitions /\ v.defs = "empty" THEN "absent"
                            ELSE v.defs]
\* Successful authored Save stores a cleared section as absence. The request's
\* explicit empty tag remains distinct from omission, which retains prior state.
Canonical(v) == [v EXCEPT !.defs = IF v.defs = "empty" THEN "absent" ELSE v.defs]
ApplyWire(v) == IF v.defs = "absent" THEN [v EXCEPT !.defs = durable.defs] ELSE Canonical(v)
ExpectedAsset(kind, asset) == IF kind \in {"legacy", "unrepresented"} THEN "assetUUID" ELSE asset

Init == /\ current \in {Ordinary, Two, Three}
        /\ clean = current /\ durable = current /\ submitted = current /\ request = current
        /\ phase = "idle" /\ saves = 0 /\ edits = 0
        /\ mergeResult = current /\ mergePending = FALSE /\ mergeConflict = FALSE
        /\ failures = {} /\ witnessed = {}
        /\ loadKind = "none" /\ loadAsset = "missing" /\ loadedAsset = "missing"
        /\ loadedSource = "S1" /\ loadedDirty = FALSE

Edit(v) == /\ edits < 2 /\ v \in Values \ {Ordinary} /\ v # current
           /\ current' = v /\ edits' = edits + 1
           /\ UNCHANGED <<clean, submitted, request, durable, phase, saves,
                          mergeResult, mergePending, mergeConflict, failures, witnessed,
                          loadKind, loadAsset, loadedAsset, loadedSource, loadedDirty>>

BeginSave == /\ phase = "idle" /\ saves < 1 /\ Dirty /\ ~mergeConflict /\ ~mergePending
             /\ (Valid(current) \/ AllowInvalidSave)
             /\ submitted' = current /\ request' = Wire(current) /\ phase' = "pending"
             /\ failures' = failures
                 \cup (IF request' # submitted' THEN {"request"} ELSE {})
                 \cup (IF submitted' = Orphan THEN {"invalidSave"} ELSE {})
             /\ UNCHANGED <<current, clean, durable, saves, edits, mergeResult, mergePending,
                            mergeConflict, witnessed, loadKind, loadAsset, loadedAsset,
                            loadedSource, loadedDirty>>
\* The pending interval is a real await. Edit remains enabled throughout it.
Acknowledge == /\ phase = "pending"
               /\ durable' = ApplyWire(request)
               /\ clean' = IF AckCurrent THEN current ELSE submitted
               /\ current' = IF OverwriteCurrentOnAck THEN submitted ELSE current
               /\ phase' = "idle" /\ saves' = saves + 1
               /\ failures' = failures
                   \cup (IF durable' # Canonical(submitted) THEN {"durable"} ELSE {})
                   \cup (IF clean' # submitted THEN {"ack"} ELSE {})
                   \cup (IF current' # current THEN {"editLost"} ELSE {})
               /\ witnessed' = (IF submitted = Two THEN {"splitSave"} ELSE {})
                   \cup (IF submitted = Unstacked THEN {"unstack"} ELSE {})
                   \cup (IF submitted = Layout THEN {"layoutSave"} ELSE {})
                   \cup (IF current # submitted /\ current' = current /\ clean' = submitted THEN {"concurrent"} ELSE {})
               /\ UNCHANGED <<submitted, request, edits, mergeResult, mergePending,
                              mergeConflict, loadKind, loadAsset, loadedAsset, loadedSource, loadedDirty>>
FailSave == /\ phase = "pending" /\ phase' = "idle"
            /\ UNCHANGED <<current, clean, submitted, request, durable, saves, edits,
                           mergeResult, mergePending, mergeConflict, failures, witnessed,
                           loadKind, loadAsset, loadedAsset, loadedSource, loadedDirty>>
Reopen == /\ phase = "idle" /\ saves > 0 /\ current = clean
          /\ current' = durable /\ clean' = durable
          /\ witnessed' = (IF durable = Two THEN {"splitReopen"} ELSE {})
          /\ UNCHANGED <<submitted, request, durable, phase, saves, edits, mergeResult,
                         mergePending, mergeConflict, failures, loadKind, loadAsset,
                         loadedAsset, loadedSource, loadedDirty>>

\* Explicit section metadata means AI membership is authoritative, including removal.
StageAI == /\ phase = "idle" /\ current = Three /\ edits < 2
           /\ current' = IF AppendRemovedMembers THEN [Removed EXCEPT !.slots = Three.slots,
                                                                        !.members = Three.members] ELSE Removed
           /\ edits' = edits + 1
           /\ failures' = failures \cup (IF current' # Removed THEN {"membership"} ELSE {})
           /\ witnessed' = {"aiRemoval"}
           /\ UNCHANGED <<clean, submitted, request, durable, phase, saves, mergeResult,
                          mergePending, mergeConflict, loadKind, loadAsset, loadedAsset,
                          loadedSource, loadedDirty>>

\* Concrete conflict witness: local three-panel layout and remote panel removal both
\* change the coupled authored group. No result is adoptable until resolved.
Merge == /\ phase = "idle" /\ ~mergePending /\ ~mergeConflict
         /\ clean = Three /\ current = ThreeLayout
         /\ mergePending' = SplitMergeBundle /\ mergeConflict' = ~SplitMergeBundle
         /\ mergeResult' = IF SplitMergeBundle THEN [ThreeLayout EXCEPT !.lanes = Removed.lanes] ELSE current
         /\ failures' = failures \cup (IF mergePending' /\ mergeResult' \notin {ThreeLayout, Removed} THEN {"merge"} ELSE {})
         /\ UNCHANGED <<current, clean, submitted, request, durable, phase, saves, edits,
                        witnessed, loadKind, loadAsset, loadedAsset, loadedSource, loadedDirty>>
Resolve(v) == /\ phase = "idle" /\ mergeConflict /\ v \in {ThreeLayout, Removed}
              /\ mergeResult' = v /\ mergeConflict' = FALSE /\ mergePending' = TRUE
              /\ failures' = failures \cup (IF mergeResult' \notin {ThreeLayout, Removed} THEN {"merge"} ELSE {})
              /\ witnessed' = {"mergeResolved"}
              /\ UNCHANGED <<current, clean, submitted, request, durable, phase, saves, edits,
                             loadKind, loadAsset, loadedAsset, loadedSource, loadedDirty>>
\* Unsupported remote data can reach validation without being a supported edit.
ReceiveInvalidMerge == /\ phase = "idle" /\ ~mergePending /\ ~mergeConflict
                       /\ mergeResult' = Orphan /\ mergePending' = TRUE
                       /\ UNCHANGED <<current, clean, submitted, request, durable, phase, saves, edits,
                                      mergeConflict, failures, witnessed, loadKind, loadAsset,
                                      loadedAsset, loadedSource, loadedDirty>>
Adopt == /\ phase = "idle" /\ mergePending /\ (Valid(mergeResult) \/ AllowInvalidAdoption)
         /\ current' = mergeResult /\ mergePending' = FALSE
         /\ failures' = failures \cup (IF current' = Orphan THEN {"invalidAdoption"} ELSE {})
         /\ witnessed' = (IF current' \in {ThreeLayout, Removed} THEN {"mergeAdopted"} ELSE {})
         /\ UNCHANGED <<clean, submitted, request, durable, phase, saves, edits, mergeResult,
                        mergeConflict, loadKind, loadAsset, loadedAsset, loadedSource, loadedDirty>>

\* Canonical slot-ID and UUID-alias matching are observationally equivalent here.
Load(kind, asset) ==
    /\ kind \in {"canonicalSlot", "canonicalAlias", "fallback", "legacy", "unrepresented"}
    /\ asset \in {"missing", "null", "assetUUID", "opaqueAsset"}
    /\ loadKind' = kind /\ loadAsset' = asset
    /\ loadedSource' = IF WrongSourceOnLoad THEN Source["c"] ELSE Source["a"]
    /\ loadedDirty' = DirtyOnLoad
    /\ loadedAsset' = IF kind \in {"legacy", "unrepresented"}
                      THEN IF DropLegacyAsset THEN "missing" ELSE "assetUUID"
                      ELSE IF OverwriteCanonicalAsset /\ asset # "assetUUID" THEN "adapterUUID" ELSE asset
    /\ witnessed' = {kind}
    /\ UNCHANGED <<current, clean, submitted, request, durable, phase, saves, edits,
                   mergeResult, mergePending, mergeConflict, failures>>
FallbackOrdinary ==
    /\ phase = "idle" /\ current = Two
    /\ current' = IF RetainFallbackDefinitions THEN [Ordinary EXCEPT !.defs = Two.defs] ELSE Ordinary
    /\ clean' = IF RetainFallbackDefinitions THEN [Ordinary EXCEPT !.defs = Two.defs] ELSE Ordinary
    /\ failures' = failures \cup (IF current' # Ordinary \/ clean' # Ordinary THEN {"fallback"} ELSE {})
    /\ witnessed' = {"ordinaryFallback"}
    /\ UNCHANGED <<submitted, request, durable, phase, saves, edits, mergeResult, mergePending,
                   mergeConflict, loadKind, loadAsset, loadedAsset, loadedSource, loadedDirty>>

Next == (\E v \in Values : Edit(v)) \/ BeginSave \/ Acknowledge \/ FailSave \/ Reopen
        \/ StageAI \/ Merge \/ (\E v \in {ThreeLayout, Removed} : Resolve(v))
        \/ ReceiveInvalidMerge \/ Adopt \/ FallbackOrdinary
        \/ (\E k \in {"canonicalSlot", "canonicalAlias", "fallback", "legacy", "unrepresented"},
                 a \in {"missing", "null", "assetUUID", "opaqueAsset"} : Load(k, a))
Spec == Init /\ [][Next]_vars

RequestPreservesSubmission == "request" \notin failures
DurableMatchesSubmission == "durable" \notin failures /\ "ack" \notin failures
PostSubmitEditPreserved == "ack" \notin failures /\ "editLost" \notin failures
ValidCommit == "invalidSave" \notin failures
ValidAdoption == "invalidAdoption" \notin failures
MergeIsCoupled == "merge" \notin failures
AuthoritativeMembership == "membership" \notin failures
AssetProvenancePreserved == loadKind = "none" \/ loadedAsset = ExpectedAsset(loadKind, loadAsset)
CleanLoad == ~loadedDirty /\ loadedSource = "S1"
LayoutEditIsDirty == current = Layout /\ clean = Two => Dirty
OrdinaryFallbackClearsDefinitions == "fallback" \notin failures

\* These are deliberately false invariants used as positive reachability controls.
NeverSplitSave == "splitSave" \notin witnessed
NeverSplitReopen == "splitReopen" \notin witnessed
NeverUnstack == "unstack" \notin witnessed
NeverLayoutSave == "layoutSave" \notin witnessed
NeverConcurrentEdit == "concurrent" \notin witnessed
NeverAIRemoval == "aiRemoval" \notin witnessed
NeverMergeResolution == "mergeResolved" \notin witnessed
NeverMergeAdoption == "mergeAdopted" \notin witnessed
NeverCanonicalSlotLoad == "canonicalSlot" \notin witnessed
NeverCanonicalAliasLoad == "canonicalAlias" \notin witnessed
NeverFallbackLoad == "fallback" \notin witnessed
NeverLegacyLoad == "legacy" \notin witnessed
NeverUnrepresentedLoad == "unrepresented" \notin witnessed
NeverOrdinaryFallback == "ordinaryFallback" \notin witnessed
====
