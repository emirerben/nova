# Steer now (KRI #3 of the Green 4/5 findings)

A message the creator sends while Kria is still working on an earlier one shows in the chat as a
**waiting message**, with a **Steer now** button next to it. If the creator does nothing, the order
stays as today: the waiting message runs once the earlier turn settles. Steer now stops the earlier
turn and starts the waiting message at once. iOS only; the web is out of scope.

Direction (Emir, Slack DM, 2026-11-04): "The new ask should look like a queued message. There
should be a button next to the queued messag that says something like “steer now” which can
override".

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

Plain cancel (no `steer`) is unchanged, flag on or off.

## Decision table

`A` = the turn Kria is working on, `B` = the message that waits behind it.

| `A` is… | creator waits | creator taps **Steer now** | creator sends another message `C` |
| --- | --- | --- | --- |
| **planning** (Thinking row, no card) | `B` shows "Waiting" + Steer now. `A`'s card arrives; `B` keeps waiting until it is approved or dismissed (today's order). | `A` stops and writes nothing. Chat: "Stopped. Working on your newer message instead." `B` loses its Waiting look and gets the Thinking row. One card, for `B` (with `A`'s asks if `A` was a real message). | Not sent. `C` stays in the composer with a note: one message is already waiting; tap Steer now or send this after Kria answers. (Server would 409 `queued_successor_exists`.) |
| **card being written** (planner returned, completion committing) | as above | Either the stop lands first (`A` writes no card), or the card lands first and the stop closes it (`approval_cancelled`). Either way: one live card at the end, for `B`. | as above |
| **card showing** | `A`'s card and `B` waiting with Steer now. Approve → `A` renders, Steer now disappears, `B` runs after. "Change direction" → `B` runs. | `A`'s card closes (no buttons), stop line, `B` plans → one card, for `B`. | as above |
| **approved, video being made** | `B` shows "Waiting", **no Steer now**; runs after the video. | Not offered. A tap that raced the approval gets 409 `turn_not_cancellable`: the button hides and the chat says the video is already being made and the message runs next. The video is never cancelled. | as above |
| **done** (answered, rendered, failed, stopped) | `B` is already running (Thinking row) or answered. | Not offered. A stale tap gets 409 (`nothing_to_steer_to` / already finished); the app refreshes quietly. `B` is never stopped by it. | `C` is sent normally (it waits behind `B` if `B` is still running). |

Switch off: today's behaviour in every cell (the composer is blocked while Kria plans, a queued
message looks like any sent message, no button).

## Ways it could fail (each has a test)

| # | failure | test |
| --- | --- | --- |
| F1 | Steer tapped as the card appears: two live cards, or an orphan card with live buttons | `test_steer_now_while_the_card_is_being_written[stop_first,card_first]` |
| F2 | Steer and Approve at once: both win (video renders *and* `B` plans), a deadlock, or a 500 | `test_steer_now_after_approve_is_refused_and_the_video_runs`, `test_steer_now_first_makes_approve_refuse`, `test_steer_and_approve_at_the_same_moment_never_both_win` |
| F3 | A third message while one waits is lost, or shows "Something went wrong" | `SteerNowUITests.testThirdMessageStaysInComposerWithANote`, `test_a_third_message_waits_for_the_slot` |
| F4 | The stopped turn vanishes with no line, its card keeps live buttons, the Thinking row never clears | `test_green5_steer_now_gives_one_card_for_the_typed_request`, `SteerNowUITests.testSteerNowClosesTheCardAndSaysWhy` |
| F5 | Asks from a stopped real message are lost (a prior brief exists) | `test_steer_now_keeps_the_stopped_messages_asks[prior_brief]` |
| F6 | A stale Steer (its turn already finished, `B` already running) stops `B` | `test_a_stale_or_double_steer_never_stops_the_waiting_message` |
| F7 | Steer with nothing waiting stops Kria anyway | `test_steer_now_with_nothing_waiting_is_refused` |
| F8 | The thread revision moved (a worker event) between render and tap: Steer fails for good | `SteerNowUITests.testSteerNowRetriesOnceAfterAStaleRevision` |
| F9 | A double tap stops the promoted message | `test_a_stale_or_double_steer_never_stops_the_waiting_message` |
| F10 | The stop line is in the old language (Green 5's Turkish) | `test_green5_steer_now_gives_one_card_for_the_typed_request` (English after the English message), `test_the_stop_line_follows_a_turkish_chat` |
| F11 | The cancel's promotion dies after commit: `B` stuck | existing `test_a_follow_up_is_not_stuck_when_cancel_dies_before_promoting` (unchanged path) |
| F12 | Switch off changes anything | `test_switch_off_is_byte_identical`, `SteerNowUITests.testSwitchOffLooksLikeToday` |
| F13 | Old app builds see new events they cannot read | additive only: `approval_cancelled` / `status_update` are already in both clients' lists; `steer` is opt-in |
| F14 | A Steer tap makes Kria spend twice | the in-flight model call of `A` finishes but is discarded (lease check); `B`'s planning is the call that would have run anyway. No retry loop. |
| F15 | The chip text "Suggest an edit." is read as a creator ask | `test_steer_now_keeps_the_stopped_messages_asks` asserts the chip line is not carried |
| F16 | An approved video is cancelled | `test_steer_now_never_stops_an_approved_video` |
| F17 | The Waiting look stays after `B` started, or Steer now shows while the video renders | `SteerNowUITests.testNoSteerNowWhileTheVideoIsBeingMade`, `SteerNowPolicyTests` |
| F18 | Steer now is not reachable with VoiceOver | `SteerNowUITests` query by accessibility id and label |

## Rollout

`fly secrets set -a nova-video KRIA_STEER_NOW_ENABLED=true` (or add creator UUIDs to
`KRIA_STEER_NOW_USER_IDS` for a canary), restart api + worker. No Vercel twin: the app reads the
capability. Kill switch: set it false and restart; waiting messages then behave as today.
