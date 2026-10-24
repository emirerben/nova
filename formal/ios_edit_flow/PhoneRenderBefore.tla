---- MODULE PhoneRenderBefore ----
\* One phone-rendered video (one DeviceRenderKey = job + variant) on one iPhone, as the
\* code ran it before the killer-screens fixes (origin/main ff27b0a65). Kept so verify.py
\* can prove the old code broke each finding rule; PhoneRender.tla is the fixed code.
\* Every action is one step the code really takes between two awaits. Line refs are
\* at ff27b0a65.
\*
\*   S  = src/apps/ios/Kria/Core/DeviceRenderSessions.swift
\*   C  = src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/DeviceRenderCoordinator.swift
\*   K  = src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/Capabilities.swift
\*   P  = src/apps/ios/Kria/Core/DeviceRendering.swift (publisher, failure reporter)
\*   E  = src/apps/ios/Kria/Features/NativeEditorSession.swift
\*   EV = src/apps/ios/Kria/Features/NativeEditorView.swift
\*   EC = src/apps/ios/Kria/Features/NativeEditorComponents.swift
\*   W  = src/apps/ios/Kria/Features/ChatWorkspaceView.swift
\*   D  = src/apps/ios/Kria/Features/DeviceRenderPanel.swift
\*   R  = src/apps/api/app/routes/device_render.py
\*   V  = src/apps/api/app/services/device_render.py
\*   X  = src/apps/api/app/tasks/device_render_reaper.py
\*
\* Identities: the server's pinned request is a number (sId). An editor Save or a save
\* on another device pins a new request (new number); POST /device-render/retry pins
\* recipe_revision + 1 (also a new number). Generations: Saves make 2, 3, ...; a phone
\* publish makes 100 + identity (R:1336-1351 render_generation_id = attempt id).
EXTENDS Naturals, FiniteSets

