# Steer now (KRI #3 of the Green 4/5 findings)

A message the creator sends while Kria is still working on an earlier one waits in a **bar attached
to the top of the input box** (Codex's waiting-message bar, adapted to the phone and Kria's light
look), not in the chat. If the creator does nothing, the order stays as today: the waiting message
runs once the earlier turn settles. **Steer** stops the earlier turn and starts the waiting message
at once; the **trash** removes it; **⋯** (or holding the bar) offers Edit message / Steer instead /
Cancel message. iOS only; the web is out of scope.

Direction (Emir, Slack DM, 2026-11-04): first "The new ask should look like a queued message …
“steer now” which can override"; after the first build: copy ChatGPT/Codex, no "Up next" label and
no separate Steer button in the chat. Reference: the Codex desktop bar (queue icon, message on one
line, "↳ Steer", trash, "⋯") and the Codex mobile menu (Cancel message / Steer instead / Edit
message).

Ships dark behind `KRIA_STEER_NOW_ENABLED` / `KRIA_STEER_NOW_USER_IDS` (capability `steer_now`).
Off is byte-identical to today on the server and in the app.

## The incident (Green 5, thread `D842DE91`, build 1888, 2026-11-04)

| time (UTC) | seq | event |
| --- | --- | --- |
| 12:45:44 | 4 | chip "Suggest an edit." → turn `9588752d`, `pending` |
| 12:45:58 | 6 | typed request (green screen, cut restart, small white lowercase middle captions, no music) → turn `bfd0b2c1`, **`queued`** behind `9588752d` (`request_outcome_link.relation: revision`) |
| 12:46:27 | 8, 9 | the chip turn's draft + approval card, in Turkish (phone locale), for the generic suggestion |
| 12:47:15 | 10–12 | creator approves the card → the generic video renders (grey default captions) |
| 12:52:09 | 18, 19 | the typed request finally plans; its card expired unanswered at 13:22 |

Nothing on the chip's card said a newer message was waiting.

## Is the existing cancel enough?

`POST /creation-threads/{id}/turns/{turn_id}/cancel` (`runtime.cancel_turn`) already does most of
it: it cancels a `pending` / `planning` / `awaiting_approval` turn (refuses `executing` /
`observing`), cancels its pending approvals, appends `turn_cancelled`, then promotes the queued
message in a second transaction (`_promote_queued_successor`). A cancel that lands while the
planner runs is safe: the worker's completion re-checks its lease and writes nothing
(`_owns_turn_lease`). TLC's `formal/kria_turns` model covers Submit/Cancel/Promote.

It is not enough on its own. Four gaps, each pinned by a test below:

1. **The card stays open and nothing is said.** The cancel writes only `turn_cancelled` (system
   role, no content). Both clients close an approval card only on an `approval_*` event, so the
   stopped turn's "Create this video" card keeps its buttons (a tap then fails), and the chat
   shows no line at all. The app also never learns the turn ended, so its Thinking row stays.
2. **A late tap stops Kria with nothing to switch to.** Cancel does not care whether a message is
   waiting. A Steer tap that arrives after the waiting message was already promoted (or removed)
   would stop the turn the creator wanted.
3. **Approve then cancel runs two turns at once.** Approving does not change the turn's status
   (`decide_approval` leaves it `awaiting_approval` until the render claim). A cancel in that
   window succeeds and promotes the waiting message, then `_claim_approval_dispatch` sets the
   cancelled turn to `executing` and renders anyway: the video is made, the chat says it stopped,
   and the waiting message plans next to a running render.
4. **A stopped message's asks are lost once a brief exists.** The promoted turn's brief
   extraction reads the prior brief plus *its own* message (`brief_context(latest_message=…)`).
   The stopped message never wrote a brief version, so its asks vanish. (With no prior brief the
   KRI-814 path already reads every unrecorded line, so Green 5 itself is covered.)

The app also cannot tell which turn a waiting message waits behind (turn status is not in the
projection), so it cannot hide Steer now while a video is being made.

**So: no new route.** The existing cancel gains one optional body field, `steer: true`, and the
queued `user_message` payload gains `queued_behind_turn_id`. Both exist only with the switch on.

## Server contract (switch on)

* Queued `user_message` payload: `queued_behind_turn_id` (the active turn). Not in
  `request_digest`.
* `POST …/turns/{A}/cancel {"expected_thread_revision": n, "steer": true}`:
  * switch off → 404 `steer_now_unavailable` (never silently a plain cancel);
  * `A` is not the running turn, or no message waits behind it → 409 `nothing_to_steer_to`;
  * `A`'s approval is already approved or consumed, or `A` is `executing` / `observing` → 409
    `turn_not_cancellable` ("Your video is already being made…");
  * otherwise, in one transaction: `A` → `cancelled`; each pending approval → `cancelled` with an
    `approval_cancelled` event; `turn_cancelled` with `steered_to_turn_id`; a `status_update`
    line (assistant, `turn_id: A`, chat language): "Stopped. Working on your newer message
    instead." / "Durdurdum. Bunun yerine yeni mesajın üzerinde çalışıyorum." Then the usual
    promotion and publish.
