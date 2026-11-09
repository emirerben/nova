# KRI-1342 item 8 — a word-cued visual starts with the phrase that names it

Incident: yorum3 (2026-11-09, job `7eba9903`, thread `d9d7ad3d`, Fly `4f6ed3014`). The creator
asked "When I mention the launch, Vela's 2 posts and the billboards, show that picture full
screen". The planner picked one trigger word per photo; the phone grounding started each photo
ON that word and held it 1.5 s. "billboards" is the last word of "Like car brands fighting on
billboards." — the photo appeared as the phrase ended (output 19.25 s, the caption already read
"billboards.") and covered "so, how do I read this" after it.

## Acceptance (written before code)

- A visual cued by the *meaning* of the talk (no creator-named spoken word) starts within 0.3 s
  of the first word of the phrase/clause that contains its trigger (a small lead into silence
  only, never over the previous word).
- Its default hold is sized to that phrase: it ends at a clause or sentence end, between the
  lane's min and max, and never ends inside the next sentence. If the phrase is too short, it
  grows by whole following sentences that fit; only when none fits does it fall back to the
  minimum hold.
- Unchanged: a creator-named spoken word ("when I say X", brief `facts.triggers`), an `after`
  cue, a sticker, a sound effect (stays on its word), an explicit `hold_s` length, timed cards
  ("at 0:12"), the closing shot, a photo-then-clip sequence's length, and every render with the
  flag off (byte-identical).
- EN and TR, Talking and Voiceover/Narrated phone lanes (one shared grounding).

## Failure modes (listed before implementation)

1. Words with no punctuation (raw whisper) — only pauses mark phrases; a run-on clause could pull
   the start far back. Bound the look-back (2.5 s); past it, keep the trigger start.
2. Trigger already first in its phrase — must not move (no regression).
3. A multi-word trigger spanning a comma ("Welcome, Lumo") — boundaries inside the hit ignored.
4. A truncated / restarted word ("bill...") or a removed retake — treat "..." and long pauses as
   boundaries so the start never walks back across a restart; a start inside a later cut is
   clamped by the existing `remap_range`.
5. Two beats in one phrase, or "every" occurrences close together — both would take the same
   phrase start and the overlap rule would drop one. A beat never starts before the previous
   same-lane beat's own trigger word ends.
6. The lead covers the tail of the previous word — the lead only uses silence.
7. A short phrase grown to the minimum hold ends mid-way through the next sentence — grow by
   whole sentences only; fall back to the minimum only when no whole sentence fits the max.
8. A clause longer than the max hold — clamp to the max at a word end.
9. Explicit timings moved by mistake — see "Unchanged" above.
10. Voiceover photo-then-clip (`following_clip_id`) requires window length == hold
    (`narrated_photo_sequence`) — shift the start only, keep the length.
11. Video beats — keep the video length rules; shift the start only.
12. Turkish: suffixes, verb-final clauses — same punctuation/pause rules, same look-back bound.
13. Clamp at 0 and at the clip's duration.
14. Flag off must be byte-identical (receipts too).
15. Whisper word ends stretched over pauses — punctuation first, pauses second.
16. Hallucinated mid-phrase commas — start lands later than ideal but never after the trigger.

## Pass metric

On every placed word-cued visual of the old-kit corpus, before vs after: `start - phrase_start`
within 0.3 s, and the visual does not end inside a sentence that started after its phrase.