CONSTANTS MaxFaults,  \* network failures, app kills and backgroundings, in total
          MaxSaves,   \* editor Saves plus saves from another device
          MaxTicks,   \* preview poll ticks before "Preview needs another check" (E:7228, 300 x 1 s)
          MaxId,      \* identity bound; a liveness rule counts reaching it as done
          Conds,      \* phone-side conditions to start from (subset of none/hot/unsupported/missing)
          Approvals,  \* runtime-v2 chat approval on retry (subset of BOOLEAN)
          Events      \* optional events this run allows: "save", "other", "stop", "kill",
                      \* "background", "reaper", "sync" (the editor's draft sync, S2)

None == 0
Pub(k) == 100 + k

NoCoord == [id |-> 0, ph |-> "none", file |-> FALSE, run |-> FALSE, pc |-> "idle", cause |-> "none"]
NoPres  == [ph |-> "none", rsr |-> FALSE, cause |-> "none", pub |-> 0]
NoResp  == [ok |-> FALSE, id |-> 0, ph |-> "none", reason |-> "none", pub |-> 0]
NoRec   == [src |-> "none", caps |-> "ok", retry |-> FALSE, pc |-> "none", resp |-> NoResp]
NoRep   == [id |-> 0, reason |-> "none"]
EdIdle  == [pc |-> "idle", pend |-> 0, devId |-> 0, save |-> "idle", ppc |-> "off",
            ticks |-> 0, cause |-> "none", rflag |-> FALSE]

VARIABLES
  \* ---- server (assembly_plan._device_render_v1[variant] + variants[]) ----
  sPhase,    \* record status.phase: awaiting | syncing | published | attention (needs_attention)
  sId,       \* identity of the pinned request
  sReason,   \* reason_code when attention: transient | cancelled | origMissing | timedOut | structural
  vStatus,   \* variant render_status: awaiting | ready | attention
  vGen,      \* variant render_generation_id
  stale,     \* no poll for 24h (last_polled_at old), X:58-67
  reapedId,  \* identity the reaper already failed (reaped_at)
  pubIds,    \* ghost: identities the server published
  pubFrom,   \* ghost: identity whose recipe produced the published video
  \* ---- environment ----
  faults, hot, unsupported, missing, approval, saves, stops,
  \* ---- the app ----
  app,       \* open | killed
  chatCaps,  \* W capabilitiesAreAuthoritative (W:2205, cleared at W:2217)
  coord,     \* the DeviceRenderCoordinator receipt (on disk) + its running task
  req,       \* S requests[key] (0 = no entry)
  pres,      \* S presentations[key]: what the card shows
  obs,       \* S observations[key] is live
  rec,       \* the reconcile that holds tickets[key]
  oldRec,    \* a reconcile whose ticket was cleared after its fetch returned
  rep,       \* a fire-and-forget failure report in flight (C:210-213)
  hiReq,     \* ghost: newest request this app process installed
  banner,    \* editor "Couldn't refresh this edit" (E:1869-1871)
  ed         \* NativeEditorSession save / preview-poll state

vars == <<sPhase, sId, sReason, vStatus, vGen, stale, reapedId, pubIds, pubFrom,
          faults, hot, unsupported, missing, approval, saves, stops,
          app, chatCaps, coord, req, pres, obs, rec, oldRec, rep, hiReq, banner, ed>>
srv == <<sPhase, sId, sReason, vStatus, vGen, stale, reapedId, pubIds, pubFrom>>
cli == <<chatCaps, coord, req, pres, obs, rec, oldRec, rep, hiReq, banner, ed>>

Init ==
  /\ sPhase = "awaiting" /\ sId = 1 /\ sReason = "none" /\ vStatus = "awaiting" /\ vGen = 1
  /\ stale = FALSE /\ reapedId = 0 /\ pubIds = {} /\ pubFrom = 0
  /\ faults = MaxFaults
  \* At most one phone-side condition per run: too hot (ProcessInfo.thermalState serious or
  \* critical, cools later), a recipe needing a capability this phone's verified set lacks
  \* (K:19-20), or an original clip that is not on this iPhone (genuine, fixed by relinking).
  /\ \E cond \in Conds :
       /\ hot = (cond = "hot") /\ unsupported = (cond = "unsupported") /\ missing = (cond = "missing")
  /\ approval \in Approvals    \* runtime-v2 job: retry_requires_approval (R:275-293, :362-368)
  /\ saves = 0 /\ stops = 0
  /\ app = "open" /\ chatCaps = TRUE
  /\ coord = NoCoord /\ req = 0 /\ pres = NoPres /\ obs = FALSE
  /\ rec = NoRec /\ oldRec = NoRec /\ rep = NoRep /\ hiReq = 0 /\ banner = FALSE
  /\ ed = EdIdle

Busy == rec # NoRec
Fault == faults > 0

\* S:329-350 + K:16-44: the local decision, checked before anything starts.
Decision(caps) ==
  IF caps = "fail" THEN "cloudCaps"          \* E:812 a failed fetch becomes .disabled
  ELSE IF unsupported THEN "structural"
  ELSE IF hot THEN "hot"
  ELSE "local"

PresOf(c) == [ph |-> c.ph, rsr |-> FALSE, cause |-> c.cause, pub |-> 0]   \* S:307-327

\* The observer copies every receipt write to the card while it watches this request
\* (S:281-305); it stops once the receipt is settled and idle (S:298-300).
Show(c) ==
  /\ pres' = IF obs /\ req = c.id THEN PresOf(c) ELSE pres
  /\ obs' = (obs /\ req = c.id /\ (c.ph \in {"preparing", "rendering", "syncing"} \/ c.run))

\* E:7457 surfaceDeviceRenderFailure
SurfaceFail(p) == p.rsr \/ p.ph \in {"attention", "cancelled"} \/ (p.ph = "localReady" /\ p.cause # "none")

StartRec(src, caps, retry) == rec' = [src |-> src, caps |-> caps, retry |-> retry, pc |-> "fetch", resp |-> NoResp]

\* Clearing tickets[key] (S:213, S:266): a reconcile already past its fetch becomes stale.
ClearTicket == /\ rec' = NoRec
               /\ oldRec' = IF rec.pc = "apply" THEN rec ELSE oldRec

\* ---------------------------------------------------------------- server

\* Pin a new request for a new generation (editor-commit, plan_items.py:7635-7680, then
\* V:463 pin_device_request with base_generation = the new generation).
PinForSave ==
  /\ sId' = sId + 1 /\ sPhase' = "awaiting" /\ sReason' = "none"
  /\ vStatus' = "awaiting" /\ vGen' = saves + 2 /\ stale' = FALSE
  /\ saves' = saves + 1

\* R:856-898 POST /device-render/retry -> V:637 retry_device_render (revision + 1).
ServerRetryOK == sPhase = "attention" /\ ~approval /\ sId < MaxId
ServerRetry ==
  /\ sId' = sId + 1 /\ sPhase' = "awaiting" /\ sReason' = "none"
  /\ vStatus' = "awaiting" /\ stale' = FALSE

\* Chat "Retry approved edit" (W:2275-2297 -> kria/render_retry.py). It needs a failed
\* execution with recovery=retry: never there for a local-only failure, for a stop
\* (cancelled_by_user is in _NEEDS_CHANGE), or for a structural decline.
ApprovedRetryOK == sPhase = "attention" /\ sReason \notin {"cancelled", "structural"} /\ sId < MaxId

\* R:811-853 POST /failures -> V:592 mark_device_failed (only from awaiting/syncing).
FailableForReport == sPhase \in {"awaiting", "syncing"}
Deliver ==
  /\ rep # NoRep
  /\ IF rep.id = sId /\ FailableForReport
     THEN /\ sPhase' = "attention" /\ sReason' = rep.reason /\ vStatus' = "attention"
     ELSE UNCHANGED <<sPhase, sReason, vStatus>>    \* 409, swallowed by P:172-180
  /\ rep' = NoRep
  /\ UNCHANGED <<sId, vGen, stale, reapedId, pubIds, pubFrom, faults, hot, unsupported,
                 missing, approval, saves, stops, app, chatCaps, coord, req, pres, obs, rec, oldRec,
                 hiReq, banner, ed>>

DropReport ==                                       \* offline: the report is lost (C:212)
  /\ rep # NoRep /\ Fault
  /\ rep' = NoRep /\ faults' = faults - 1
  /\ UNCHANGED <<srv, hot, unsupported, missing, approval, saves, stops, app, chatCaps, coord, req,
                 pres, obs, rec, oldRec, hiReq, banner, ed>>

\* X:70-111: no poll for 24 h. A day only passes unnoticed while the app is not polling.
DayPasses ==
  /\ app = "killed" /\ "reaper" \in Events /\ sPhase \in {"awaiting", "syncing"} /\ ~stale
  /\ stale' = TRUE
  /\ UNCHANGED <<sPhase, sId, sReason, vStatus, vGen, reapedId, pubIds, pubFrom, faults, hot,
                 unsupported, missing, approval, saves, stops, app, cli>>
ReapablePhase == sPhase \in {"awaiting", "syncing"}
Reaper ==
  /\ stale /\ ReapablePhase /\ reapedId # sId
  /\ sPhase' = "attention" /\ sReason' = "timedOut" /\ vStatus' = "attention" /\ reapedId' = sId
  /\ UNCHANGED <<sId, vGen, stale, pubIds, pubFrom, faults, hot, unsupported, missing, approval,
                 saves, stops, app, cli>>

OtherDeviceSave ==                                   \* another device saves (editor notes N1)
  /\ saves < MaxSaves /\ "other" \in Events /\ sId < MaxId
  /\ PinForSave
  /\ UNCHANGED <<reapedId, pubIds, pubFrom, faults, hot, unsupported, missing, approval, stops, app, cli>>

Cool ==                                             \* the phone cools down
  /\ hot /\ hot' = FALSE
  /\ UNCHANGED <<srv, faults, unsupported, missing, approval, saves, stops, app, cli>>

\* ---------------------------------------------------------------- reconcile (S:115-188)

\* Chat poll (W:2224-2236) reconciles while the workspace is open, but only with
\* authoritative capabilities (W:2268-2272).
ChatPoll ==
  /\ app = "open" /\ chatCaps /\ ~Busy
  /\ StartRec("chat", "ok", FALSE)
  /\ UNCHANGED <<srv, faults, hot, unsupported, missing, approval, saves, stops, app, chatCaps, coord,
                 req, pres, obs, oldRec, rep, hiReq, banner, ed>>

\* S:121 GET /device-render. A success touches last_polled_at (R:369-379).
RecFetch ==
  /\ rec.pc = "fetch"
  /\ \/ /\ Fault
        /\ rec' = [rec EXCEPT !.pc = "apply", !.resp = NoResp]
        /\ faults' = faults - 1
        /\ UNCHANGED stale
     \/ /\ rec' = [rec EXCEPT !.pc = "apply",
                     !.resp = [ok |-> TRUE, id |-> sId, ph |-> sPhase, reason |-> sReason,
                               pub |-> IF sPhase = "published" THEN Pub(sId) ELSE 0]]
        /\ stale' = FALSE
        /\ UNCHANGED faults
  /\ UNCHANGED <<sPhase, sId, sReason, vStatus, vGen, reapedId, pubIds, pubFrom, hot,
                 unsupported, missing, approval, saves, stops, app, chatCaps, coord, req, pres, obs,
                 oldRec, rep, hiReq, banner, ed>>

\* The rest of reconcile after the fetch, run on the main actor without a network await
\* (coordinator calls are actor hops, not network). `own` = tickets[key] == ticket.
Apply(r, own) ==
  IF ~own                                            \* S:122, S:171 ticket guard
  THEN UNCHANGED <<coord, req, pres, obs, hiReq, rep>>
  ELSE IF ~r.resp.ok
  THEN \* S:170-187 catch: keep an in-flight coordinator, else S1 needsAttention.
       /\ pres' = IF coord.run /\ coord.id = req THEN pres
                  ELSE [ph |-> "attention", rsr |-> FALSE, cause |-> "prepFail", pub |-> 0]
       /\ UNCHANGED <<coord, req, obs, hiReq, rep>>
  ELSE LET new == req # r.resp.id
           \* S:130-135: cancel the old coordinator (receipt -> cancelled) and load the
           \* receipt on disk again; the directory is per key, not per identity (S:85-87).
           c1 == IF new /\ req # 0 /\ coord.id # 0
                 THEN [coord EXCEPT !.ph = "cancelled", !.run = FALSE, !.pc = "idle"]
                 ELSE IF new THEN [coord EXCEPT !.run = FALSE, !.pc = "idle"] ELSE coord
           id == r.resp.id
           obs1 == obs /\ ~new
           dec == Decision(r.caps)
       IN /\ req' = id
          /\ hiReq' = IF id > hiReq THEN id ELSE hiReq
          /\ UNCHANGED rep
          /\ IF r.resp.ph = "published"                   \* S:140-143
             THEN /\ coord' = c1 /\ obs' = FALSE
                  /\ pres' = [ph |-> "synced", rsr |-> FALSE, cause |-> "none", pub |-> r.resp.pub]
             ELSE IF r.resp.ph = "attention" \/ dec # "local"   \* S:145-158: no report, no start()
             THEN /\ coord' = c1 /\ obs' = FALSE
                  /\ pres' = [ph |-> IF c1.file THEN "localReady" ELSE "attention",
                              rsr |-> r.resp.ph = "attention",
                              cause |-> IF r.resp.ph = "attention" THEN r.resp.reason ELSE dec,
                              pub |-> 0]
             ELSE LET c2 ==
                        IF c1.id # id \/ r.retry
                        THEN \* C:229-250 start()
                             IF c1.id = id /\ (c1.run \/ c1.ph \in {"synced", "superseded"}) THEN c1
                             ELSE IF c1.id = id /\ c1.ph = "localReady"
                             THEN IF c1.file /\ ~c1.run THEN [c1 EXCEPT !.run = TRUE, !.pc = "prep"] ELSE c1
                             ELSE [id |-> id, ph |-> "preparing", file |-> FALSE, run |-> TRUE,
                                   pc |-> "prep", cause |-> "none"]
                        ELSE IF ~obs1 /\ ~c1.run /\ c1.id # 0
                                /\ c1.ph \notin {"cancelled", "superseded", "synced", "attention"}
                        THEN [c1 EXCEPT !.run = TRUE, !.pc = "prep"]      \* C:253 recover()
                        ELSE c1
                  IN /\ coord' = c2
                     \* S:281-305 observe
                     /\ pres' = IF c2.id = id THEN PresOf(c2) ELSE pres
                     /\ obs' = (c2.id = id /\ (c2.ph \in {"preparing", "rendering", "syncing"} \/ c2.run))

