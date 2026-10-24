---- MODULE PhoneRender ----
\* One phone-rendered video (one DeviceRenderKey = job + variant) on one iPhone, as the
\* code runs it on codex/killer-integration (the killer-screens fixes). The code before
\* those fixes is PhoneRenderBefore.tla (origin/main ff27b0a65). Every action is one step
\* the code really takes between two awaits.
\*
\*   S  = src/apps/ios/Kria/Core/DeviceRenderSessions.swift
\*   C  = src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/DeviceRenderCoordinator.swift
\*   K  = src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/Capabilities.swift
\*   P  = src/apps/ios/Kria/Core/DeviceRendering.swift (publisher, failure reporter)
\*   E  = src/apps/ios/Kria/Features/NativeEditorSession.swift
\*   EC = src/apps/ios/Kria/Features/NativeEditorComponents.swift
\*   W  = src/apps/ios/Kria/Features/ChatWorkspaceView.swift
\*   D  = src/apps/ios/Kria/Features/DeviceRenderPanel.swift
\*   R  = src/apps/api/app/routes/device_render.py
\*   V  = src/apps/api/app/services/device_render.py
\*   X  = src/apps/api/app/tasks/device_render_reaper.py
\*
\* Identities: the server's pinned request is a number (sId). An editor Save or a save
\* on another device pins a new request (new number); POST /device-render/retry on a
\* needs_attention record pins recipe_revision + 1 (also a new number). Generations: Saves
\* make 2, 3, ...; a phone publish makes 100 + identity (render_generation_id = attempt id).
\*
\* Decline reasons: the phone declines a recipe it will never render as "unsupported"
\* (unsupported_recipe: a capability its verified set lacks, K:19-20) or "outdated"
\* (renderer_outdated: this build is older than the recipe). The card turns each into the
\* creator's turn with its own way forward (D changeReasons / updateReason): "This edit
\* needs a change" + "Review the updated edit" (chat), or "Update Kria" + "Review in chat".
\*
\* Builds (round 3): levels stand for client protocols. An older build on another device is
\* level OLD = 1; this iPhone starts at 2 and an update takes it to 3 or 4. A recipe needs
\* level `need` (2, or 4 for an outdated one). Two kinds of version gap: "floor" (a feature
\* the server gates by protocol, R _client_update_detail: the GET answers 426 naming the
\* request) and "schema" (a recipe/renderer version the server cannot see: the GET returns
\* the recipe and the phone's own gate says outdated, K:23-38). The server records the
\* declining level (declined_client_protocol) and the newest level that read the recipe
\* without a 426 (max_reader_client_protocol, kept across re-pins of the same recipe).
\*
\* Not modelled (see findings.md "Scope"): the rollout hold (phone_rendering_paused), 409
\* superseded / cancelled / editor_empty / 426 answers on GET, contract refusals, output
\* verification, the automatic-retry caps (12 re-reads, 4 transient waits, 12 decline
\* sends: never reached with MaxFaults <= 1), and the editor's "reconnecting" hint (shown
\* after 3 failed refresh reads). The phone's own clear of a renderer_outdated it can render
\* now (S apply, needs_attention: "renderable_now") is modelled; it shows when an operator
\* lowers a protocol floor ("rollback") for the build that declined.
EXTENDS Naturals, FiniteSets