* Planning a turn that a Steer started: the brief extraction reads the stopped messages
  (chip taps excluded: they carry no creator words) together with its own message.

The trash and Edit send `only_if_queued: true` (Emir's review: a retry after a stale-revision answer
could otherwise cancel the message after Kria had picked it up, i.e. stop the running turn). The
turn is then locked with `status IN ('queued', 'cancelled')` in the same statement, so Postgres
re-checks it after waiting out a concurrent promotion; a turn that moved on gets 409
`turn_no_longer_queued` ("Kria already started on this message.") and nothing changes. A repeated
tap on a removed message keeps the old idempotent answer. Without the field the cancel is unchanged.
Before its one retry the app refreshes and re-reads the events (`SteerNowPolicy.removalRetry`): a
message that no longer waits is not tried again.

Plain cancel (no `steer`) is the trash and Edit. With the switch on, a plain cancel of a **queued**
turn marks its `turn_cancelled` `removed_while_waiting: true`, and every reader that hands the
creator's words to planning skips that message (`app/kria/removed_messages.py`): the pinned full
request (`_full_creator_request_sync`), the planner's history (`_load_thread_events`), the model
conversations, the unread lines a brief reads (`_unrecorded_creator_lines`) and the delegation check
(`_delegation_covers_request`). Before this, a removed waiting message was still read as an ask.
Readers only query for removals when one of the messages they read was queued. Switch off: nothing
is marked, plain cancel is byte-identical.

## The bar (iOS)

Measured from the Codex desktop screenshot, converted to an iPhone 17 Pro (input box 374 pt wide,
corner 30): the bar is 12 pt in from the box on each side, 44 pt tall (the tap minimum), top corners
12 pt, bottom tucked under the box's top edge. Queue icon 13 pt, then the message in Inter 14 on one
line cut off with "…", then "↳ Steer" (only while Steer can happen), trash and "⋯", each in a 44 pt
target, muted ink on Kria's `fill` with a `line` border. The waiting message is hidden from the chat
while it waits (it would show twice) and forever once removed; a steered message, or one Kria picked
up on its own, joins the chat. While a message is being sent behind busy Kria it goes straight to
the bar (no chat bubble). Edit puts the words back in the box ahead of anything already typed.
Turkish: "Yönlendir", "Mesajı düzenle", "Mesajı iptal et", "Bekleyen mesajı sil". Notes (third
message, too late, could not switch/remove) show above the bar.

## Decision table

`A` = the turn Kria is working on, `B` = the message that waits behind it.

| `A` is… | creator waits | creator taps **Steer now** | creator sends another message `C` |
| --- | --- | --- | --- |
| **planning** (Thinking row, no card) | `B` waits in the bar with Steer, trash and ⋯. `A`'s card arrives; `B` keeps waiting until it is approved or dismissed (today's order). | `A` stops and writes nothing. Chat: "Stopped. Working on your newer message instead." The bar goes, `B` joins the chat with the Thinking row. One card, for `B` (with `A`'s asks if `A` was a real message). | Not sent. `C` stays in the composer with a note above the bar: one message is already waiting; steer it or send this after Kria answers. (Server would 409 `queued_successor_exists`.) |
| **card being written** (planner returned, completion committing) | as above | Either the stop lands first (`A` writes no card), or the card lands first and the stop closes it (`approval_cancelled`). Either way: one live card at the end, for `B`. | as above |
| **card showing** | `A`'s card and `B` in the bar. Approve → `A` renders, Steer disappears, `B` runs after. "Change direction" → `B` runs. | `A`'s card closes (no buttons), stop line, `B` plans → one card, for `B`. | as above |
| **approved, video being made** | `B` in the bar, **no Steer** (trash and ⋯ stay); runs after the video. | Not offered. A tap that raced the approval gets 409 `turn_not_cancellable`: the button hides and the chat says the video is already being made and the message runs next. The video is never cancelled. | as above |
| **done** (answered, rendered, failed, stopped) | `B` is already running (Thinking row) or answered. | Not offered. A stale tap gets 409 (`nothing_to_steer_to` / already finished); the app refreshes quietly. `B` is never stopped by it. | `C` is sent normally (it waits behind `B` if `B` is still running). |

Trash or Edit, in any row while `B` waits: the bar goes, `B` never shows in the chat and is never
read (Edit puts its words back in the box). A tap after Kria picked `B` up gets 409
`turn_no_longer_queued` and a note; `B` keeps running.

Switch off: today's behaviour in every cell (the composer is blocked while Kria plans, a queued
message looks like any sent message, no bar).

## Ways it could fail (each has a test)