RecApply ==
  /\ rec.pc = "apply"
  /\ Apply(rec, TRUE)
  /\ rec' = NoRec
  /\ UNCHANGED <<srv, faults, hot, unsupported, missing, approval, saves, stops, app, chatCaps, oldRec,
                 banner, ed>>

OldApply ==
  /\ oldRec # NoRec
  /\ Apply(oldRec, FALSE)
  /\ oldRec' = NoRec
  /\ UNCHANGED <<srv, faults, hot, unsupported, missing, approval, saves, stops, app, chatCaps, rec,
                 banner, ed>>

\* ---------------------------------------------------------------- the coordinator (C:344-399)

Run(pc) == app = "open" /\ coord.run /\ coord.pc = pc

\* C:384-397 catch: no output file -> needsAttention + fire-and-forget report;
\* with a file -> localReady carrying the error (Retry sync), no report.
FailStep(cause, reason) ==
  LET c == IF coord.file
           THEN [coord EXCEPT !.ph = "localReady", !.cause = "syncFail", !.run = FALSE, !.pc = "idle"]
           ELSE [coord EXCEPT !.ph = "attention", !.cause = cause, !.run = FALSE, !.pc = "idle"]
  IN /\ coord' = c /\ Show(c)
     /\ rep' = IF coord.file THEN rep ELSE [id |-> coord.id, reason |-> reason]