CONSTANTS MaxFaults,  \* network failures, app kills and backgroundings, in total
          MaxSaves,   \* editor Saves plus saves from another device
          MaxTicks,   \* answered preview polls before "taking longer than expected" (E:7637, 300)
          MaxId,      \* identity bound; a liveness rule counts reaching it as done
          Conds,      \* phone-side conditions to start from (subset of
                      \* none/hot/unsupported/outdated/schema/newschema/missing): "outdated"
                      \* is a protocol-floor gap (426), "schema" one the server cannot see,
                      \* "newschema" a schema gap that only a later save ("feature") brings
          Approvals,  \* runtime-v2 job (subset of BOOLEAN); routes nothing any more (R:535)
          Chat,       \* is the chat workspace (and its poll) under the editor? (subset of
                      \* BOOLEAN). FALSE: the editor opened from the gallery (EditorViews:265)
          Events      \* optional events this run allows: "save", "other", "stop", "kill",
                      \* "background", "reaper", "sync" (the editor's draft sync), "caps"
                      \* (the phone loses a verified capability, also mid-render), "old" (a
                      \* device on an older build declines or retries), "feature" (another
                      \* device's save adds a feature this build can't decode), "rollback"
                      \* (an operator lowers the protocol floor of the recipe)

None == 0
Pub(k) == 100 + k
OLD == 1                 \* the older build on another device
Max(a, b) == IF a > b THEN a ELSE b

NoCoord == [id |-> 0, ph |-> "none", file |-> FALSE, run |-> FALSE, pc |-> "idle", cause |-> "none"]
\* wait: DeviceRenderPresentation.waiting (S:25-39): none | quiet (connection/server) | cooling
NoPres  == [ph |-> "none", rsr |-> FALSE, cause |-> "none", pub |-> 0, wait |-> "none"]
\* upd: a 426 client_update_required naming request id (phase ph); no recipe came with it.
\* need: the level the answered request's recipe needs (a stale answer is about its own recipe).
NoResp  == [ok |-> FALSE, upd |-> FALSE, id |-> 0, ph |-> "none", reason |-> "none", pub |-> 0,
            paused |-> FALSE, need |-> 0]
NoRec   == [src |-> "none", retry |-> FALSE, pc |-> "none", resp |-> NoResp]
\* The coordinator's pending report (C:338-375): live = a delivery task is running in this
\* process; disk = it is still persisted on the receipt (survives a kill).
NoRep   == [id |-> 0, reason |-> "none", live |-> FALSE, disk |-> FALSE]
\* S declines[key] (S:262-272): the identity, its reason and whether it is still being sent
\* ("sending") or the server already answered it ("done": accepted or rejected).
NoDecl  == [id |-> 0, reason |-> "none", st |-> "none"]
EdIdle  == [pc |-> "idle", pend |-> 0, devId |-> 0, save |-> "idle", ppc |-> "off",
            ticks |-> 0, cause |-> "none", rflag |-> FALSE, from |-> 0, seen |-> 0,
            caps |-> FALSE, recPend |-> FALSE]

VARIABLES
  \* ---- server (assembly_plan._device_render_v1[variant] + variants[]) ----
  sPhase,    \* record status.phase: awaiting | syncing | published | attention (needs_attention)
  sId,       \* identity of the pinned request
  sReason,   \* reason_code when attention: origMissing | timedOut | unsupported | outdated
  sPaused,   \* record paused_by_user (V:659-690); counts only on a live phase
  vStatus,   \* variant render_status: awaiting | ready | attention
  vGen,      \* variant render_generation_id
  stale,     \* no poll for 24h (last_polled_at old)
  reapedId,  \* identity the reaper already failed (reaped_at)
  pubIds,    \* ghost: identities the server published
  pubFrom,   \* ghost: identity whose recipe produced the published video
  futile,    \* ghost: the server re-pinned the same recipe for a phone that declines it again
  sDecl,     \* declined_client_protocol: level of the build whose renderer_outdated failed it (0: none)
  sFloor,    \* declined_gap = floor: that build was below a protocol floor the server checks
  sRead,     \* max_reader_client_protocol: newest level that read the recipe without a 426
  autoRepins,\* ghost: automatic re-pins of an outdated decline (R _auto_resume_updated)
  oldDecl,   \* identity the older device last declined (it reports once per identity)
  oldRetr,   \* identity the older device last retried
  \* ---- environment ----
  faults, hot, unsupported, lvl, need, kind, missing, approval, saves, stops, chat,
  \* ---- the app ----
  app,       \* open | killed
  coord,     \* the DeviceRenderCoordinator receipt (on disk) + its running task
  req,       \* S requests[key] (0 = no entry)
  pres,      \* S presentations[key]: what the card shows
  obs,       \* S observations[key] is live
  rec,       \* the reconcile that holds tickets[key]
  oldRec,    \* a reconcile whose ticket was cleared after its fetch returned
  rep,       \* the coordinator's pending report (Stop or a genuine failure)
  decl,      \* S declines[key]: the "won't render" report for an identity
  sched,     \* S retryTasks[key]: an automatic re-read is scheduled
  again,     \* S rereads[key]: a read asked for while the ticket was held: no | read | retry
  hiReq,     \* ghost: newest request this app process installed
  banner,    \* editor "Couldn't refresh this edit" (removed; never set any more)
  upd426,      \* ghost: request a 426 this phone applied was about (0: none)
  ed         \* NativeEditorSession save / preview-poll state

vars == <<sPhase, sId, sReason, sPaused, vStatus, vGen, stale, reapedId, pubIds, pubFrom, futile,
          sDecl, sFloor, sRead, autoRepins, oldDecl, oldRetr,
          faults, hot, unsupported, lvl, need, kind, missing, approval, saves, stops, chat,
          app, coord, req, pres, obs, rec, oldRec, rep, decl, sched, hiReq, banner, ed, again, upd426>>
bld == <<sDecl, sFloor, sRead, autoRepins, oldDecl, oldRetr>>
srv == <<sPhase, sId, sReason, sPaused, vStatus, vGen, stale, reapedId, pubIds, pubFrom, futile, bld>>
env == <<hot, unsupported, lvl, need, kind, missing, approval, saves, stops, chat>>
cli == <<coord, req, pres, obs, rec, oldRec, rep, decl, sched, hiReq, banner, ed, again, upd426>>

\* This build is older than the recipe.
Outdated == lvl < need

Init ==
  /\ sPhase = "awaiting" /\ sId = 1 /\ sReason = "none" /\ sPaused = FALSE
  /\ vStatus = "awaiting" /\ vGen = 1
  /\ stale = FALSE /\ reapedId = 0 /\ pubIds = {} /\ pubFrom = 0 /\ futile = FALSE
  /\ sDecl = 0 /\ sFloor = FALSE /\ sRead = 0 /\ autoRepins = 0 /\ oldDecl = 0 /\ oldRetr = 0
  /\ faults = MaxFaults
  \* At most one phone-side condition per run: too hot (cools later), a recipe needing a
  \* capability this phone's verified set lacks (K:19-20), a recipe newer than this build
  \* (K:23-38, fixed by updating Kria), or an original clip that is not on this iPhone
  \* (genuine, fixed by relinking).
  /\ \E cond \in Conds :
       /\ hot = (cond = "hot") /\ unsupported = (cond = "unsupported")
       /\ lvl = 2 /\ need = (IF cond \in {"outdated", "schema"} THEN 4 ELSE 2)
       \* "newschema": the first recipe renders; a later one ("feature") has a newer schema.
       /\ kind = (IF cond \in {"schema", "newschema"} THEN "schema" ELSE "floor")
       /\ missing = (cond = "missing")
  /\ approval \in Approvals
  /\ chat \in Chat
  /\ saves = 0 /\ stops = 0
  /\ app = "open"
  /\ coord = NoCoord /\ req = 0 /\ pres = NoPres /\ obs = FALSE
  /\ rec = NoRec /\ oldRec = NoRec /\ rep = NoRep /\ decl = NoDecl /\ sched = FALSE
  /\ hiReq = 0 /\ banner = FALSE /\ again = "no" /\ upd426 = 0
  \* E:7534 the editor read capabilities when it opened. Start from a failed read: with a
  \* good one, a later failed read behaves like the no-fault branch (EdRefresh, PollTick).
  /\ ed = EdIdle

Busy == rec # NoRec
Fault == faults > 0
Live == {"awaiting", "syncing"}
Paused == sPaused /\ sPhase \in Live          \* V:678-681 device_paused
Accepted == pubIds # {}                       \* V:199 has_accepted_artifact

\* S:886-910 gate: what the phone does with a recipe the server waits on it to render.
\* Capabilities are always the last enabled set: the editor keeps its last answer or skips
\* (E:900-922), the chat falls back to sessions.lastEnabledCapabilities (W:2282-2289), and
\* S:336 replaces a .disabled with it. So a failed capability read never declines.
\* Order as in S:886-906 + K:16-44: missing capabilities, then storage/heat, then the
\* renderer version.
\* A floor gap never reaches the gate: the GET answers 426 without the recipe.
GateAt(n) == IF unsupported THEN "unsupported" ELSE IF hot THEN "wait"
             ELSE IF lvl < n /\ kind = "schema" THEN "outdated" ELSE "local"
Gate == GateAt(need)
Declines == {"unsupported", "outdated"}
\* S rendersLocally (sent with /retry): this phone's renderer produces the recipe, now or
\* once it cools. An outdated build or a decline says no.
Asserts == Gate \notin Declines /\ ~Outdated

\* D:189-215: reasons a retry cannot change. A change in chat fixes the first two (the card's
\* "This edit needs a change" + "Review the updated edit"); updating Kria fixes "outdated".
ChangeReasons == {"unsupported", "requiresChange"}
Structural == ChangeReasons \cup {"outdated"}
\* D yourTurnPhases: a change or update reason belongs to a render that stopped here or to
\* a finished video this iPhone keeps but cannot sync; on another phase it is stale.
YourTurnPh == {"attention", "localReady"}
\* D needsChange / needsUpdate / nextStep: the creator's turn, with a button of its own. A
\* quiet network wait (a failed status read, S wait) sits on top of it and keeps the button.
NeedsChange(p) == p.ph \in YourTurnPh /\ p.wait \in {"none", "quiet"} /\ p.cause \in ChangeReasons
NeedsUpdate(p) == p.ph \in YourTurnPh /\ p.wait \in {"none", "quiet"} /\ p.cause = "outdated"
YourTurn(p) == NeedsChange(p) \/ NeedsUpdate(p)
\* What the card's title says (D title/detail): "This edit needs a change" / "Update Kria to
\* render this edit" (or the kept video's "Ready on this iPhone" with that way forward),
\* "Ready to render on this iPhone" for renderable_now, never "needs your attention".
Screen(p) == IF NeedsChange(p) THEN "change"
             ELSE IF NeedsUpdate(p) THEN "update"
             ELSE IF p.ph = "attention" /\ p.cause = "renderable" THEN "renderable"
             ELSE p.ph

\* S:331-365 + the defer: a read asked for while the ticket is held runs once it is released.
Again1(a, retry) == IF retry THEN "retry" ELSE IF a = "no" THEN "read" ELSE a
\* S:304-307 sendDecline: one report per identity; a second decline for an identity the
\* server already answered (or is being told) is skipped.
SendDecline(id, reason) == IF decl.id = id /\ decl.st # "none" THEN decl
                           ELSE [id |-> id, reason |-> reason, st |-> "sending"]

\* S:842-865 presentation(receipt): a stopped receipt is the creator's pause.
PresOf(c) == [ph |-> c.ph, rsr |-> FALSE, cause |-> IF c.ph = "cancelled" THEN "paused" ELSE c.cause,
              pub |-> 0, wait |-> "none"]

\* The observer copies every receipt write to the card while the coordinator is busy
\* (S:814-848); it stops once the coordinator is idle.
Show(c) ==
  /\ pres' = IF obs /\ req = c.id THEN PresOf(c) ELSE pres
  /\ obs' = (obs /\ req = c.id /\ c.run)

\* E:870-874 deviceRenderNeedsRetry
SurfaceFail(p) == p.rsr \/ p.ph \in {"attention", "cancelled"} \/ (p.ph = "localReady" /\ p.cause # "none")
\* EC banner by D nextStep: "Review in chat" for a change, "Update Kria" for an outdated
\* build, "Resume rendering" for the creator's own Stop, else "Retry device render".
EdFail(p) == IF NeedsChange(p) THEN "review"
             ELSE IF NeedsUpdate(p) THEN "update"
             ELSE IF p.ph = "cancelled" /\ p.cause = "paused" THEN "paused"
             ELSE IF p.cause = "renderable" THEN "renderNow"   \* "Render on this iPhone"
             ELSE "deviceRetry"
EdBanner == {"deviceRetry", "paused", "review", "update", "renderNow"}

\* src only tells the editor's own reconciles apart ("ed", "edpoll"); the chat poll, the
\* scheduled re-read and the post-decline read are all "chat" (they behave the same).
StartRec(src, retry) == rec' = [src |-> src, retry |-> retry, pc |-> "fetch", resp |-> NoResp]

\* Clearing tickets[key]: a reconcile already past its fetch becomes stale.
ClearTicket == /\ rec' = NoRec
               /\ oldRec' = IF rec.pc = "apply" THEN rec ELSE oldRec

\* ---------------------------------------------------------------- server

\* Pin a new request for a new generation (editor-commit, then V:497 pin_device_request,
\* which starts a fresh record: no pause, no reaped_at).
PinForSave ==
  /\ sId' = sId + 1 /\ sPhase' = "awaiting" /\ sReason' = "none" /\ sPaused' = FALSE
  /\ vStatus' = "awaiting" /\ vGen' = saves + 2 /\ stale' = FALSE
  /\ saves' = saves + 1
  /\ sDecl' = 0 /\ sFloor' = FALSE /\ sRead' = 0   \* a new recipe: nobody has read it yet
  /\ UNCHANGED <<futile, autoRepins, oldDecl, oldRetr>>

\* V:705 retry_device_render (revision + 1) + V:757 apply_retry_variant_reset.
\* `futile` records a re-pin of the same recipe for a phone that will decline it again.
\* The fresh record keeps the newest reader (R _carry_reader_protocol) and forgets the decliner.
ServerRetry ==
  /\ sId' = sId + 1 /\ sPhase' = "awaiting" /\ sReason' = "none" /\ sPaused' = FALSE
  /\ vStatus' = "awaiting" /\ stale' = FALSE
  /\ futile' = (futile \/ (sReason = "unsupported" /\ unsupported) \/ (sReason = "outdated" /\ Outdated))
  /\ sDecl' = 0 /\ sFloor' = FALSE

\* R POST /device-render/retry for the phone's request. No approval check any more; an
\* unsupported decline answers requires_change (R:1090), and an outdated one answers
\* requires_update unless this build is newer than the one that declined
\* (R _caller_is_newer_than_decliner) or says it renders the recipe (renders_locally).
RetryOK == /\ sPhase = "attention" /\ sReason # "unsupported"
           /\ ~(sReason = "outdated" /\ lvl <= sDecl /\ ~Asserts)
           /\ req = sId /\ sId < MaxId
ResumeOK == Paused /\ req = sId                      \* R:1084-1088 resume, same identity

\* R:980-1047 POST /failures. A Stop pauses the record (phase unchanged, R:1015-1020); a
\* failure marks it needs_attention (V:626, only from awaiting/syncing) and leaves an
\* accepted video's variant alone (R:1037).
\* R report_device_failure: a renderer_outdated decline from a build older than the newest
\* reader, or while a device uploads (syncing), is held (409 held_by_newer_build): another
\* device can render it. An accepted one records the declining level and whether that build
\* was below a protocol floor of the recipe (R declined_gap: the server checks its floors
\* for the decliner; a schema gap it cannot see is "renderer").
FailableForReport == sPhase \in {"awaiting", "syncing"}
HeldDecline(reason, from) == reason = "outdated" /\ (sPhase = "syncing" \/ from < sRead)
Report(id, reason, from) ==
  IF id = sId /\ FailableForReport /\ ~HeldDecline(reason, from)
  THEN IF reason = "cancelled"
       THEN /\ sPaused' = TRUE /\ UNCHANGED <<sPhase, sReason, vStatus, sDecl, sFloor>>
       ELSE /\ sPhase' = "attention" /\ sReason' = reason /\ UNCHANGED sPaused
            /\ vStatus' = IF Accepted THEN vStatus ELSE "attention"
            /\ sDecl' = IF reason = "outdated" THEN from ELSE sDecl
            /\ sFloor' = IF reason = "outdated" THEN kind = "floor" /\ from < need ELSE sFloor
  ELSE UNCHANGED <<sPhase, sReason, sPaused, vStatus, sDecl, sFloor>>     \* 409 / idempotent: rejected

\* C:354-375 the delivery task: sent until the server accepts or refuses it.
Deliver ==
  /\ app = "open" /\ rep.live
  /\ Report(rep.id, rep.reason, lvl)
  /\ rep' = NoRep
  /\ UNCHANGED <<sId, vGen, stale, reapedId, pubIds, pubFrom, futile, sRead, autoRepins, oldDecl,
                 oldRetr, faults, env, app, coord, req, pres, obs, rec, oldRec, decl, sched, hiReq,
                 banner, ed, again, upd426>>

DeliveryFails ==                                    \* offline: back off and send again
  /\ app = "open" /\ rep.live /\ Fault
  /\ faults' = faults - 1 /\ rep' = rep
  /\ UNCHANGED <<srv, env, app, coord, req, pres, obs, rec, oldRec, decl, sched, hiReq, banner, ed,
                 again, upd426>>

\* S:623-656 sendDecline: retried until the server answers (accepted, or refused for good),
\* then reconcile to read needs_attention (a read asked for while busy runs after).
DeclDeliver ==
  /\ app = "open" /\ decl.st = "sending"
  /\ Report(decl.id, decl.reason, lvl)
  /\ decl' = [decl EXCEPT !.st = "done"]
  /\ IF ~Busy THEN StartRec("chat", FALSE) /\ UNCHANGED again
     ELSE UNCHANGED rec /\ again' = Again1(again, FALSE)
  /\ UNCHANGED <<sId, vGen, stale, reapedId, pubIds, pubFrom, futile, sRead, autoRepins, oldDecl,
                 oldRetr, faults, env, app, coord, req, pres, obs, oldRec, rep, sched, hiReq, banner,
                 ed, upd426>>

DeclFails ==                                        \* offline: back off and send again
  /\ app = "open" /\ decl.st = "sending" /\ Fault
  /\ faults' = faults - 1 /\ decl' = decl
  /\ UNCHANGED <<srv, env, app, coord, req, pres, obs, rec, oldRec, rep, sched, hiReq, banner, ed,
                 again, upd426>>

\* X: no poll for 24 h. A day only passes unnoticed while the app is not polling.
DayPasses ==
  /\ app = "killed" /\ "reaper" \in Events /\ sPhase \in Live /\ ~stale
  /\ stale' = TRUE
  /\ UNCHANGED <<sPhase, sId, sReason, sPaused, vStatus, vGen, reapedId, pubIds, pubFrom, futile,
                 bld, faults, env, app, cli>>
\* X:99-145: skips a paused record (X:123); keeps an accepted video's variant (X:140).
ReapablePhase == sPhase \in Live /\ ~Paused
Reaper ==
  /\ stale /\ ReapablePhase /\ reapedId # sId
  /\ sPhase' = "attention" /\ sReason' = "timedOut" /\ reapedId' = sId
  /\ vStatus' = IF Accepted THEN vStatus ELSE "attention"
  /\ UNCHANGED <<sId, sPaused, vGen, stale, pubIds, pubFrom, futile, bld, faults, env, app, cli>>

OtherDeviceSave ==                                   \* another device saves
  /\ saves < MaxSaves /\ ("other" \in Events \/ "feature" \in Events) /\ sId < MaxId
  /\ PinForSave
  \* "feature": that device's newer build adds a protocol-gated feature this build lacks.
  /\ need' \in (IF "feature" \in Events THEN {need, 4} ELSE {need})
  /\ UNCHANGED <<reapedId, pubIds, pubFrom, faults, hot, unsupported, lvl, kind, missing, approval,
                 stops, chat, app, cli>>

\* An older build on another device reads the record: the GET answers it 426 (it decodes
\* no recipe of this iPhone's level), so it declines as renderer_outdated, once per identity.
OldDecline ==
  /\ "old" \in Events /\ sPhase \in Live /\ oldDecl # sId
  /\ oldDecl' = sId
  /\ Report(sId, "outdated", OLD)
  /\ UNCHANGED <<sId, vGen, stale, reapedId, pubIds, pubFrom, futile, sRead, autoRepins, oldRetr,
                 faults, env, app, cli>>

\* That older build taps Try again on a needs_attention card (its card may be older than this
\* fix). The server's checks are what stop it: requires_change for an unsupported recipe,
\* requires_update for an outdated one (an old build never says renders_locally).
OldRetry ==
  /\ "old" \in Events /\ sPhase = "attention" /\ oldRetr # sId /\ sId < MaxId
  /\ oldRetr' = sId
  /\ IF sReason # "unsupported" /\ ~(sReason = "outdated" /\ OLD <= sDecl)
     THEN ServerRetry /\ UNCHANGED <<vGen, sRead>>
     ELSE UNCHANGED <<sId, sPhase, sReason, sPaused, vStatus, stale, futile, sDecl, sFloor, vGen, sRead>>
  /\ UNCHANGED <<reapedId, pubIds, pubFrom, autoRepins, oldDecl, faults, env, app, cli>>

\* An operator lowers the protocol floor of the recipe: the build that declined it can now
\* render it (S apply shows renderable_now; its /retry says renders_locally).
Rollback ==
  /\ "rollback" \in Events /\ kind = "floor" /\ Outdated
  /\ need' = lvl
  /\ UNCHANGED <<srv, faults, hot, unsupported, lvl, kind, missing, approval, saves, stops, chat, app,
                 cli>>

\* The phone cools down; thermalStateDidChange runs resumeWaiting(thermalOnly) (S:703-713).
Cool ==
  /\ hot /\ hot' = FALSE
  /\ sched' = IF app = "open" /\ pres.wait = "cooling" THEN TRUE ELSE sched
  /\ UNCHANGED <<srv, faults, unsupported, lvl, need, kind, missing, approval, saves, stops, chat, app,
                 coord, req, pres, obs, rec, oldRec, rep, decl, hiReq, banner, ed, again, upd426>>

\* The server stops listing a feature as verified for this phone (a rollback of a verified
\* capability) while its coordinator works on the request: mid-render (no file yet) or after
\* its export finished. S apply re-checks capabilities on every reconcile.
LoseCaps ==
  /\ "caps" \in Events /\ ~unsupported /\ (coord.file \/ coord.run)
  /\ unsupported' = TRUE
  /\ UNCHANGED <<srv, faults, hot, lvl, need, kind, missing, approval, saves, stops, chat, app, cli>>

\* ---------------------------------------------------------------- reconcile (S:331-497)

\* Chat poll (W:2232-2280) reconciles while the workspace is open. It always may (a thread
\* change or a rendering project status asks for it), but it only must while the render is
\* not settled (W:2240-2242: `changed || !deviceSettled || status == .rendering`, S isSettled
\* = synced; superseded is not settled any more, the next read finds out what replaced it).
\* `changed` and the project status are server projections the phone cannot promise, so
\* the fairness below only covers the unsettled case.
\* A chat poll that finds the ticket held also asks for a re-read (S rereads); that read is
\* the same as a later ChatPoll, which the model always allows, so it is not modelled twice.
Settled == pres.ph = "synced"
ChatPoll ==
  /\ app = "open" /\ chat /\ ~Busy
  /\ StartRec("chat", FALSE)
  /\ UNCHANGED <<srv, faults, env, app, coord, req, pres, obs, oldRec, rep, decl, sched, hiReq,
                 banner, ed, again, upd426>>
ChatPollMust == ChatPoll /\ ~Settled

\* S defer: the read asked for while another held the ticket runs once it is released.
Reread ==
  /\ app = "open" /\ again # "no" /\ ~Busy
  /\ StartRec("chat", again = "retry") /\ again' = "no"
  /\ UNCHANGED <<srv, faults, env, app, coord, req, pres, obs, oldRec, rep, decl, sched, hiReq,
                 banner, ed, upd426>>

\* S:590-603 the scheduled re-read (also cut short by foreground / reconnect). It returns
\* at once when another reconcile holds the ticket.
RetryWait ==
  /\ app = "open" /\ sched
  /\ sched' = FALSE
  /\ IF ~Busy THEN StartRec("chat", FALSE) /\ UNCHANGED again
     ELSE UNCHANGED rec /\ again' = Again1(again, FALSE)
  /\ UNCHANGED <<srv, faults, env, app, coord, req, pres, obs, oldRec, rep, decl, hiReq, banner, ed,
                 upd426>>

\* S:344 GET /device-render. A success touches last_polled_at. The protocol gates run first
\* (R _client_update_detail): a build below a floor of the recipe gets a 426 naming the
\* request and nothing is re-pinned. Otherwise a record the reaper failed is re-pinned for a
\* phone build on the spot (R _auto_resume_reaped), and so is an outdated decline of a
\* protocol floor once a build newer than the decliner reads it and passes the floors
\* (R _auto_resume_updated). A schema gap is never re-pinned by the server (it cannot know
\* whether the newer build renders it): the phone's own gate decides (renderable_now). A build that passes the
\* gates is remembered as a reader (R _note_reader_protocol).
Floors == kind = "schema" \/ lvl >= need             \* the server sees no schema gap
RecFetch ==
  /\ rec.pc = "fetch"
  /\ \/ /\ Fault
        /\ rec' = [rec EXCEPT !.pc = "apply", !.resp = NoResp]
        /\ faults' = faults - 1
        /\ UNCHANGED <<sPhase, sId, sReason, sPaused, vStatus, stale, futile, sDecl, sFloor, sRead, autoRepins>>
     \/ LET reaped == sPhase = "attention" /\ sReason = "timedOut" /\ reapedId = sId
            updated == sPhase = "attention" /\ sReason = "outdated" /\ sDecl > 0 /\ lvl > sDecl /\ sFloor
            auto == Floors /\ (reaped \/ updated) /\ sId < MaxId
            id == IF auto THEN sId + 1 ELSE sId
            ph == IF auto THEN "awaiting" ELSE sPhase
        IN /\ IF auto THEN ServerRetry ELSE UNCHANGED <<sPhase, sId, sReason, sPaused, vStatus, futile, sDecl, sFloor>>
           /\ sRead' = IF Floors THEN Max(sRead, lvl) ELSE sRead
           /\ autoRepins' = IF auto /\ updated THEN autoRepins + 1 ELSE autoRepins
           /\ rec' = [rec EXCEPT !.pc = "apply",
                        !.resp = [ok |-> TRUE, upd |-> ~Floors, id |-> id, ph |-> ph,
                                  reason |-> IF auto THEN "none" ELSE sReason,
                                  pub |-> IF ph = "published" THEN Pub(id) ELSE 0,
                                  paused |-> ~auto /\ Paused, need |-> need]]
           /\ stale' = FALSE
           /\ UNCHANGED faults
  /\ UNCHANGED <<vGen, reapedId, pubIds, pubFrom, oldDecl, oldRetr, env, app, coord, req, pres, obs,
                 oldRec, rep, decl, sched, hiReq, banner, ed, again, upd426>>

\* C:392-411 start(): same request running/finished -> no-op; same request localReady ->
\* retrySync; otherwise a FRESH receipt (which drops a persisted pending report).
Reuses(c, id) == c.id = id /\ (c.run \/ c.ph \in {"synced", "superseded", "localReady"})
StartC(c, id) ==
  IF c.id = id /\ (c.run \/ c.ph \in {"synced", "superseded"}) THEN c
  ELSE IF c.id = id /\ c.ph = "localReady"
  THEN IF c.file THEN [c EXCEPT !.run = TRUE, !.pc = "prep"] ELSE c
  ELSE [id |-> id, ph |-> "preparing", file |-> FALSE, run |-> TRUE, pc |-> "prep", cause |-> "none"]
\* C:417-422 recover(): resumes a parked receipt (and re-sends its pending report).
RecoverC(c) ==
  IF c.id # 0 /\ ~c.run /\ c.ph \notin {"cancelled", "superseded", "synced", "attention"}
  THEN [c EXCEPT !.run = TRUE, !.pc = "prep"] ELSE c

\* S readFailed .transient -> wait(): keep what the card shows, wait quietly and re-read
\* (S scheduleRetry). A synced video keeps its card as it is, but is re-read too: the
\* failed read may have been the one to pick up a newer request, and the chat poll no
\* longer reads a settled render.
ReadFailedPres ==
  IF pres.ph = "synced" THEN pres
  ELSE [(IF pres = NoPres THEN [NoPres EXCEPT !.ph = "preparing"] ELSE pres) EXCEPT !.wait = "quiet"]

\* S:440-445 resume: `try? await retryFailure` lifts the pause under the same identity; a
\* failed call is swallowed (the reservation drops the pause later anyway).
ResumeServer(r, id) ==
  IF r.resp.paused
  THEN \/ /\ sPaused' = IF id = sId /\ sPhase \in Live THEN FALSE ELSE sPaused
          /\ UNCHANGED faults
       \/ /\ Fault /\ faults' = faults - 1 /\ UNCHANGED sPaused
  ELSE UNCHANGED <<sPaused, faults>>

\* The rest of reconcile after the fetch, on the main actor. `own` = tickets[key] == ticket.
\* S holdsFinishedVideo
HoldsFinished == pres.ph = "synced" \/ (pres.ph = "localReady" /\ coord.file)
Apply(r, own) ==
  IF ~own                                            \* S:347, S:351 ticket guard
  THEN UNCHANGED <<coord, req, pres, obs, hiReq, rep, decl, sched, sPaused, faults, upd426>>
  ELSE IF ~r.resp.ok
  THEN /\ pres' = ReadFailedPres
       /\ sched' = TRUE
       /\ UNCHANGED <<coord, req, obs, hiReq, rep, decl, sPaused, faults, upd426>>
  ELSE IF r.resp.upd
  THEN \* S readFailed .updateRequired(target): the 426 names its request. A published one
       \* stays published; a finished video of the same request stays; a newer request is
       \* shown as "Update Kria" and reported, whatever this phone holds for an older one.
       LET id == r.resp.id
       IN IF r.resp.ph = "published"
          THEN /\ pres' = IF pres.ph = "synced" THEN pres ELSE [NoPres EXCEPT !.ph = "synced"]
               /\ obs' = FALSE /\ sched' = FALSE
               /\ UNCHANGED <<coord, req, hiReq, rep, decl, sPaused, faults, upd426>>
          ELSE IF HoldsFinished /\ id = req
          THEN /\ sched' = FALSE /\ upd426' = id
               /\ UNCHANGED <<coord, req, pres, obs, hiReq, rep, decl, sPaused, faults>>
          ELSE /\ obs' = FALSE /\ sched' = FALSE /\ upd426' = id
               /\ pres' = [ph |-> "attention", rsr |-> r.resp.ph = "attention", cause |-> "outdated",
                           pub |-> 0, wait |-> "none"]
               /\ decl' = IF r.resp.ph \in Live THEN SendDecline(id, "outdated") ELSE decl
               /\ UNCHANGED <<coord, req, hiReq, rep, sPaused, faults>>
  ELSE LET id == r.resp.id
           gate == GateAt(r.resp.need)                   \* the answered request's recipe
           new == req # id
           \* S:374-380: a new request cancels the old coordinator (housekeeping, no report)
           \* and loads the receipt on disk again; the directory is per key.
           c1 == IF new /\ req # 0 /\ coord.id # 0
                 THEN [coord EXCEPT !.ph = "cancelled", !.run = FALSE, !.pc = "idle"]
                 ELSE IF new THEN [coord EXCEPT !.run = FALSE, !.pc = "idle"] ELSE coord
       IN /\ req' = id
          /\ upd426' = 0                                  \* ghost: an answer with a recipe
          /\ hiReq' = IF id > hiReq THEN id ELSE hiReq
          /\ IF r.resp.ph = "published"                   \* S:385-390
             THEN /\ coord' = c1 /\ obs' = FALSE /\ sched' = FALSE
                  /\ pres' = [NoPres EXCEPT !.ph = "synced", !.pub = r.resp.pub]
                  /\ UNCHANGED <<rep, decl, sPaused, faults>>
             ELSE IF r.resp.ph = "attention"              \* S:392-411
             THEN \* S apply: a decline this build no longer holds (it renders the recipe now)
                  \* reads renderable_now; its Try again says renders_locally.
                  LET reason == IF r.resp.reason = "outdated" /\ gate = "local"
                                THEN "renderable" ELSE r.resp.reason
                  IN /\ coord' = c1 /\ obs' = FALSE /\ sched' = FALSE
                     /\ pres' = [ph |-> IF c1.file THEN "localReady" ELSE "attention", rsr |-> TRUE,
                                 cause |-> reason, pub |-> 0, wait |-> "none"]
                     /\ UNCHANGED <<rep, decl, sPaused, faults>>
             ELSE IF r.resp.paused /\ (c1.id # id \/ c1.ph = "cancelled") /\ ~r.retry
             THEN \* S:430-436 the creator's Stop holds until they resume it
                  /\ coord' = c1 /\ obs' = FALSE /\ sched' = FALSE
                  /\ pres' = [NoPres EXCEPT !.ph = "cancelled", !.cause = "paused"]
                  /\ UNCHANGED <<rep, decl, sPaused, faults>>
             ELSE \* S:440-445 resume (or a Stop report that landed after a resume here).
                  /\ ResumeServer(r, id)
                  \* S resumesSync: this request's video exists here (also already synced: a
                  \* stale "awaiting" answer must not decline a video that is published).
                  /\ LET resumesSync == c1.id = id /\ c1.file /\ c1.ph \in {"localReady", "syncing", "synced"}
                         inFlight == c1.id = id /\ c1.ph \in {"preparing", "rendering"} /\ c1.run
                     IN IF gate = "wait" /\ ~resumesSync /\ ~inFlight
                        THEN \* S:463-467 waiting for the phone to cool; re-checked on a timer
                             /\ coord' = c1 /\ obs' = FALSE /\ sched' = TRUE
                             /\ pres' = [NoPres EXCEPT !.ph = "preparing", !.wait = "cooling"]
                             /\ UNCHANGED <<rep, decl>>
                        ELSE IF gate \in Declines /\ ~resumesSync /\ ~inFlight
                        THEN \* S decline: the phone will never render it. The card shows the
                             \* creator's turn (a change or an update), and the server is told,
                             \* also when a finished export of an earlier attempt is kept here.
                             \* (`.decline where resumesSync || inFlight` lets an export of this
                             \* request that finished or is running upload: the server checks
                             \* the bytes, never the phone's capabilities.)
                             LET keeps == c1.id = id /\ c1.file
                             IN /\ coord' = c1 /\ obs' = FALSE /\ sched' = FALSE
                                /\ pres' = [ph |-> IF keeps THEN "localReady" ELSE "attention",
                                            rsr |-> FALSE, cause |-> gate, pub |-> 0,
                                            wait |-> "none"]
                                /\ decl' = SendDecline(id, gate)
                                /\ UNCHANGED rep
                        ELSE \* S:483-496 start or recover, then observe
                             LET starts == c1.id # id \/ r.retry
                                 c2 == IF starts THEN StartC(c1, id) ELSE RecoverC(c1)
                                 fresh == starts /\ ~Reuses(c1, id)
                             IN /\ coord' = c2 /\ sched' = FALSE
                                /\ rep' = IF rep = NoRep THEN rep
                                          ELSE IF fresh
                                          THEN (IF rep.live THEN [rep EXCEPT !.disk = FALSE] ELSE NoRep)
                                          ELSE IF ~starts THEN [rep EXCEPT !.live = TRUE]   \* C:418
                                          ELSE rep
                                /\ pres' = IF c2.id = id THEN PresOf(c2) ELSE pres
                                /\ obs' = (c2.id = id /\ c2.run)
                                /\ UNCHANGED decl

