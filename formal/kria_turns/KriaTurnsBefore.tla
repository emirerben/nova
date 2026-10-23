---- MODULE KriaTurnsBefore ----
\* Kria v2 chat turns on one thread, as origin/main ran them at bf7be5f92 (before
\* codex/kri-chat-turn-tla). Kept as the record of what TLC found; KriaTurns.tla
\* models the code as it is now. Each action is one database transaction (or one
\* crash) in the code it cites, at that commit:
\*   R = src/apps/api/app/kria/runtime.py
\*   T = src/apps/api/app/tasks/kria_runtime.py
\*   P = src/apps/api/app/kria/planner.py
\* A turn is named by the user message that created it (1..Msgs).
EXTENDS Naturals, FiniteSets

CONSTANTS Msgs, Workers, MaxEpoch

Turns == 1..Msgs
None == 0
Active == {"pending", "planning"}   \* R:720 also counts executing/awaiting_approval/observing

VARIABLES
  status,     \* CreatorAgentTurn.status per turn ("none" = not sent yet)
  owner,      \* lease_owner: the Celery task id, which every publish of a turn sets to
              \* the turn id (T:2697; routes/kria_runtime.py:101; T:6493), so a
              \* redelivered or republished run has the SAME owner as the first run
  epoch,      \* CreatorAgentTurn.lease_epoch
  live,       \* lease_expires_at > now()
  claimedRev, \* thread.revision the run claimed against (T:2300)
  rev,        \* CreationThread.revision; every event bumps it (R:456 _append_event)
  queuedAt,   \* revisions whose event is a queued follow-up's user_message (R:933-940)
  sent,       \* user messages sent so far
  w,          \* each worker's run: [pc, turn, epoch, read]
  api,        \* the cancel route between its commit and its promotion (R:1203-1207)
  answered,   \* ghost: the messages each committed reply was planned with
  committer   \* ghost: the lease epoch held by the run that committed each turn

vars == <<status, owner, epoch, live, claimedRev, rev, queuedAt, sent, w, api, answered, committer>>

Idle == [pc |-> "idle", turn |-> None, epoch |-> 0, read |-> {}]

\* Messages 1..n as an enumerated set (TLC prints a range as 1..n).
UpTo(n) == {m \in Turns : m <= n}

Init ==
  /\ status = [t \in Turns |-> "none"]
  /\ owner = [t \in Turns |-> None]
  /\ epoch = [t \in Turns |-> 0]
  /\ live = [t \in Turns |-> FALSE]
  /\ claimedRev = [t \in Turns |-> 0]
  /\ rev = 0
  /\ queuedAt = {}
  /\ sent = 0
  /\ w = [x \in Workers |-> Idle]
  /\ api = "idle"
  /\ answered = [t \in Turns |-> {}]
  /\ committer = [t \in Turns |-> 0]

RunningTurn == \E t \in Turns : status[t] \in Active
Queued == {t \in Turns : status[t] = "queued"}

\* ---- the creator ----

\* submit_turn, one transaction under the Thread lock (R:594, commit R:1034).
Submit ==
  /\ sent < Msgs
  /\ Queued = {}                                \* R:909-926 queued_successor_exists
  /\ LET t == sent + 1 IN
       status' = [status EXCEPT ![t] = IF RunningTurn THEN "queued" ELSE "pending"]  \* R:928-931
  /\ sent' = sent + 1
  /\ rev' = rev + 1                             \* R:933 user_message event
  /\ queuedAt' = IF RunningTurn THEN queuedAt \cup {rev + 1} ELSE queuedAt
  /\ UNCHANGED <<owner, epoch, live, claimedRev, w, api, answered, committer>>