Step(c) == coord' = c /\ Show(c) /\ UNCHANGED rep

ServerCurrent == sId = coord.id /\ sPhase \in {"awaiting", "syncing", "published"}   \* P:210-216

\* C:348-362: isCurrent, then resolve the clips (asset grants + originals). Both are
\* network calls before any output exists, so they fail the same way.
PerfPrep ==
  /\ Run("prep")
  /\ \/ /\ Fault /\ faults' = faults - 1
        /\ FailStep("blip", "transient")             \* URLError -> .unknown (C:144)
        /\ UNCHANGED stale
     \/ /\ UNCHANGED faults /\ stale' = FALSE         \* the GET touches last_polled_at
        /\ IF ~ServerCurrent
           THEN Step([coord EXCEPT !.ph = "superseded", !.run = FALSE, !.pc = "idle"])   \* C:350
           ELSE IF coord.file THEN Step([coord EXCEPT !.pc = "sync"])              \* C:354 intact MP4
           ELSE IF missing THEN FailStep("origMissing", "origMissing")             \* SourceAssetError
           ELSE Step([coord EXCEPT !.ph = "rendering", !.pc = "export"])           \* C:362
  /\ UNCHANGED <<sPhase, sId, sReason, vStatus, vGen, reapedId, pubIds, pubFrom, hot, unsupported,
                 missing, approval, saves, stops, app, chatCaps, req, rec, oldRec, hiReq, banner, ed>>

PerfExport ==                                        \* C:365-372
  /\ Run("export")
  /\ Step([coord EXCEPT !.ph = "localReady", !.file = TRUE, !.pc = "sync"])
  /\ UNCHANGED <<srv, faults, hot, unsupported, missing, approval, saves, stops, app, chatCaps, req, rec,
                 oldRec, hiReq, banner, ed>>

Background ==                                        \* no beginBackgroundTask around C:365
  /\ Run("export") /\ "background" \in Events /\ Fault
  /\ faults' = faults - 1
  /\ FailStep("background", "transient")             \* MediaEngineError -> export_failed (C:143)
  /\ UNCHANGED <<srv, hot, unsupported, missing, approval, saves, stops, app, chatCaps, req, rec, oldRec,
                 hiReq, banner, ed>>