RecApply ==
  /\ rec.pc = "apply"
  /\ Apply(rec, TRUE)
  /\ rec' = NoRec
  /\ UNCHANGED <<sPhase, sId, sReason, vStatus, vGen, stale, reapedId, pubIds, pubFrom, futile, bld,
                 env, app, oldRec, banner, ed, again>>

OldApply ==
  /\ oldRec # NoRec
  /\ Apply(oldRec, FALSE)
  /\ oldRec' = NoRec
  /\ UNCHANGED <<sPhase, sId, sReason, vStatus, vGen, stale, reapedId, pubIds, pubFrom, futile, bld,
                 env, app, rec, banner, ed, again>>

\* ---------------------------------------------------------------- the coordinator (C:546-649)

Run(pc) == app = "open" /\ coord.run /\ coord.pc = pc

\* C:604-614 a genuine failure: no output file -> needsAttention + a persisted report
\* that is delivered until the server takes it; with a file -> localReady carrying the error.
FailStep(cause, reason) ==
  LET c == IF coord.file
           THEN [coord EXCEPT !.ph = "localReady", !.cause = "syncFail", !.run = FALSE, !.pc = "idle"]
           ELSE [coord EXCEPT !.ph = "attention", !.cause = cause, !.run = FALSE, !.pc = "idle"]
  IN /\ coord' = c /\ Show(c)
     /\ rep' = IF coord.file THEN rep ELSE [id |-> coord.id, reason |-> reason, live |-> TRUE, disk |-> TRUE]