| # | failure | test |
| --- | --- | --- |
| F1 | Steer tapped as the card appears: two live cards, or an orphan card with live buttons | `test_steer_now_while_the_card_is_being_written[stop_first,card_first]` |
| F2 | Steer and Approve at once: both win (video renders *and* `B` plans), a deadlock, or a 500 | `test_steer_now_after_approve_is_refused_and_the_video_runs`, `test_steer_now_first_makes_approve_refuse`, `test_steer_and_approve_at_the_same_moment_never_both_win` |
| F3 | A third message while one waits is lost, or shows "Something went wrong" | `SteerNowUITests.testThirdMessageStaysInComposerWithANote`, `test_a_third_message_waits_for_the_slot` |
| F3b | A removed (trash / Edit) waiting message is still read by planning, shows in the chat, or holds a later delegation | `test_a_removed_waiting_message_is_marked`, `test_kria_never_reads_a_removed_waiting_message`, `test_a_removed_line_never_holds_a_later_delegation`, `SteerNowUITests.testTrashRemovesTheWaitingMessage`, `testEditFromTheMenuPutsTheTextBackInTheBox` |
| F3c | Re-sending the removed words is answered as a copy | `test_sending_the_removed_words_again_is_a_new_message` |
| F3d | The waiting message shows twice (bar and chat) | `SteerNowUITests.testWaitingBarAndSteerWhileKriaPlans`, `SteerNowPolicyTests.testTheWaitingMessageIsHiddenFromTheChat` |
| F4 | The stopped turn vanishes with no line, its card keeps live buttons, the Thinking row never clears | `test_green5_steer_now_gives_one_card_for_the_typed_request`, `SteerNowUITests.testSteerClosesTheCardAndSaysWhy` |
| F5 | Asks from a stopped real message are lost (a prior brief exists) | `test_steer_now_keeps_the_stopped_messages_asks[prior_brief]` |
| F6 | A stale Steer (its turn already finished, `B` already running) stops `B` | `test_a_stale_or_double_steer_never_stops_the_waiting_message` |
| F7 | Steer with nothing waiting stops Kria anyway | `test_steer_now_with_nothing_waiting_is_refused` |
| F8 | The thread revision moved (a worker event) between render and tap: Steer fails for good | `SteerNowUITests.testSteerRetriesOnceAfterAStaleRevision` |
| F9 | A double tap stops the promoted message | `test_a_stale_or_double_steer_never_stops_the_waiting_message` |
| F10 | The stop line is in the old language (Green 5's Turkish) | `test_green5_steer_now_gives_one_card_for_the_typed_request` (English after the English message), `test_the_stop_line_follows_a_turkish_chat` |
| F11 | The cancel's promotion dies after commit: `B` stuck | existing `test_a_follow_up_is_not_stuck_when_cancel_dies_before_promoting` (unchanged path) |
| F12 | Switch off changes anything | `test_switch_off_is_byte_identical`, `SteerNowUITests.testSwitchOffLooksLikeToday` |
| F13 | Old app builds see new events they cannot read | additive only: `approval_cancelled` / `status_update` are already in both clients' lists; `steer` is opt-in |
| F14 | A Steer tap makes Kria spend twice | the in-flight model call of `A` finishes but is discarded (lease check); `B`'s planning is the call that would have run anyway. No retry loop. |
| F15 | The chip text "Suggest an edit." is read as a creator ask | `test_steer_now_keeps_the_stopped_messages_asks` asserts the chip line is not carried |
| F16 | An approved video is cancelled | `test_steer_now_never_stops_an_approved_video` |
| F17 | The bar stays after `B` started, or Steer shows while the video renders | `SteerNowUITests.testNoSteerWhileTheVideoIsBeingMade`, `SteerNowPolicyTests` |
| F18 | The bar's controls are not reachable with VoiceOver, or smaller than 44 pt | `SteerNowUITests` query by accessibility id and label; `WaitingMessageBar` frames |
| F19 | The menu differs between "⋯" and holding the bar | `SteerNowUITests.testHoldingTheBarOpensTheSameMenu` |
| F20 | The trash / Edit hits a stale revision, Kria picks the message up, and the retry cancels the running turn (or puts its words back in the box); or the condition blocks a real removal / a repeated tap | `test_a_retried_removal_never_stops_a_message_kria_started`, `test_a_retried_removal_of_a_still_waiting_message_goes_through`, `SteerNowPolicyTests.testARemovalIs*`, `SteerNowUITests.testEditAfterKriaPickedTheMessageUpNeverStopsIt` |
| F21 | The "still waiting" check is a read before the write, so a promotion committing in between slips through | `test_the_still_waiting_check_and_the_removal_are_one_step` |

## Rollout

`fly secrets set -a nova-video KRIA_STEER_NOW_ENABLED=true` (or add creator UUIDs to
`KRIA_STEER_NOW_USER_IDS` for a canary), restart api + worker. No Vercel twin: the app reads the
capability. Kill switch: set it false and restart; waiting messages then behave as today.