\* C:375-378 isCurrent, then P:217-227 reserve the upload (R:741-808 sets syncing).
PerfSync ==
  /\ Run("sync")
  /\ \/ /\ Fault /\ faults' = faults - 1 /\ FailStep("blip", "transient")
        /\ UNCHANGED <<sPhase, stale>>
     \/ /\ UNCHANGED faults /\ stale' = FALSE
        /\ IF ~ServerCurrent
           THEN /\ UNCHANGED sPhase
                /\ Step([coord EXCEPT !.ph = "superseded", !.run = FALSE, !.pc = "idle"])   \* C:377
           ELSE IF sPhase \in {"awaiting", "syncing"}
           THEN /\ sPhase' = "syncing"
                /\ Step([coord EXCEPT !.ph = "syncing", !.pc = "complete"])
           ELSE /\ UNCHANGED sPhase /\ FailStep("blip", "transient")   \* reserve 409 (R:754)
  /\ UNCHANGED <<sId, sReason, vStatus, vGen, reapedId, pubIds, pubFrom, hot, unsupported,
                 missing, approval, saves, stops, app, chatCaps, req, rec, oldRec, hiReq, banner, ed>>

LateComplete == [coord EXCEPT !.ph = "superseded", !.run = FALSE, !.pc = "idle"]  \* P:238-239
CompleteOK == sId = coord.id /\ sPhase = "syncing"   \* R:1091-1097 via _record identity check

PerfComplete ==                                      \* P:236 -> R:1054-1351
  /\ Run("complete")
  /\ \/ /\ Fault /\ faults' = faults - 1 /\ FailStep("blip", "transient")
        /\ UNCHANGED <<sPhase, vStatus, vGen, pubIds, pubFrom>>
     \/ /\ UNCHANGED faults
        /\ IF CompleteOK
           THEN /\ sPhase' = "published" /\ vStatus' = "ready" /\ vGen' = Pub(sId)
                /\ pubIds' = pubIds \cup {coord.id} /\ pubFrom' = coord.id
                /\ Step([coord EXCEPT !.ph = "synced", !.run = FALSE, !.pc = "idle"])
           ELSE /\ UNCHANGED <<sPhase, vStatus, vGen, pubIds, pubFrom>>
                \* P:237-240: 409 -> isCurrent -> superseded (or rethrow while current)
                /\ IF ServerCurrent THEN FailStep("blip", "transient")
                   ELSE Step(LateComplete)
  /\ UNCHANGED <<sId, sReason, stale, reapedId, hot, unsupported, missing, approval, saves, stops, app,
                 chatCaps, req, rec, oldRec, hiReq, banner, ed>>

\* ---------------------------------------------------------------- app lifecycle

Kill ==                                              \* iOS kills the app; memory is gone
  /\ app = "open" /\ "kill" \in Events /\ Fault
  /\ app' = "killed" /\ faults' = faults - 1
  /\ coord' = [coord EXCEPT !.run = FALSE, !.pc = "idle"]   \* receipt stays on disk (C:290)
  /\ req' = 0 /\ pres' = NoPres /\ obs' = FALSE /\ rec' = NoRec /\ oldRec' = NoRec
  /\ rep' = NoRep /\ hiReq' = 0 /\ banner' = FALSE /\ ed' = EdIdle
  /\ UNCHANGED <<srv, hot, unsupported, missing, approval, saves, stops, chatCaps>>

Reopen ==                                            \* you open the project again
  /\ app = "killed"
  /\ app' = "open" /\ chatCaps' = TRUE
  /\ UNCHANGED <<srv, faults, hot, unsupported, missing, approval, saves, stops, coord, req, pres, obs,
                 rec, oldRec, rep, hiReq, banner, ed>>

\* ---------------------------------------------------------------- you, in the chat card

\* D:216-229 + D:163-178: which button the card shows.
StructuralCode(p) == p.rsr /\ p.cause \in {"structural"}
ShowsRetry ==
  /\ pres.ph \in {"attention", "cancelled", "localReady"}
  /\ ~(missing /\ pres.ph = "attention")              \* KRI-211 originalsMissing hides it
  /\ ~StructuralCode(pres)

\* "Stop rendering" (D:236-240 -> S:265-270 -> C:280-285).
\* The creator taps it at most once per run (like Saves, a bounded user event).
Stop ==
  /\ app = "open" /\ "stop" \in Events /\ pres.ph \in {"preparing", "rendering"}
  /\ stops = 0 /\ stops' = 1
  /\ ClearTicket
  /\ IF req # 0
     THEN LET c == IF coord.id # 0 THEN [coord EXCEPT !.ph = "cancelled", !.run = FALSE, !.pc = "idle"]
                   ELSE coord
          IN /\ coord' = c /\ pres' = PresOf(c) /\ obs' = FALSE
             /\ rep' = IF coord.id # 0 /\ coord.ph \notin {"cancelled", "synced", "superseded", "attention"}
                       THEN [id |-> coord.id, reason |-> "cancelled"] ELSE rep
     ELSE UNCHANGED <<coord, pres, obs, rep>>         \* no coordinator: the button does nothing
  /\ UNCHANGED <<srv, faults, hot, unsupported, missing, approval, saves, app, chatCaps, req, hiReq,
                 banner, ed>>