\* C:580-600 a transient failure (offline, 5xx, 429): park where it can continue
\* (preparing before an output exists, localReady after) with no error, wait, and retry
\* the same step. Never reported.
Park == [coord EXCEPT !.ph = IF coord.file THEN "localReady" ELSE "preparing", !.cause = "none"]
ParkStep == coord' = Park /\ Show(Park) /\ UNCHANGED rep

Step(c) == coord' = c /\ Show(c) /\ UNCHANGED rep

ServerCurrent == sId = coord.id /\ sPhase \in {"awaiting", "syncing", "published"}   \* P:313-319

\* C:618-634: isCurrent, then resolve the clips (asset grants + originals).
PerfPrep ==
  /\ Run("prep")
  /\ \/ /\ Fault /\ faults' = faults - 1
        /\ ParkStep
        /\ UNCHANGED stale
     \/ /\ UNCHANGED faults /\ stale' = FALSE         \* the GET touches last_polled_at
        /\ IF ~ServerCurrent
           THEN Step([coord EXCEPT !.ph = "superseded", !.run = FALSE, !.pc = "idle"])
           ELSE IF coord.file THEN Step([coord EXCEPT !.pc = "sync"])              \* intact MP4
           ELSE IF missing THEN FailStep("origMissing", "origMissing")             \* original_missing
           ELSE Step([coord EXCEPT !.ph = "rendering", !.pc = "export"])
  /\ UNCHANGED <<sPhase, sId, sReason, sPaused, vStatus, vGen, reapedId, pubIds, pubFrom, futile, bld,
                 env, app, req, rec, oldRec, decl, sched, hiReq, banner, ed, again, upd426>>