\* cancel_turn: Turn -> Thread, commit (R:1121-1202). The promotion is a second
\* transaction (R:1207), modelled as CancelPromote.
Cancel(t) ==
  /\ api = "idle"
  /\ status[t] \in {"pending", "planning", "queued"}  \* R:1150-1167
  /\ status' = [status EXCEPT ![t] = "cancelled"]
  /\ rev' = rev + 1                                    \* R:1190 turn_cancelled event
  /\ api' = "promote"
  /\ UNCHANGED <<owner, epoch, live, claimedRev, queuedAt, sent, w, answered, committer>>

\* ---- the system ----

\* _promote_queued_successor(_sync): lock the oldest queued Turn, lock Thread, set
\* pending (R:1367-1391, T:2437-2447). Nothing checks that no turn runs.
PromoteNext(s) ==
  IF Queued = {} THEN s
  ELSE LET q == CHOOSE q \in Queued : \A o \in Queued : q <= o
       IN [s EXCEPT ![q] = "pending"]

CancelPromote ==
  /\ api = "promote"
  /\ status' = PromoteNext(status)
  /\ api' = "idle"
  /\ UNCHANGED <<owner, epoch, live, claimedRev, rev, queuedAt, sent, w, answered, committer>>

\* _claim (T:2243-2280): a pending turn, or a planning turn whose lease lapsed.
Claim(x, t) ==
  /\ w[x].pc = "idle"
  /\ epoch[t] < MaxEpoch
  /\ \/ status[t] = "pending"                        \* T:2259
     \/ status[t] = "planning" /\ ~live[t]          \* T:2253-2258 expired_read_lease
  /\ status' = [status EXCEPT ![t] = "planning"]    \* T:2274
  /\ owner' = [owner EXCEPT ![t] = t]               \* T:2275 lease_owner = task id
  /\ epoch' = [epoch EXCEPT ![t] = @ + 1]           \* T:2276
  /\ live' = [live EXCEPT ![t] = TRUE]              \* T:2277
  /\ claimedRev' = [claimedRev EXCEPT ![t] = rev]   \* T:2300 int(thread.revision)
  /\ w' = [w EXCEPT ![x] = [pc |-> "planning", turn |-> t, epoch |-> epoch[t] + 1, read |-> {}]]
  /\ UNCHANGED <<rev, queuedAt, sent, api, answered, committer>>

\* The planner loads every user/assistant event of the thread, with no upper bound
\* on sequence (P:1054-1068), after the claim committed.
ReadHistory(x) ==
  /\ w[x].pc = "planning"
  /\ w' = [w EXCEPT ![x].pc = "planned", ![x].read = UpTo(sent)]
  /\ UNCHANGED <<status, owner, epoch, live, claimedRev, rev, queuedAt, sent, api, answered, committer>>

\* The heartbeat stops or falls behind, and the lease's 15 s run out.
LeaseLapses(t) ==
  /\ status[t] = "planning"                         \* T:1931-1950 renews only a planning turn
  /\ live[t]
  /\ live' = [live EXCEPT ![t] = FALSE]
  /\ UNCHANGED <<status, owner, epoch, claimedRev, rev, queuedAt, sent, w, api, answered, committer>>

OwnsLease(x, t) ==                                   \* _owns_turn_lease T:2166-2179
  /\ status[t] = "planning"
  /\ owner[t] = w[x].turn                          \* always true: same task id
  /\ epoch[t] = w[x].epoch
  /\ live[t]

\* The revision fence: conversation_revision_matches_sync (services/creation_thread_titles.py:81).
FenceFails(t) == rev # claimedRev[t]