\* retryNeedsAttention (S:199-226): POST /device-render/retry, then drop the local
\* coordinator and receipt identity and clear the ticket. `after` is what the caller
\* does next (S:213-216; D:29-33 then retry()).
RetryNeedsAttention(after) ==
  /\ req # 0
  /\ UNCHANGED <<saves, stops>>
  /\ IF Fault
     THEN \* offline: S:217-224 writes a message only; also a 409 when the server is
          \* not needs_attention (R:878-879) or the job needs chat approval (R:880-884)
          /\ faults' = faults - 1
          /\ UNCHANGED <<srv, coord, req, pres, obs, rec, oldRec, chatCaps, ed>>
     ELSE /\ UNCHANGED faults
          /\ IF ServerRetryOK
             THEN /\ ServerRetry
                  /\ UNCHANGED <<vGen, reapedId, pubIds, pubFrom>>
                  /\ coord' = IF coord.id # 0
                              THEN [coord EXCEPT !.ph = "cancelled", !.run = FALSE, !.pc = "idle"]
                              ELSE coord
                  /\ req' = 0 /\ obs' = FALSE
                  /\ pres' = [NoPres EXCEPT !.ph = "preparing"]
                  /\ oldRec' = IF rec.pc = "apply" THEN rec ELSE oldRec
                  /\ after
             ELSE UNCHANGED <<srv, coord, req, pres, obs, rec, oldRec, chatCaps, ed>>

ChatAfterRetry ==                                    \* W:1825-1830 retry closure
  /\ chatCaps' = TRUE
  /\ StartRec("chat", "ok", TRUE)
  /\ UNCHANGED ed

EdAfterRetry ==                                      \* E:7439-7441 then refreshDeviceRender(retry: true)
  /\ rec' = NoRec
  /\ ed' = [ed EXCEPT !.pc = "refresh", !.rflag = TRUE]
  /\ UNCHANGED chatCaps

\* "Retry approved edit" from the chat (W:2275-2297). Server effect only.
ApprovedRetry ==
  /\ IF ApprovedRetryOK
     THEN /\ ServerRetry /\ UNCHANGED <<vGen, reapedId, pubIds, pubFrom, saves, stops>>
     ELSE UNCHANGED srv
  /\ UNCHANGED <<saves, stops>>

\* The chat card's Try again (D:216-226 and D:19-35).
ChatTryAgain ==
  /\ app = "open" /\ ShowsRetry
  \* D:22-24 + D:29-31: with retryApproved set, both buttons go to the chat approval,
  \* except re-sending a finished local file (Retry sync, not a server retry).
  /\ IF approval /\ (pres.rsr \/ pres.ph # "localReady")
     THEN /\ ApprovedRetry
          /\ UNCHANGED <<faults, chatCaps, coord, req, pres, obs, rec, oldRec, ed>>
     ELSE IF pres.rsr                                 \* D:217-226: requiresServerRetry
     THEN /\ RetryNeedsAttention(ChatAfterRetry)
     ELSE \* W:1826-1827: refreshCapabilities, then reconcile(retry: true)
          /\ UNCHANGED <<srv, saves, stops, coord, req, pres, obs, oldRec, ed>>
          /\ IF Fault
             THEN /\ faults' = faults - 1 /\ chatCaps' = FALSE /\ UNCHANGED rec   \* W:2216-2220
             ELSE /\ UNCHANGED faults /\ chatCaps' = TRUE
                  /\ IF ~Busy THEN StartRec("chat", "ok", TRUE) ELSE UNCHANGED rec
  /\ UNCHANGED <<hot, unsupported, missing, approval, app, rep, hiReq, banner>>

Relink ==                                            \* D:59-99 Find original files
  /\ app = "open" /\ missing /\ pres.ph = "attention"
  /\ missing' = FALSE
  /\ UNCHANGED <<srv, faults, hot, unsupported, approval, saves, stops, app, cli>>

\* ---------------------------------------------------------------- the editor

\* E:7218-7226 startPreviewRefresh
StartPreview(e) ==
  IF SurfaceFail(pres)
  THEN [e EXCEPT !.save = "deviceRetry", !.cause = pres.cause, !.ppc = "off", !.pc = "idle"]
  ELSE [e EXCEPT !.save = "previewPending", !.ppc = "tick", !.ticks = 0, !.pc = "idle", !.cause = "none"]

\* You tap Save (E:5588-5735): the server pins a new request for a new generation; a
\* running preview poll is cancelled (E:7219), and if it was inside its reconcile's GET
\* that GET throws URLError(.cancelled), which S:168 does not catch as cancellation.
EdSave ==
  /\ app = "open" /\ "save" \in Events /\ ed.pc = "idle" /\ saves < MaxSaves /\ sId < MaxId
  /\ PinForSave
  /\ ed' = [ed EXCEPT !.pc = "refresh", !.pend = saves + 2, !.save = "previewPending",
                      !.ppc = "off", !.ticks = 0, !.rflag = FALSE, !.cause = "none"]
  /\ rec' = IF rec.src = "edpoll" /\ rec.pc = "fetch"
            THEN [rec EXCEPT !.pc = "apply", !.resp = NoResp] ELSE rec
  /\ UNCHANGED <<reapedId, pubIds, pubFrom, faults, hot, unsupported, missing, approval, stops, app,
                 chatCaps, coord, req, pres, obs, oldRec, rep, hiReq, banner>>