PerfExport ==
  /\ Run("export")
  /\ Step([coord EXCEPT !.ph = "localReady", !.file = TRUE, !.pc = "sync"])
  /\ UNCHANGED <<srv, faults, env, app, req, rec, oldRec, decl, sched, hiReq, banner, ed, again, upd426>>

\* C:458-500: the background window expired (or AVFoundation reports an interruption):
\* stop the encode and keep the receipt resumable; the next reconcile recovers it.
Background ==
  /\ Run("export") /\ "background" \in Events /\ Fault
  /\ faults' = faults - 1
  /\ Step([Park EXCEPT !.run = FALSE, !.pc = "idle"])
  /\ UNCHANGED <<srv, env, app, req, rec, oldRec, decl, sched, hiReq, banner, ed, again, upd426>>

\* C:643-645 isCurrent, then P:320-350 reserve the upload. A reservation means the phone
\* resumed, so it drops a user pause (R:922).
PerfSync ==
  /\ Run("sync")
  /\ \/ /\ Fault /\ faults' = faults - 1 /\ ParkStep
        /\ UNCHANGED <<sPhase, sPaused, stale>>
     \/ /\ UNCHANGED faults /\ stale' = FALSE
        /\ IF ~ServerCurrent
           THEN /\ UNCHANGED <<sPhase, sPaused>>
                /\ Step([coord EXCEPT !.ph = "superseded", !.run = FALSE, !.pc = "idle"])
           ELSE IF sPhase \in Live
           THEN /\ sPhase' = "syncing" /\ sPaused' = FALSE
                /\ Step([coord EXCEPT !.ph = "syncing", !.pc = "complete"])
           ELSE /\ UNCHANGED <<sPhase, sPaused>> /\ FailStep("blip", "transient")
  /\ UNCHANGED <<sId, sReason, vStatus, vGen, reapedId, pubIds, pubFrom, futile, bld, env, app, req,
                 rec, oldRec, decl, sched, hiReq, banner, ed, again, upd426>>

LateComplete == [coord EXCEPT !.ph = "superseded", !.run = FALSE, !.pc = "idle"]
CompleteOK == sId = coord.id /\ sPhase = "syncing"   \* complete via _record identity check

PerfComplete ==
  /\ Run("complete")
  /\ \/ /\ Fault /\ faults' = faults - 1 /\ ParkStep
        /\ UNCHANGED <<sPhase, vStatus, vGen, pubIds, pubFrom>>
     \/ /\ UNCHANGED faults
        /\ IF CompleteOK
           THEN /\ sPhase' = "published" /\ vStatus' = "ready" /\ vGen' = Pub(sId)
                /\ pubIds' = pubIds \cup {coord.id} /\ pubFrom' = coord.id
                /\ Step([coord EXCEPT !.ph = "synced", !.run = FALSE, !.pc = "idle"])
           ELSE /\ UNCHANGED <<sPhase, vStatus, vGen, pubIds, pubFrom>>
                \* 409 -> isCurrent -> superseded (or rethrow while current)
                /\ IF ServerCurrent THEN FailStep("blip", "transient")
                   ELSE Step(LateComplete)
  /\ UNCHANGED <<sId, sReason, sPaused, stale, reapedId, futile, bld, env, app, req, rec, oldRec, decl,
                 sched, hiReq, banner, ed, again, upd426>>

