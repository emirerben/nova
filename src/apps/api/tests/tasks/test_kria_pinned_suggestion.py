"""Pinned suggestion taps through the real ``run_kria_turn`` path.

Failure catalogue, written before any seam code. Every row is one fixture below that drives
submit -> claim -> plan -> row-locked commit on real Postgres; none restates an implementation
detail. The rule each row defends: a tap runs the action the card was proven with, through the
ONE existing draft path, or says plainly that it no longer fits. It never reaches the free planner.

Pin intake (submit and claim)
  FM1  flag off: a valid pin is inert. No stamp on the event, none on the claim, the legacy planner
       plans the turn, the pinned planner never runs.
  FM2  unresolvable pin: an `ss_` id missing from THIS thread's ring (another thread's set, an
       evicted set, an unknown id) answers "no longer fits". The planner never sees it.
  FM3  typed words win: a pin whose message is not the stored prompt is the creator's own request
       (KRI-129); it goes to the planner unpinned.
  FM4  a legacy `sc_` chip id with the flag on stays on the KRI-756 path, never pinned.
  FM20 the ring entry changes between submit and run (stored action no longer the stamped one):
       refuse, apply nothing.

Execution
  FM5  exact base, per allowed kind (sfx, transition, music level, caption meta, remove_clip): a new
       draft revision with a real diff, receipts written and met, one typed brief version owned by
       the turn, `can_undo` with the previous head as parent, no approval, no render, no Job, no
       model constructed (copilot, Main Creator, extractor all poisoned).
  FM6  the deferred brief extraction is not scheduled for a pinned turn.
  FM7  stale base (a draft head appeared after the set was served): the action is rebuilt from the
       registry and the current facts and applied once. An index-addressed op (remove_clip
       slot_index) never lands on whichever clip now sits at that index.
  FM8  stale base and the change is gone (already present / no longer available): "no longer fits",
       head unchanged, no brief version, the suggester is asked to refresh.
  FM9  the state moves between planning and the row lock (an editor Save): the in-lock check over
       `editor_base.projected` refuses; nothing is written.
  FM10 the creator's unsaved editor state: the card is rebuilt against it, their edits become the
       parent revision so Undo returns to exactly what they had. A stale client state is the honest
       stale-editor reply.
  FM11 unsaved editor state + a speech-cut op: refused before compile. With the lane allow-list
       widened (a later kind), the existing in-lock refusal still fires: there is no second path.
  FM12 allow-list: a stored action carrying a text-carrying op (add_text, rewrite_text, set_title)
       is refused, so no agent copy reaches the screen through this seam.
  FM13 lost turn lease (another worker reclaimed the turn): the commit refuses, nothing is written.
  FM14 conversation-revision requeue: a real message lands between claim and commit; the turn goes
       back to pending, writes no brief version or draft, and the second run applies exactly once.
  FM15 queued behind an in-flight turn: the stamp survives the queue and the promoted turn plans
       against the state the predecessor left.
  FM16 creative-copy consent (KRI-506): pending or approved copy blocks the draft with the existing
       reply; nothing is applied.
  FM17 duplicate tap: the same client_event_id replays one turn; a late second tap on an applied
       card is "no longer fits", never a second change.
  FM18 a failed render's recovery target, or a render in flight: no apply, an honest reply.
  FM19 Creative Brief off for the creator: the draft applies with no brief rows, like the copilot.
"""

from __future__ import annotations