\* E:810-814 refreshDeviceRender: capabilities first (an error becomes .disabled), then
\* reconcile, which returns at once when another reconcile holds the ticket (S:116).
EdRefresh ==
  /\ app = "open" /\ ed.pc = "refresh"
  /\ \E caps \in IF Fault THEN {"ok", "fail"} ELSE {"ok"} :
       /\ faults' = IF caps = "fail" THEN faults - 1 ELSE faults
       /\ IF ~Busy
          THEN /\ StartRec("ed", caps, ed.rflag) /\ ed' = [ed EXCEPT !.pc = "waitRec"]
          ELSE /\ UNCHANGED rec /\ ed' = [ed EXCEPT !.pc = "after"]
  /\ UNCHANGED <<srv, hot, unsupported, missing, approval, saves, stops, app, chatCaps, coord, req, pres,
                 obs, oldRec, rep, hiReq, banner>>

\* E:5734-5735 / E:7442-7444: remember the identity reconcile accepted, start the poll.
EdAfter ==
  /\ app = "open"
  /\ \/ ed.pc = "after"
     \/ ed.pc = "waitRec" /\ rec.src # "ed"
  /\ ed' = StartPreview([ed EXCEPT !.devId = req])
  /\ UNCHANGED <<srv, faults, hot, unsupported, missing, approval, saves, stops, app, chatCaps, coord,
                 req, pres, obs, rec, oldRec, rep, hiReq, banner>>

GiveUp(e) == IF e.ticks + 1 >= MaxTicks
             THEN [e EXCEPT !.save = "previewFailed", !.ppc = "off", !.ticks = 0]
             ELSE [e EXCEPT !.ppc = "tick", !.ticks = e.ticks + 1]