\* ---------------------------------------------------------------- app lifecycle

\* The process ends; memory is gone, the receipt (and a persisted report) stay on disk.
ProcessEnds ==
  /\ app' = "killed"
  /\ coord' = [coord EXCEPT !.run = FALSE, !.pc = "idle"]
  /\ rep' = IF rep.disk THEN [rep EXCEPT !.live = FALSE] ELSE NoRep
  /\ req' = 0 /\ pres' = NoPres /\ obs' = FALSE /\ rec' = NoRec /\ oldRec' = NoRec
  /\ decl' = NoDecl /\ sched' = FALSE /\ hiReq' = 0 /\ banner' = FALSE
  /\ ed' = EdIdle /\ again' = "no" /\ upd426' = 0

Kill ==                                              \* iOS kills the app
  /\ app = "open" /\ "kill" \in Events /\ Fault
  /\ faults' = faults - 1
  /\ ProcessEnds
  /\ UNCHANGED <<srv, env>>

\* You open Kria again: it starts on the chat workspace (KriaApp's root) with the last
\* project, whose poll then runs, also after an update from the gallery editor.
Reopen ==
  /\ app = "killed"
  /\ app' = "open" /\ chat' = TRUE
  /\ UNCHANGED <<srv, faults, hot, unsupported, lvl, need, kind, missing, approval, saves, stops, cli>>

\* ---------------------------------------------------------------- you, in the chat card

\* D:267-277 + D:329-330: which button the card shows. Structural reasons hide it with or
\* without requiresServerRetry; heat waits hide it (quiet network waits keep it).
ShowsRetry ==
  /\ pres.ph \in {"attention", "cancelled", "localReady"}
  /\ pres.wait \in {"none", "quiet"}
  /\ ~(missing /\ pres.ph = "attention")              \* KRI-211 originalsMissing hides it
  \* D showsRetryButton: a kept video with a structural reason gets no "Retry sync" either.
  /\ ~(pres.ph \in YourTurnPh /\ pres.cause \in Structural)

\* "Stop rendering" (D:345 -> S:781-788 -> C:446-451): a resumable pause. The creator taps
\* it at most once per run (like Saves, a bounded user event).
Stop ==
  /\ app = "open" /\ "stop" \in Events /\ pres.ph \in {"preparing", "rendering"}
  /\ pres.wait \in {"none", "quiet"}
  /\ stops = 0 /\ stops' = 1
  /\ ClearTicket /\ sched' = FALSE /\ again' = "no"
  /\ IF req # 0 /\ coord.id # 0
     THEN LET c == [coord EXCEPT !.ph = "cancelled", !.run = FALSE, !.pc = "idle"]
          IN /\ coord' = c /\ pres' = PresOf(c) /\ obs' = FALSE
             /\ rep' = IF coord.ph \notin {"cancelled", "synced", "superseded", "attention"}
                       THEN [id |-> coord.id, reason |-> "cancelled", live |-> TRUE, disk |-> TRUE]
                       ELSE rep
     ELSE UNCHANGED <<coord, pres, obs, rep>>         \* no coordinator: the button does nothing
  /\ UNCHANGED <<srv, faults, hot, unsupported, lvl, need, kind, missing, approval, saves, chat, app, req,
                 decl, hiReq, banner, ed, upd426>>

\* retryNeedsAttention (S:663-701): POST /device-render/retry, then drop the local
\* coordinator and receipt identity and clear the ticket. `after` is what the caller
\* does next.
RetryNeedsAttention(after) ==
  /\ req # 0
  /\ UNCHANGED <<saves, stops>>
  /\ \/ /\ Fault                                     \* offline: S:682-697 message only
        /\ faults' = faults - 1
        /\ UNCHANGED <<srv, coord, req, pres, obs, rec, oldRec, sched, ed>>
     \/ /\ UNCHANGED faults
        /\ IF RetryOK \/ ResumeOK
           THEN /\ IF RetryOK THEN ServerRetry
                   ELSE /\ sPaused' = FALSE /\ UNCHANGED <<sId, sPhase, sReason, vStatus, stale, futile, sDecl, sFloor>>
                /\ UNCHANGED <<vGen, reapedId, pubIds, pubFrom, sRead, autoRepins, oldDecl, oldRetr>>
                /\ coord' = IF coord.id # 0
                            THEN [coord EXCEPT !.ph = "cancelled", !.run = FALSE, !.pc = "idle"]
                            ELSE coord
                /\ req' = 0 /\ obs' = FALSE /\ sched' = FALSE
                /\ pres' = [NoPres EXCEPT !.ph = "preparing"]
                /\ oldRec' = IF rec.pc = "apply" THEN rec ELSE oldRec
                /\ after
           \* S:684-705: requires_change -> the change screen; requires_update -> "Update
           \* Kria"; not_awaiting_retry / superseded (R _record) / at the bound: message only.
           ELSE /\ pres' = IF sPhase = "attention" /\ req = sId /\ sReason = "unsupported"
                           THEN [pres EXCEPT !.cause = "requiresChange"]
                           ELSE IF sPhase = "attention" /\ req = sId /\ sReason = "outdated"
                                   /\ lvl <= sDecl /\ ~Asserts
                           THEN [pres EXCEPT !.cause = "outdated"]
                           ELSE pres
                /\ UNCHANGED <<srv, coord, req, obs, rec, oldRec, sched, ed>>

ChatAfterRetry ==                                    \* D:33-39 then retry()
  /\ StartRec("chat", TRUE)
  /\ UNCHANGED ed

EdAfterRetry ==                                      \* E:7847-7860 then refreshDeviceRender(retry: true)
  /\ rec' = NoRec
  /\ ed' = [ed EXCEPT !.pc = "refresh", !.rflag = TRUE]

\* The card's retry closure (W:1829-1835): refreshCapabilities, then reconcile(retry: true)
\* with authoritative or last enabled capabilities; it returns at once when busy.
ChatRetryLocal ==
  /\ IF ~Busy THEN StartRec("chat", TRUE) /\ UNCHANGED again
     ELSE UNCHANGED rec /\ again' = Again1(again, TRUE)
  /\ UNCHANGED <<srv, faults, saves, stops, coord, req, pres, obs, oldRec, sched, ed>>

\* The chat card's Try again / Resume / Retry sync (D:22-39). retryNeedsApproval is never
\* true any more (the server sends retry_requires_approval = false, R:535), so no button
\* routes to chat approval.
ChatTryAgainBody == IF pres.rsr THEN RetryNeedsAttention(ChatAfterRetry) /\ UNCHANGED again
                    ELSE ChatRetryLocal
ChatTryAgain ==
  /\ app = "open" /\ ShowsRetry
  /\ ChatTryAgainBody
  /\ UNCHANGED <<hot, unsupported, lvl, need, kind, missing, approval, chat, app, rep, decl, hiReq, banner,
                 upd426>>

\* D:47-111 Find original files: once every original is back, resume() finishes the video
\* without another tap (retryNeedsAttention when the server needs it, then retry()).
Relink ==
  /\ app = "open" /\ missing /\ pres.ph = "attention" /\ pres.wait = "none"
  /\ pres.cause \notin Structural
  /\ missing' = FALSE
  /\ ChatTryAgainBody
  /\ UNCHANGED <<hot, unsupported, lvl, need, kind, approval, chat, app, rep, decl, hiReq, banner, upd426>>

\* "Review the updated edit" / "Review in chat" (card D, editor banner EC) in the chat
\* workspace: the creator asks for a change in the conversation. The server pins Kria's new
\* recipe as a new request for a new generation. Kria re-plans with the features verified for
\* phones, but a chat edit may also keep the feature this iPhone lacks (or the newer one it
\* can't decode): then the phone declines again and the creator still has the same way out.
\* The rule is about having a way out, not about one change fixing it.
ChatChange ==
  /\ app = "open" /\ chat /\ (YourTurn(pres) \/ ed.save \in {"review", "update"}) /\ sId < MaxId
  /\ sId' = sId + 1 /\ sPhase' = "awaiting" /\ sReason' = "none" /\ sPaused' = FALSE
  /\ vStatus' = "awaiting" /\ vGen' = 50 + sId + 1 /\ stale' = FALSE
  /\ sDecl' = 0 /\ sFloor' = FALSE /\ sRead' = 0
  /\ unsupported' \in {FALSE, unsupported}
  /\ need' \in {need, lvl}
  /\ UNCHANGED <<reapedId, pubIds, pubFrom, futile, autoRepins, oldDecl, oldRetr, faults, hot, lvl, kind,
                 missing, approval, saves, stops, chat, app, cli>>

\* "Review in chat" from the gallery editor (no conversation under it, EditorViews:265): it
\* opens the edit's own creation thread (open-in-editor names it; AppModel
\* openCreationThread), closing the gallery and the editor. The chat workspace and its poll
\* are there from then on.
OpenChat ==
  /\ app = "open" /\ ~chat
  /\ YourTurn(pres) \/ ed.save \in {"review", "update"}
  /\ chat' = TRUE /\ ed' = EdIdle
  /\ UNCHANGED <<srv, faults, hot, unsupported, lvl, need, kind, missing, approval, saves, stops, app,
                 coord, req, pres, obs, rec, oldRec, rep, decl, sched, hiReq, banner, again, upd426>>

\* "Update Kria" (card D, editor banner EC -> KriaUpdateLink: TestFlight, else its App Store
\* page): the update ends the process. The new build is newer than the one that declined,
\* but for a floor gap it may still be too old (level 3 < 4): "newer but insufficient", at
\* most once (the next update is the current build). A schema gap the server cannot see is
\* assumed fixed by the update (findings.md, open risk). Bounded like the pinning events.
UpdateApp ==
  /\ app = "open" /\ Outdated /\ sId < MaxId
  /\ NeedsUpdate(pres) \/ ed.save = "update"
  /\ lvl' \in (IF kind = "floor" /\ lvl = 2 THEN {3, 4} ELSE {4})
  /\ ProcessEnds
  /\ UNCHANGED <<srv, faults, hot, unsupported, need, kind, missing, approval, saves, stops, chat>>

\* ---------------------------------------------------------------- the editor

\* E:7605-7612 startPreviewRefresh
StartPreview(e) ==
  IF SurfaceFail(pres)
  THEN [e EXCEPT !.save = EdFail(pres), !.cause = pres.cause, !.ppc = "off", !.pc = "idle"]
  ELSE [e EXCEPT !.save = "previewPending", !.ppc = "tick", !.ticks = 0, !.seen = 0, !.pc = "idle",
                 !.cause = "none"]

\* You tap Save: the server pins a new request for a new generation; a running preview
\* poll is cancelled, and a reconcile inside its GET now returns quietly (URLError
\* .cancelled is cancellation, S:122-126). E:6115 remembers the generation it saved from.
EdSave ==
  /\ app = "open" /\ "save" \in Events /\ ed.pc = "idle" /\ saves < MaxSaves /\ sId < MaxId
  /\ PinForSave
  /\ ed' = [ed EXCEPT !.pc = "refresh", !.pend = saves + 2, !.save = "previewPending",
                      !.ppc = "off", !.ticks = 0, !.seen = 0, !.rflag = FALSE, !.cause = "none",
                      !.from = vGen]
  /\ rec' = IF rec.src = "edpoll" /\ rec.pc = "fetch" THEN NoRec ELSE rec
  /\ UNCHANGED <<reapedId, pubIds, pubFrom, faults, hot, unsupported, lvl, need, kind, missing, approval,
                 stops, chat, app, coord, req, pres, obs, oldRec, rep, decl, sched, hiReq, banner, again,
                 upd426>>

