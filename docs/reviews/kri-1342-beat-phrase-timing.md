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
- Bounds: full-screen 1.5–3 s on Talking (it covers the speaker), 2–4 s on Voiceover; a
  corner photo 2–5.5 s (Qendresa: a screenshot stays through the explanation after a name).
  When no whole sentence fits, the window ends on the last comma/pause that does.
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
17. (Found in review, #2257) A short fixed-length visual (a 1.2 s video, a photo-then-clip,
    a short `hold_s`) moved back to the phrase start ends before its own word is said. Every
    window keeps its length but starts late enough to stay on screen through its cue
    (`phrase_window._covering_cue`); a phrase-sized window clamped to its maximum likewise.
18. (Found in review, #2257) The same for a Talking own-footage cutaway: a 1 s clip moved to
    the phrase start ("Then we walked into the Maltepe stadium") played 4.9–5.9 s and was gone
    before "Maltepe stadium" (6.2 s). `schedule_mention_cutaways` now starts it only as far
    back as its real display length (footage, capped) still covers the mention; a window
    then trimmed by the next mention or with no room falls back to the word start (flag-off
    placement). Two clips named in one phrase: the later one never starts before the earlier
    one's words end. Tests: `test_short_footage_stays_on_screen_through_its_mention`,
    `test_two_clips_named_in_one_phrase_each_stay_on_their_own_mention`.

## Pass metric

On every placed word-cued visual of the old-kit corpus, before vs after: `start - phrase_start`
within 0.3 s, and the visual does not end inside a sentence that started after its phrase.

## Result (offline, flag on vs off, commit on `codex/beat-timing`)

Evidence level: **offline replay** of the real grounding (`ground_phone_reaction_beats`) on the
stored transcript words of 13 prod jobs (2026-09-24 .. 2026-11-09; DB seams stubbed, no paid
calls). No native/device export yet: phone Talking renders on the iPhone.

Corpus: 88 placed visuals. 79 were cued by a word the creator named ("when I say X", quoted
words, "when I say their name") — football ×3, best-food, cabin T2 (TR), Kadıköy T3 ×2 (TR),
N3 marathon ×2, Don Julio T2. 9 were cued by meaning ("when I mention / talk about"): yorum3 ×4
(full screen) and Qendresa Q1 run 2 ×5 (screenshots in the top/bottom band).

| | before | after |
| --- | --- | --- |
| meaning-cued: starts ≤ 0.3 s from phrase start AND does not cut away inside the next sentence | 0 / 9 | 7 / 9 |
| meaning-cued: starts ≤ 0.3 s from phrase start | 6 / 9 | 9 / 9 |
| creator-named word visuals changed | — | 0 / 79 |

yorum3 (source seconds): launch 3.18→2.06 (phrase "Ten minutes after…"), post1 9.82–11.32 →
9.72–12.54 (holds through the quoted post), post2 13.72–15.22 → 13.62–16.16, billboards
26.76–28.26 → 25.00–27.24 (gone before "So, how do I read this"). The two Qendresa misses
("Impeccable", "Cloud Code Game Studios") have a ≈5.7 s explanation sentence after the
name: the corner shot now ends on a comma inside it (18.88 "taste,", 41.94 "leads,") instead
of mid-phrase, under the 5.5 s corner maximum.

Independent check: whisper.cpp large-v3-turbo on the yorum3 output puts "like car brands
fighting on billboards." at 17.36–19.6 s and the photo (ffmpeg frames, 0.25 s) at ≈19.25–20.0 s,
over "so how do I read this" — the incident as described; the prod words give the same 1.7 s
gap between "Like" and "billboards" that the fix closes.

Negative control: `test_negative_control_word_timing_fails_the_phrase_check` runs the same pass
check on the flag-off windows and it rejects all four yorum3 photos.

Gaps: no iPhone export with the flag on (owner: KRI-1342); cloud lanes have no word-cued
visuals in the corpus; old jobs predate brief `facts.triggers`, so "named" was read by hand
from their request text. Named-word visuals keep their fixed holds, some of which still run
into the next sentence (food, football) — the creator chose the word, not the length; a
follow-up could size those holds too.

Artifacts: hand-reviewed phrase starts `kri-1342-beat-phrase-answer-key.json`; per-visual before/after
`kri-1342-beat-phrase-replay.json` (no transcripts, times only).