\* _complete_response_turn / _complete_draft_turn: Turn lock, lease check, Thread
\* lock, revision fence, commit (T:256-353, T:813-916).
Complete(x) ==
  /\ w[x].pc = "planned"
  /\ LET t == w[x].turn IN
     IF ~OwnsLease(x, t)
     THEN /\ w' = [w EXCEPT ![x] = Idle]                 \* T:270-277 committed=False
          /\ UNCHANGED <<status, owner, live, answered, committer>>
     ELSE IF FenceFails(t)                                \* T:284
     THEN /\ status' = [status EXCEPT ![t] = "pending"]  \* T:285-289 requeue
          /\ owner' = [owner EXCEPT ![t] = None]
          /\ live' = [live EXCEPT ![t] = FALSE]
          /\ w' = [w EXCEPT ![x] = Idle]
          /\ UNCHANGED <<answered, committer>>
     ELSE /\ status' = [status EXCEPT ![t] = "completed"] \* T:349-353
          /\ owner' = [owner EXCEPT ![t] = None]
          /\ live' = [live EXCEPT ![t] = FALSE]
          /\ answered' = [answered EXCEPT ![t] = w[x].read]
          /\ committer' = [committer EXCEPT ![t] = w[x].epoch]
          /\ w' = [w EXCEPT ![x].pc = "promote"]
  /\ UNCHANGED <<epoch, claimedRev, rev, queuedAt, sent, api>>

\* _promote_queued_successor_sync, its own transaction after the commit (T:358-361).
WorkerPromote(x) ==
  /\ w[x].pc = "promote"
  /\ status' = PromoteNext(status)
  /\ w' = [w EXCEPT ![x] = Idle]
  /\ UNCHANGED <<owner, epoch, live, claimedRev, rev, queuedAt, sent, api, answered, committer>>

\* The worker process dies (deploy, OOM, autostop, time limit). Celery redelivers the
\* message (acks_late); the rerun is another Claim, which _claim ignores once the
\* turn is completed (T:2259).
WorkerCrash(x) ==
  /\ w[x].pc # "idle"
  /\ w' = [w EXCEPT ![x] = Idle]
  /\ UNCHANGED <<status, owner, epoch, live, claimedRev, rev, queuedAt, sent, api, answered, committer>>

\* The API process dies between cancel's commit and its promotion.
ApiCrash ==
  /\ api = "promote"
  /\ api' = "idle"
  /\ UNCHANGED <<status, owner, epoch, live, claimedRev, rev, queuedAt, sent, w, answered, committer>>

\* reconcile_kria_turns (T:6417-6452): only pending and lapsed planning rows.
Reconcile(t) ==
  /\ status[t] = "planning"
  /\ ~live[t]                                       \* T:6432 lease_expires_at < now()
  /\ status' = [status EXCEPT ![t] = "pending"]
  /\ owner' = [owner EXCEPT ![t] = None]
  /\ UNCHANGED <<epoch, live, claimedRev, rev, queuedAt, sent, w, api, answered, committer>>

\* Everything except the creator, and except crashes (a crash is not progress).
System ==
  \/ CancelPromote
  \/ \E x \in Workers : ReadHistory(x) \/ Complete(x) \/ WorkerPromote(x)
  \/ \E x \in Workers, t \in Turns : Claim(x, t)
  \/ \E t \in Turns : LeaseLapses(t) \/ Reconcile(t)

Next ==
  \/ Submit
  \/ \E t \in Turns : Cancel(t)
  \/ System
  \/ ApiCrash
  \/ \E x \in Workers : WorkerCrash(x)

Spec == Init /\ [][Next]_vars

Settled == ~ENABLED System

\* ---- rules ----

\* Kria works on one of your messages at a time.
OneAtATime == Cardinality({t \in Turns : status[t] \in Active}) <= 1

\* A reply is only saved by the run that holds the turn's current lease.
NoStaleCommit == \A t \in Turns : status[t] = "completed" => committer[t] = epoch[t]

\* Once Kria is done, a follow-up you sent is never left waiting with nothing running,
\* which refuses every new message with "A follow-up is already waiting".
NoStuckFollowUp == Settled => ~(Queued # {} /\ ~RunningTurn)

\* Kria answers each message with only what you had said up to it; your next message
\* gets its own answer, so it is not acted on twice.
AnswersOnlyUpToItsMessage == \A t \in Turns : answered[t] \subseteq UpTo(t)
====