\* E:900-922 refreshDeviceRender: a failed capability read keeps the last real answer;
\* with none, it skips the reconcile and leaves it to the preview poll (deviceReconcilePending).
EdRefreshWith(e) ==
  IF ~Busy THEN StartRec("ed", e.rflag) /\ ed' = [e EXCEPT !.pc = "waitRec"] /\ UNCHANGED again
  ELSE UNCHANGED rec /\ ed' = [e EXCEPT !.pc = "after"] /\ again' = Again1(again, e.rflag)
EdRefresh ==
  /\ app = "open" /\ ed.pc = "refresh"
  /\ \/ /\ UNCHANGED faults /\ EdRefreshWith([ed EXCEPT !.caps = TRUE, !.recPend = FALSE])
     \/ /\ Fault /\ faults' = faults - 1
        /\ IF ed.caps THEN EdRefreshWith([ed EXCEPT !.recPend = FALSE])
           ELSE UNCHANGED <<rec, again>> /\ ed' = [ed EXCEPT !.recPend = TRUE, !.pc = "after"]
  /\ UNCHANGED <<srv, env, app, coord, req, pres, obs, oldRec, rep, decl, sched, hiReq, banner, upd426>>

\* Remember the identity reconcile accepted, start the poll.
EdAfter ==
  /\ app = "open"
  /\ \/ ed.pc = "after"
     \/ ed.pc = "waitRec" /\ rec.src # "ed"
  /\ ed' = StartPreview([ed EXCEPT !.devId = req])
  /\ UNCHANGED <<srv, faults, env, app, coord, req, pres, obs, rec, oldRec, rep, decl, sched,
                 hiReq, banner, again, upd426>>

GiveUp(e) == IF e.ticks + 1 >= MaxTicks
             THEN [e EXCEPT !.save = "previewFailed", !.ppc = "off", !.ticks = 0, !.seen = 0]
             ELSE [e EXCEPT !.ppc = "tick", !.ticks = e.ticks + 1]

\* E:7710-7758 applyPreviewVariant, including the device identity fence, then E:7666-7682:
\* a ready variant of a newer generation (not the one this save started from), read twice,
\* is followed instead of waited out.
Fence(e) == vStatus = "ready" /\ pres.pub = vGen /\ e.devId # 0 /\ req = e.devId
Newer(e) == vStatus = "ready" /\ vGen # e.pend /\ vGen # e.from
Saved(e) == [e EXCEPT !.save = "saved", !.pend = 0, !.ppc = "off", !.ticks = 0, !.devId = 0, !.seen = 0]
ApplyVariant(e) ==
  IF SurfaceFail(pres)
  THEN [e EXCEPT !.save = EdFail(pres), !.cause = pres.cause, !.ppc = "off", !.ticks = 0, !.seen = 0]
  ELSE IF (vGen = e.pend \/ Fence(e)) /\ vStatus = "ready"
  THEN Saved(e)
  ELSE IF Newer(e)
  THEN IF e.seen + 1 >= 2 THEN Saved(e) ELSE GiveUp([e EXCEPT !.seen = e.seen + 1])
  ELSE GiveUp([e EXCEPT !.seen = 0])

