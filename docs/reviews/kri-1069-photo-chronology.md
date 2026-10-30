# KRI-1069: a dated Visuals photo in a filming-order montage

Parent incident: KRI-1066 (Leverkusen-Nürnberg DFB-Pokal M1, iPhone chat 2026-10-29, thread
`4144da86`, Fly `3c8d4dd82`, TestFlight 1801, protocol 24). Sibling fixes: KRI-1067 (the
"final whistle" pin), KRI-1068 (pins that can't be withdrawn).

## Original request (verbatim)

> Make a 45-second vertical montage of my night at Leverkusen vs Nürnberg, DFB-Pokal round 2,
> final score 5–2. Keep it in chronological order from arrival to the final whistle. Open on
> the lineup sheet, end with a title card "Leverkusen 5–2 Nürnberg · BayArena". Upbeat music,
> cut on the beat.

Media: 9 iPhone videos (IMG_1012…IMG_1043, attached out of filming order: IMG_1043 first,
IMG_1012 last) and 4 Visuals photos, one of them the printed lineup sheet (IMG_1038).

## Facts the expectation rests on (read-only prod DB read, 2026-10-30)

- All 9 `clip_assignments` carry `capture.capture_time`, 18:21:28Z (IMG_1012, team bus) to
  21:47:37Z (IMG_1043, confetti).
- All 4 Visuals photos carry `plan_item_assets.capture`; the lineup sheet IMG_1038 was taken
  at 20:33:20Z.
- Turn 3 replied "I need capture times for every selected clip to verify chronological
  order." No draft, no job.

## Observable acceptance

1. A chronological montage with dated clips plus a selected dated Visuals photo reaches a
   draft. The photo plays at its own capture time among the clips (IMG_1038 between the
   last clip filmed before 20:33:20Z and the first one after it), unless a first/last pin
   places it ("open on the lineup sheet" → the lineup sheet plays first, then filming order).
2. The order the chat promises (render contract), the order the phone montage lays out and
   the order the phone verifier checks are the same order.
3. "No filming time" is said only for an item that really has none, and the line names it
   (`"IMG_1038" has no filming time, so I can't put it in the order you filmed.`), in the
   chat's language. A selected photo the format cannot order by time is never called undated.
4. Clip-only filming order, KRI-503 sequence seating ("chronological, starting with the blue
   video") and KRI-661 single-clip edits behave as before.

## Wrong results (must be refused or reported)

- The photo spread evenly between clips while the contract pins capture order, or the photo
  first while the plan seats it mid-montage: the phone verifier must refuse the recipe.
- A photo group judged "no clips of it" in the plan receipt when it led the cut.
