# KRI-1346 (reply half) + KRI-1347: honest hold / draft / review lines

Parent incident: KRI-1342 (yorum3, thread `d9d7ad3d`, job `7eba9903`, Fly `4f6ed3014`).
Switch: `KRIA_HONEST_REPLY_LINES_ENABLED` (default `false`; off = byte-identical replies).
Integrated with #2256 (closing card + corner label, KRI-1346 render half): this branch carries
#2256's commits, and the closing-card "can't" / promise / finished check is #2256's
`_check_closing_card` (not behind this switch). This PR adds the cross-line consistency.

## What the creator saw

| Surface | Line | What was true |
| --- | --- | --- |
| Hold | "End on a red screen with yellow text …: Putting a plain colour behind you isn't available on your iPhone yet." | r5 is `kind: text` with `facts.closing` + `facts.background_color`: a closing card, not a speaker background. `asks_speaker_background` read any `background_color` fact. |
| Draft | "Done: … show that picture full screen" **and** "Heads-up: I can't add the treatments (overlays) on this video yet, so they won't be applied" | The phone Talking writer drew them (`phone_lane_receipt.applied = ["overlays"]`, `phone_beat_receipt.placed` = 4). `talking_head.FIELD_HANDLING["optional_treatments"]` was a static `NotApplied`, while the manifest (`phone_subtitled_overlays_supported`) advertised `media_overlays`. |
| Draft | "Have a look at these in the video: small label in the top left for the whole video" **and** "\"Yorumla\" shows as the opening title for the first few seconds. Talking edits can't keep text on screen the whole time yet." | The plan had already turned the label into a short opening title (KRI-1009, `strategy_policy._talking_top_text_as_title`), so "have a look" promised what the next sentence denied. |

Older runs with the same class (from Linear and kit notes): KRI-1107 (draft "have a look" vs
review "couldn't"), KRI-1121/1128 (wrong reason, contradicts the plan), KRI-1234 ("Couldn't"
while the video had the cards), KRI-1303/1322 (picture ask answered as missing text; "Done" for
a logo the render dropped), KRI-1299 (Done for 4 photos, 0 shown), KRI-1293 (Talking advice on a
Montage edit).

## Failure modes (written before the code)

1. **Wrong capability named.** An ask whose typed facts describe one capability is judged by
   another checker that reads a shared fact (`background_color` on a closing card → speaker
   background). Must not regress the other way: "a plain white background behind me" (no
   `closing` fact) stays a speaker-background ask.
2. **Closing text without a colour** (`closing` only) must keep its current path: the new
   closing-card answer applies only to a closing ask that asks for a coloured screen.
3. **Closing card available** (#2256): the answer comes from #2256's
   `brief_checks._check_closing_card` through the shared provider
   `phone_rollout.phone_talking_closing_card_available()`, never a stand-in. With the card on,
   the draft promises it and the finished video is judged by its `phone_closing_card` receipt;
   no line may call an available card unsupported (`test_available_closing_card_is_never_called_unsupported`,
   and the yorum3 replay with the card switched on).
4. **Turkish**: the same facts give the Turkish twin; the reason is a format limit in both
   languages (prefix table), so the hold/approve flow behaves exactly as the old reason did.
5. **Heads-up says a treatment won't be applied while the route's writer applies it.** The
   line must come from the same predicate the manifest and worker use, per treatment, and stay
   when that predicate is false (flag off, feature not verified).
6. **Only the applied treatment drops**: a treatment the writer really can't do (transitions,
   looks) stays on the heads-up.
7. **Self-contradiction from any future source**: a Done / Will do / Partly line for an ask
   and a heads-up that its capability won't be applied. Caught by a typed check
   (`reply_consistency`), not by wording; the reply keeps the line backed by the capability
   resolver and drops the other.
8. **"Have a look at X" + a notice that X can't happen**: one Partly line carrying the notice.
9. **Review vs render receipt**: "Done" for full-screen pictures with `phone_beat_receipt`
   placing none, or "won't be applied" for a lane the receipt lists as applied.
10. **Flag off** must be byte-identical (guard test), and every new function fail-open: a
    consistency problem never fails a draft or a review.
11. **Length budget**: removing a heads-up line must not leave a dangling "Heads-up" header.
12. **Approval readiness** still marks the closing card `unsupported` (hold + "make it
    without these"), with the right reason.
13. **Route grader golden lines** unchanged with the flag off (the flag is not a prod flag).

## Case matrix

EN/TR × {closing card with colour, closing text only, speaker colour behind me}; Talking
phone route × {overlays lane on, off}; montage route (overlays never advertised); draft,
held draft, simpler version, after-video review. Excluded: web editor (out of Kria scope),
cloud routes (no route table, no heads-up lines).