\* E:7613-7690 one poll tick: GET the variant. A transport failure only waits longer (the
\* budget counts answered polls). On needs_attention, a ready variant of another
\* generation, or a skipped reconcile, refresh the phone first and apply after it
\* (PollApply; simplification: that step reads the variant again instead of keeping the
\* copy read here). Otherwise the answer is applied at once: no await between the GET
\* returning and applyPreviewVariant.
PollTick ==
  /\ app = "open" /\ ed.ppc = "tick"
  /\ \/ /\ Fault /\ faults' = faults - 1 /\ UNCHANGED <<ed, rec, again>>
     \/ /\ UNCHANGED faults
        /\ LET refresh == ed.recPend \/ vStatus = "attention" \/ (vStatus = "ready" /\ vGen # ed.pend)
               e == [ed EXCEPT !.caps = TRUE, !.recPend = FALSE]
           IN IF ~refresh THEN UNCHANGED <<rec, again>> /\ ed' = ApplyVariant(ed)
              ELSE IF ~Busy THEN StartRec("edpoll", FALSE) /\ ed' = [e EXCEPT !.ppc = "waitRec"]
                                 /\ UNCHANGED again
              ELSE UNCHANGED rec /\ ed' = [e EXCEPT !.ppc = "apply"] /\ again' = Again1(again, FALSE)
  /\ UNCHANGED <<srv, env, app, coord, req, pres, obs, oldRec, rep, decl, sched, hiReq, banner, upd426>>

PollWait ==
  /\ app = "open" /\ ed.ppc = "waitRec" /\ rec.src # "edpoll"
  /\ ed' = [ed EXCEPT !.ppc = "apply"]
  /\ UNCHANGED <<srv, faults, env, app, coord, req, pres, obs, rec, oldRec, rep, decl, sched,
                 hiReq, banner, again, upd426>>

PollApply ==
  /\ app = "open" /\ ed.ppc = "apply"
  /\ ed' = ApplyVariant(ed)
  /\ UNCHANGED <<srv, faults, env, app, coord, req, pres, obs, rec, oldRec, rep, decl, sched,
                 hiReq, banner, again, upd426>>

\* Banner "Retry device render" / "Resume rendering" (EC -> E retryDeviceRender). The entry
\* point refuses a change or update reason even when a stale banner calls it (E filter).
EdRetry ==
  /\ app = "open" /\ ed.save \in {"deviceRetry", "paused", "renderNow"} /\ ed.pc = "idle" /\ ed.pend # 0
  /\ ~YourTurn(pres)
  /\ IF pres.rsr
     THEN RetryNeedsAttention(EdAfterRetry)
     ELSE /\ ed' = [ed EXCEPT !.pc = "refresh", !.rflag = TRUE]
          /\ UNCHANGED <<srv, saves, stops, faults, coord, req, pres, obs, rec, oldRec, sched>>
  /\ UNCHANGED <<hot, unsupported, lvl, need, kind, missing, approval, chat, app, rep, decl, hiReq, banner,
                 again, upd426>>

EdCheckAgain ==                                      \* "Check preview again"
  /\ app = "open" /\ ed.save = "previewFailed" /\ ed.pc = "idle"
  /\ ed' = StartPreview(ed)
  /\ UNCHANGED <<srv, faults, env, app, coord, req, pres, obs, rec, oldRec, rep, decl, sched,
                 hiReq, banner, again, upd426>>

\* E:837-865: the retry banner follows the live render. A healed render goes back to
\* waiting for this save's video; a changed failure updates the banner.
EdHeal ==
  /\ app = "open" /\ ed.save \in EdBanner /\ pres # NoPres
  /\ ed' = IF SurfaceFail(pres)
           THEN [ed EXCEPT !.save = EdFail(pres), !.cause = pres.cause]
           ELSE IF ed.pend # 0
           THEN [ed EXCEPT !.save = "previewPending", !.ppc = "tick", !.ticks = 0, !.seen = 0,
                           !.cause = "none"]
           ELSE [ed EXCEPT !.save = "idle", !.cause = "none"]
  /\ ed' # ed
  /\ UNCHANGED <<srv, faults, env, app, coord, req, pres, obs, rec, oldRec, rep, decl, sched,
                 hiReq, banner, again, upd426>>

\* Chat events start the editor's draft sync (E:1831-1985). A failed read now writes only
\* refreshSync (a quiet "reconnecting" hint after 3 failures) and retries on its own
\* (E:2045-2073); "Couldn't refresh this edit" is gone.
PromptSync ==
  /\ app = "open" /\ ed.pc = "idle" /\ "sync" \in Events /\ Fault
  /\ faults' = faults - 1
  /\ banner' = banner
  /\ UNCHANGED <<srv, env, app, coord, req, pres, obs, rec, oldRec, rep, decl, sched, hiReq, ed, again,
                 upd426>>

\* ---------------------------------------------------------------- spec

Next ==
  \/ ChatPoll \/ Reread \/ RetryWait \/ RecFetch \/ RecApply \/ OldApply
  \/ PerfPrep \/ PerfExport \/ PerfSync \/ PerfComplete
  \/ Deliver \/ DeliveryFails \/ DeclDeliver \/ DeclFails
  \/ Background \/ Kill \/ Reopen \/ DayPasses \/ Reaper
  \/ OtherDeviceSave \/ OldDecline \/ OldRetry \/ Rollback \/ Cool \/ LoseCaps
  \/ Stop \/ ChatTryAgain \/ Relink \/ ChatChange \/ OpenChat \/ UpdateApp
  \/ EdSave \/ EdRefresh \/ EdAfter \/ PollTick \/ PollWait \/ PollApply
  \/ EdRetry \/ EdCheckAgain \/ EdHeal \/ PromptSync

\* The app and server keep working; a day passing, kills, faults and saves are optional.
\* Actions grouped under one WF have disjoint enabling conditions (one pc each), and only
\* bounded events (Stop, Save, kill, retries up to MaxId) move between members without a
\* member's own step, so WF of the group equals WF of each member; TLC checks fewer
\* conjuncts. (The default liveness runs also pass with one WF per action.)
Reconcile == ChatPollMust \/ RecFetch \/ RecApply   \* ~Busy | rec.pc = fetch | apply
Perform == PerfPrep \/ PerfExport \/ PerfSync \/ PerfComplete   \* coord.pc
EdSteps == EdRefresh \/ EdAfter                     \* ed.pc = refresh | after/waitRec
Poll == PollTick \/ PollWait \/ PollApply            \* ed.ppc = tick | waitRec | apply
SysFair ==
  /\ WF_vars(Reconcile) /\ WF_vars(RetryWait) /\ WF_vars(Reread) /\ WF_vars(OldApply)
  /\ WF_vars(Perform)
  /\ WF_vars(Deliver) /\ WF_vars(DeclDeliver) /\ WF_vars(EdSteps) /\ WF_vars(Poll)
  /\ WF_vars(EdHeal) /\ WF_vars(Cool) /\ WF_vars(Reopen)

\* A patient user who keeps tapping whatever retry the screen offers.
UserFair ==
  /\ SF_vars(ChatTryAgain) /\ SF_vars(EdRetry) /\ SF_vars(EdCheckAgain) /\ SF_vars(Relink)
  /\ SF_vars(ChatChange) /\ SF_vars(OpenChat) /\ SF_vars(UpdateApp)

Spec == Init /\ [][Next]_vars /\ SysFair
SpecUser == Spec /\ UserFair
\* The creator updates Kria when told to, and taps nothing else: the render then finishes
\* on its own (Kria fixes; no "Try again" after an update).
SpecUpdate == Spec /\ SF_vars(UpdateApp)

\* ---------------------------------------------------------------- rules

Genuine == {"origMissing", "timedOut"}   \* an original is missing; the phone was away a day

\* The app never shows needs-attention / "couldn't prepare" / "couldn't refresh" unless
\* a genuine, user-fixable failure happened.
NoSpuriousAttention ==
  /\ (Screen(pres) = "attention" => pres.cause \in Genuine)
  /\ (ed.save = "deviceRetry" => ed.cause \in Genuine)
  /\ ~banner

\* Told: a report for the pinned request is on its way (the coordinator's report or a
\* decline). ServerWaiting: the server still waits for this phone (a paused record does
\* not). NoReportInFlight: nothing is on its way.
Told == (rep # NoRep /\ rep.id = sId) \/ (decl.st = "sending" /\ decl.id = sId)
ServerWaiting == sPhase \in {"awaiting", "syncing"} /\ ~Paused
NoReportInFlight == rep = NoRep /\ decl.st # "sending"

\* When the phone decides it cannot render locally, the server is told. The decision on
\* the card is about req; a request pinned since (another device saved) is not decided yet.
CloudDecisionReported ==
  (pres.ph \in {"attention", "localReady"}
     /\ pres.cause \in {"cloudCaps", "structural", "hot", "unsupported", "outdated"}
     /\ req = sId)
    => (~ServerWaiting \/ Told)

\* From every needs-attention screen, and every screen that hands the creator the next
\* step (a change in chat, an app update), what the screen offers (or the app by itself)
\* reaches a published video.
\* Also any card on a record the server marked needs_attention (rsr), whatever its title:
\* a kept video included.
Stuck(p, e) == Screen(p) \in {"attention", "change", "update", "renderable"}
               \/ (p.rsr /\ p.ph \in {"attention", "localReady"})
               \/ e.save \in {"deviceRetry", "review", "update", "renderNow"}
Shown == app = "open" /\ Stuck(pres, ed)
Resolved == sId = MaxId \/ (sPhase = "published" /\ ~Stuck(pres, ed))
AttentionHasWayOut == Shown ~> Resolved

\* The phone and the server do not stay in contradicting states (phone: stopped / needs
\* attention; server: still waiting for this phone) without anyone tapping anything.
\* A finished video the phone cannot sync ("localReady" with an error) counts too.
Contradict == app = "open"
              /\ (pres.ph \in {"attention", "cancelled"} \/ (pres.ph = "localReady" /\ pres.cause # "none"))
              /\ ServerWaiting /\ NoReportInFlight /\ ~coord.run
ClientServerAgree == Contradict ~> ~Contradict

\* After a save, the editor eventually stops waiting and shows the new video.
PreviewConverges == ed.pend # 0 ~> (ed.pend = 0 \/ sId = MaxId)

\* The server never marks a request published with a video rendered for another request.
NoStalePublish == sPhase = "published" => pubFrom = sId

\* A published request never turns into needs_attention afterwards.
PublishedStaysPublished == sId \in pubIds => sPhase = "published"

\* The phone says "synced" only for a request the server really published.
SyncedMeansPublished == coord.ph = "synced" => coord.id \in pubIds

\* A late status answer never reinstalls an older request over a newer one.
NoStaleRequestReinstalled == req = 0 \/ req = hiReq

\* The server never re-pins the same recipe for a phone that will decline it the same way
\* (a retry loop): an unsupported recipe needs a change, an outdated one an update.
NoFutileRepin == ~futile

\* Once this phone applied a 426 for the pinned request, the server is told until it stops
\* waiting (the 426 path names its request; a finished video of an older one never hides it).
UpdateTold == (app = "open" /\ upd426 = sId /\ upd426 # 0 /\ ServerWaiting) => Told

\* The server re-pins an outdated decline automatically at most once: each re-pin needs a
\* build newer than the decliner, and an older build's decline never fails a record a newer
\* one holds (two devices on different builds never loop).
NoRepinLoop == autoRepins <= 1

\* With the chat open on a phone that can render the pinned request, and its card not
\* showing a finished video, the server stops waiting on its own (a published video, or the
\* phone's own report): nobody taps anything. (A save on another device while this card
\* shows its finished video is that device's to render.)
Capable == ~unsupported /\ ~Outdated /\ ~missing
CapableRenders == (app = "open" /\ chat /\ Capable /\ ServerWaiting /\ pres.ph # "synced")
                  ~> (~ServerWaiting \/ app # "open" \/ sId = MaxId)

\* Sanity: with no faults the model can publish (verify.py expects this to break).
NeverPublished == sPhase # "published"

TypeOK ==
  /\ sPhase \in {"awaiting", "syncing", "published", "attention"}
  /\ faults \in 0..MaxFaults
====
