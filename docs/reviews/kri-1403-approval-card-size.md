# KRI-1403: chat goes white when a long approval card arrives

## Report

2026-11-13 ~19:19 UTC, ylab chat (thread 8044373b), TestFlight 1977, protocol 49, German. Right after Kria posted
the draft and its approval card (`draft_applied` + `approval_requested` at 19:19:14) the whole chat turned white
for a few seconds, then came back on its own. The API log shows the phone kept polling with 200s: not a server
failure.

The card's `consequence_summary` was 1,788 characters, 14 bullet lines:

- 6 requirement lines, three of them "Wie besprochen weggelassen" (left out as agreed) for asks the creator had
  agreed to drop one tap earlier;
- 4 "Schau dir diese Stellen im Video an" lines, two with "(Das kann ich noch nicht automatisch prüfen)", a reason
  English and Turkish leave out under that header;
- 2 "Hinweis zu diesem Video" lines about Kria's own plan ideas the route doesn't use (a hook idea, a six-part story
  structure), which the creator never asked for.

## Root cause

The chat draws the approval stage as one row of its `LazyVStack`. The card drew that summary at 22 pt under a stock
subtitle, a "Creative direction" label and three fixed rows ("Format · short-form", "Rhythm: Natural, intentional",
"Opening: The story, right away"): 2,229 pt on a 656 pt visible chat. In the same update the draft row (drawn
alone one round trip earlier) is removed and that row is inserted at the end while the chat follows the end; a row
taller than the viewport swapped in like this leaves the lazy stack drawing nothing, the same failure as KRI-1351
(media receipts as one tall row).

## Decision

Emir, 2026-11-14 (DM thread on the direction question): the card is too big because the fonts are too big and it
repeats unneeded text; fix it with a few improvements. Follow-up answered "1B, 2A": also cut the Heads-up lines from
the card, and show the draft and the card together.

## Fix

- iOS `DirectionStage`: the summary is 15 pt chat text; the stock subtitle, label and fixed rows are gone.
- API, card only (`approval_card_text`, `_card_summary`): the heads-up block keeps only the research line (plan
  notes stay in the draft reply and on the approval's `not_applied` record); two or more "Left out as agreed" lines
  become one count line.
- API `brief_checks._NEUTRAL_REASONS`: every catalog language's "can't check automatically" reasons are neutral,
  like English and Turkish.
- iOS `refreshDelta`: a delta that brings an open approval request reads the approval first and merges the events
  and sets the card in one update, so the draft never shows alone and the chat moves once. A failed read falls back
  to the old order.
- iOS: approval ids are compared as UUIDs (the server's are lowercase, `uuidString` is uppercase), so a poll no
  longer re-reads an unchanged approval.

## Evidence (iPhone 17e simulator, iOS 26.5, Debug, offline chat fixture)

`LongApprovalCardUITests/testTheYlabApprovalCardFitsTheScreenAndTheChatNeverGoesBlank` replays the ylab draft (the
prod card text, word for word) with the approval read answered 2 s late, and samples the chat from Send until 6 s
after the card lands.

| | card height (heading to "Create this video") | visible chat |
|---|---|---|
| before (main, prod card text) | 2,229 pt | 656 pt |
| font and filler fix only, prod card text | 1,090 pt | 656 pt |
| this change, card text the server now sends (857 characters) | 606 pt | 656 pt |

The same test also fails if the draft message (its "Hinweis zu diesem Video" line, which only the draft carries) is
on screen at any sample before the card: with this change it never is, and the chat had text on screen in every
sample. Red control: with only the `refreshDelta` read-first step switched off (a temporary environment check, not
committed), the same test fails with the draft seen alone before its card.

The white flash itself did not reproduce in the simulator before the fix (no blank sample in either run), so the
evidence for the white chat is the size bound: no approval card on this input is taller than the visible chat.
The card text above is computed from the prod card with this branch's server functions (German bound); the draft
message stays the prod text word for word.

Not covered: a physical iPhone; other very long cards (a Talking card with many receipts in another language).
Owed: a device check on the next TestFlight build.
