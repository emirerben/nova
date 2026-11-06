---- MODULE SavedRenderReceipt ----
EXTENDS Naturals
CONSTANTS LegacyReceipt, LegacyWindow, AllowUnresolved, LateSaveWrite
VARIABLES generation, request, bindingRequest, bindingGeneration, fileRequest,
          read, queued, unresolved, exported, exportWasUnresolved, badExport,
          editor, saveEditor, saveRead, editorState, corrupted
vars == <<generation, request, bindingRequest, bindingGeneration, fileRequest,
          read, queued, unresolved, exported, exportWasUnresolved, badExport,
          editor, saveEditor, saveRead, editorState, corrupted>>

\* Requests 1/2 render generations 1/2. File 1 remains on disk through replacement.
\* fileRequest is the eligible presentation, not the mere existence of bytes.
Init == /\ generation = 1 /\ request = 1
        /\ bindingRequest = 1 /\ bindingGeneration = 1 /\ fileRequest = 1
        /\ read = "idle" /\ queued = FALSE /\ unresolved = FALSE
        /\ exported = 0 /\ badExport = FALSE /\ exportWasUnresolved = FALSE
        /\ editor = 1 /\ saveEditor = 0 /\ saveRead = "idle"
        /\ editorState = "idle" /\ corrupted = FALSE

Commit == /\ generation = 1 /\ generation' = 2
          /\ UNCHANGED <<request, bindingRequest, bindingGeneration, fileRequest,
                         read, queued, unresolved, exported, exportWasUnresolved, badExport,
                         editor, saveEditor, saveRead, editorState, corrupted>>
StartRead == /\ read = "idle" /\ read' = "fetch" /\ unresolved' = FALSE
             /\ UNCHANGED <<generation, request, bindingRequest, bindingGeneration,
                            fileRequest, queued, exported, exportWasUnresolved, badExport,
                            editor, saveEditor, saveRead, editorState, corrupted>>
\* A Save's reconcile coalesces while another ticket owns the status read.
Coalesce == /\ read \in {"fetch", "cancel", "snapshot"} /\ ~queued
            /\ queued' = TRUE /\ unresolved' = TRUE
            /\ UNCHANGED <<generation, request, bindingRequest, bindingGeneration,
                           fileRequest, read, exported, exportWasUnresolved, badExport,
                           editor, saveEditor, saveRead, editorState, corrupted>>
\* apply receives R2/G2 before awaiting previous.cancel(). Legacy code exposed
\* the new baseGeneration with the old request and presentation here.
Status == /\ read = "fetch" /\ generation = 2
          /\ read' = "cancel"
          /\ bindingRequest' = 0 /\ bindingGeneration' = 0 /\ fileRequest' = 0
          /\ UNCHANGED <<generation, request, queued, unresolved, exported,
                         exportWasUnresolved, badExport, editor, saveEditor, saveRead, editorState, corrupted>>
\* requests[key] changes before coordinator.snapshot() suspends.
Replace == /\ read = "cancel" /\ request' = 2 /\ read' = "snapshot"
           /\ UNCHANGED <<generation, bindingRequest, bindingGeneration, fileRequest,
                          queued, unresolved, exported, exportWasUnresolved, badExport,
                          editor, saveEditor, saveRead, editorState, corrupted>>
Snapshot == /\ read = "snapshot" /\ read' = "accepted"
            /\ bindingRequest' = request /\ bindingGeneration' = 2 /\ fileRequest' = request
            /\ UNCHANGED <<generation, request, queued, unresolved, exported,
                           exportWasUnresolved, badExport, editor, saveEditor, saveRead, editorState, corrupted>>
SameStatus == /\ read = "fetch" /\ generation = 1 /\ read' = "accepted"
              /\ UNCHANGED <<generation, request, bindingRequest, bindingGeneration,
                             fileRequest, queued, unresolved, exported, exportWasUnresolved, badExport,
                             editor, saveEditor, saveRead, editorState, corrupted>>
FailedRead == /\ read = "fetch" /\ read' = "accepted"
              /\ UNCHANGED <<generation, request, bindingRequest, bindingGeneration,
                             fileRequest, queued, unresolved, exported, exportWasUnresolved, badExport,
                             editor, saveEditor, saveRead, editorState, corrupted>>
Finish == /\ read = "accepted" /\ read' = "idle"
          /\ unresolved' = queued /\ queued' = FALSE
          /\ UNCHANGED <<generation, request, bindingRequest, bindingGeneration,
                         fileRequest, exported, exportWasUnresolved, badExport,
                         editor, saveEditor, saveRead, editorState, corrupted>>
Receipt == bindingGeneration = generation /\ bindingRequest = request
           /\ fileRequest = request /\ fileRequest # 0
Settled == read = "idle" /\ ~queued /\ ~unresolved
\* LegacyReceipt is the old independently-read base/request/presentation path.
\* Its retained disk file is R1 at either replacement suspension point.
Export == /\ (IF LegacyReceipt THEN generation = 2 /\ read = LegacyWindow
              ELSE Receipt /\ (AllowUnresolved \/ Settled))
          /\ exported' = IF LegacyReceipt THEN 1 ELSE fileRequest
          /\ badExport' = (badExport \/ ((IF LegacyReceipt THEN 1 ELSE fileRequest) # generation))
          /\ exportWasUnresolved' = ~Settled
          /\ UNCHANGED <<generation, request, bindingRequest, bindingGeneration,
                         fileRequest, read, queued, unresolved,
                         editor, saveEditor, saveRead, editorState, corrupted>>
\* Capture editor A, await committedRead, then write renderRetryNeeded only if
\* the current editor is still A. B is otherwise left idle.
BeginSave == /\ saveRead = "idle" /\ saveRead' = "waiting" /\ saveEditor' = editor
             /\ UNCHANGED <<generation, request, bindingRequest, bindingGeneration,
                            fileRequest, read, queued, unresolved, exported,
                            exportWasUnresolved, badExport, editor, editorState, corrupted>>
Navigate == /\ editor = 1 /\ editor' = 2 /\ editorState' = "idle"
            /\ UNCHANGED <<generation, request, bindingRequest, bindingGeneration,
                           fileRequest, read, queued, unresolved, exported,
                           exportWasUnresolved, badExport, saveEditor, saveRead, corrupted>>
FinishSave == /\ saveRead = "waiting" /\ saveRead' = "done"
              /\ editorState' = IF LateSaveWrite \/ editor = saveEditor THEN "retry" ELSE editorState
              /\ corrupted' = (corrupted \/ (LateSaveWrite /\ editor # saveEditor))
              /\ UNCHANGED <<generation, request, bindingRequest, bindingGeneration,
                             fileRequest, read, queued, unresolved, exported,
                             exportWasUnresolved, badExport, editor, saveEditor>>
Next == Commit \/ StartRead \/ Coalesce \/ Status \/ Replace \/ Snapshot \/ SameStatus
        \/ FailedRead \/ Finish \/ Export \/ BeginSave \/ Navigate \/ FinishSave
Spec == Init /\ [][Next]_vars
\* An export is an event: compare its file to the generation at that event.
ExportMatchesSaved == ~badExport
NoUnresolvedExport == ~exportWasUnresolved
ReplacementEditorUntouched == ~corrupted
NeverNewExport == exported # 2
====