\* E:7227-7262 one poll tick: GET the variant; on needs_attention or a ready variant of
\* another generation, reconcile the phone first (and return at once if another
\* reconcile holds the ticket). Simplification: the apply step below reads the variant
\* again instead of keeping the copy read here.
PollTick ==
  /\ app = "open" /\ ed.ppc = "tick"
  /\ IF Fault
     THEN /\ faults' = faults - 1 /\ ed' = GiveUp(ed) /\ UNCHANGED rec   \* E:7238-7244 continue
     ELSE LET refresh == vStatus = "attention" \/ (vStatus = "ready" /\ vGen # ed.pend)
          IN /\ UNCHANGED faults
             /\ IF refresh /\ ~Busy
                THEN /\ StartRec("edpoll", "ok", FALSE) /\ ed' = [ed EXCEPT !.ppc = "waitRec"]
                ELSE /\ UNCHANGED rec /\ ed' = [ed EXCEPT !.ppc = "apply"]
  /\ UNCHANGED <<srv, hot, unsupported, missing, approval, saves, stops, app, chatCaps, coord, req, pres,
                 obs, oldRec, rep, hiReq, banner>>

PollWait ==
  /\ app = "open" /\ ed.ppc = "waitRec" /\ rec.src # "edpoll"
  /\ ed' = [ed EXCEPT !.ppc = "apply"]
  /\ UNCHANGED <<srv, faults, hot, unsupported, missing, approval, saves, stops, app, chatCaps, coord,
                 req, pres, obs, rec, oldRec, rep, hiReq, banner>>

\* E:7296-7342 applyPreviewVariant, including the device identity fence (E:7303-7310).
Fence == vStatus = "ready" /\ pres.pub = vGen /\ ed.devId # 0 /\ req = ed.devId
PollApply ==
  /\ app = "open" /\ ed.ppc = "apply"
  /\ ed' = IF SurfaceFail(pres)
           THEN [ed EXCEPT !.save = "deviceRetry", !.cause = pres.cause, !.ppc = "off", !.ticks = 0]
           ELSE IF (vGen = ed.pend \/ Fence) /\ vStatus = "ready"
           THEN [ed EXCEPT !.save = "saved", !.pend = 0, !.ppc = "off", !.ticks = 0, !.devId = 0]
           ELSE GiveUp(ed)
  /\ UNCHANGED <<srv, faults, hot, unsupported, missing, approval, saves, stops, app, chatCaps, coord,
                 req, pres, obs, rec, oldRec, rep, hiReq, banner>>

\* Banner "Retry device render" / "Review retry in chat" (EC:106-117, E:7435-7447).
EdRetry ==
  /\ app = "open" /\ ed.save = "deviceRetry" /\ ed.pc = "idle" /\ ed.pend # 0
  /\ IF approval                                     \* EV:322-324 -> chat (S1-k)
     THEN /\ ApprovedRetry
          /\ UNCHANGED <<faults, chatCaps, coord, req, pres, obs, rec, oldRec, ed>>
     ELSE IF pres.rsr
     THEN RetryNeedsAttention(EdAfterRetry)
     ELSE /\ ed' = [ed EXCEPT !.pc = "refresh", !.rflag = TRUE]
          /\ UNCHANGED <<srv, saves, stops, faults, chatCaps, coord, req, pres, obs, rec, oldRec>>
  /\ UNCHANGED <<hot, unsupported, missing, approval, app, rep, hiReq, banner>>

EdCheckAgain ==                                      \* "Check preview again" (E:7286-7290)
  /\ app = "open" /\ ed.save = "previewFailed" /\ ed.pc = "idle"
  /\ ed' = StartPreview(ed)
  /\ UNCHANGED <<srv, faults, hot, unsupported, missing, approval, saves, stops, app, chatCaps, coord,
                 req, pres, obs, rec, oldRec, rep, hiReq, banner>>

\* Chat events start the editor's draft sync (E:1709-1871). A failed read sets
\* "Couldn't refresh this edit"; the next successful one clears it.
PromptSync ==
  /\ app = "open" /\ ed.pc = "idle" /\ "sync" \in Events
  /\ IF Fault THEN /\ banner' = TRUE /\ faults' = faults - 1
     ELSE /\ banner' = FALSE /\ UNCHANGED faults
  /\ UNCHANGED <<srv, hot, unsupported, missing, approval, saves, stops, app, chatCaps, coord, req, pres,
                 obs, rec, oldRec, rep, hiReq, ed>>

\* ---------------------------------------------------------------- spec

Next ==
  \/ ChatPoll \/ RecFetch \/ RecApply \/ OldApply
  \/ PerfPrep \/ PerfExport \/ PerfSync \/ PerfComplete
  \/ Deliver \/ DropReport \/ Background \/ Kill \/ Reopen \/ DayPasses \/ Reaper
  \/ OtherDeviceSave \/ Cool
  \/ Stop \/ ChatTryAgain \/ Relink
  \/ EdSave \/ EdRefresh \/ EdAfter \/ PollTick \/ PollWait \/ PollApply
  \/ EdRetry \/ EdCheckAgain \/ PromptSync

\* The app and server keep working; a day passing, kills, faults and saves are optional.
SysFair ==
  /\ WF_vars(ChatPoll) /\ WF_vars(RecFetch) /\ WF_vars(RecApply) /\ WF_vars(OldApply)
  /\ WF_vars(PerfPrep) /\ WF_vars(PerfExport) /\ WF_vars(PerfSync)
  /\ WF_vars(PerfComplete) /\ WF_vars(Deliver)
  /\ WF_vars(EdRefresh) /\ WF_vars(EdAfter) /\ WF_vars(PollTick) /\ WF_vars(PollWait)
  /\ WF_vars(PollApply) /\ WF_vars(Cool) /\ WF_vars(Reopen)

\* A patient user who keeps tapping whatever retry the screen offers.
UserFair ==
  /\ SF_vars(ChatTryAgain) /\ SF_vars(EdRetry) /\ SF_vars(EdCheckAgain) /\ SF_vars(Relink)

Spec == Init /\ [][Next]_vars /\ SysFair
SpecUser == Spec /\ UserFair

\* ---------------------------------------------------------------- rules

Genuine == {"origMissing", "timedOut"}   \* an original is missing; the phone was away a day

\* FINDING rules (PhoneRenderFindings.cfg) ------------------------------------------

\* The app never shows needs-attention / "couldn't prepare" / "couldn't refresh" unless
\* a genuine, user-fixable failure happened.
NoSpuriousAttention ==
  /\ (pres.ph = "attention" => pres.cause \in Genuine)
  /\ (ed.save = "deviceRetry" => ed.cause \in Genuine)
  /\ ~banner

\* When the phone decides it cannot render locally, the server is told.
\* Told: a report for the pinned request is on its way. ServerWaiting: the server still
\* waits for this phone. NoReportInFlight: nothing is on its way. (Before the fix the
\* coordinator's fire-and-forget report was the only channel and there was no pause.)
Told == rep # NoRep /\ rep.id = sId
ServerWaiting == sPhase \in {"awaiting", "syncing"}
NoReportInFlight == rep = NoRep

\* The decision on the card is about req; a request pinned since is not decided yet.
CloudDecisionReported ==
  (pres.ph \in {"attention", "localReady"} /\ pres.cause \in {"cloudCaps", "structural", "hot"}
     /\ req = sId)
    => (~ServerWaiting \/ Told)

\* From every needs-attention screen, the retries on screen (or the app by itself) reach
\* a published video.
Shown == app = "open" /\ (pres.ph = "attention" \/ ed.save = "deviceRetry")
Resolved == sId = MaxId \/ (sPhase = "published" /\ pres.ph # "attention" /\ ed.save # "deviceRetry")
AttentionHasWayOut == Shown ~> Resolved

\* The phone and the server do not stay in contradicting states (phone: stopped / needs
\* attention; server: still waiting for this phone) without anyone tapping anything.
Contradict == app = "open" /\ pres.ph \in {"attention", "cancelled"}
              /\ ServerWaiting /\ NoReportInFlight /\ ~coord.run
ClientServerAgree == Contradict ~> ~Contradict

\* After a save, the editor eventually stops waiting and shows the new video.
PreviewConverges == ed.pend # 0 ~> (ed.pend = 0 \/ sId = MaxId)

\* PASSING rules (PhoneRender.cfg) --------------------------------------------------

\* The server never marks a request published with a video rendered for another request.
NoStalePublish == sPhase = "published" => pubFrom = sId

\* A published request never turns into needs_attention afterwards.
PublishedStaysPublished == sId \in pubIds => sPhase = "published"

\* The phone says "synced" only for a request the server really published.
SyncedMeansPublished == coord.ph = "synced" => coord.id \in pubIds

\* A late status answer never reinstalls an older request over a newer one.
NoStaleRequestReinstalled == req = 0 \/ req = hiReq

\* Sanity: with no faults the model can publish (verify.py expects this to break).
NeverPublished == sPhase # "published"

TypeOK ==
  /\ sPhase \in {"awaiting", "syncing", "published", "attention"}
  /\ faults \in 0..MaxFaults
====
