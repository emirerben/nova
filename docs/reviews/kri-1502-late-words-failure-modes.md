# KRI-1502 Cusco DJI T2: words stamped into a pause (KRI-1506, KRI-1507, KRI-1508)

Run: prod job `887f89d6`, thread `ca6a8138`, build 2015, server `6d36200c4`, 2026-11-16.
One Turkish talking clip (51.9 s), four numbered labels, Turkish captions, Clean up speech.

## What happened

| Ticket | What the creator saw | Root cause |
| --- | --- | --- |
| KRI-1506 | Label "2. Kilitli sistemler" and its caption 1.6 s after the words | whisper-1 stamped "Kilitli sistemler" at 13.96–15.24 s, inside the 1.9 s pause after the words. Their voice (12.31–13.38 s) had no word. The label and the caption follow the stored words. |
| KRI-1507 | Cleanup kept the take's longest pause (2.05 s) and cut shorter ones | The same stamps hid the pause from the cut: it looked like speech. Rule 2 even proposed the real words as an acoustic filler; only the KRI-1017 voice guard kept them. |
| KRI-1508 | The draft opened "Not everything you asked for made it in:" and showed "Partly: Turkish captions." | Approval readiness takes the phone Talking captions plan as a render promise (`talking_caption_promise`, KRI-1405). The reply's own check did not, so the captions row counted as a shortfall. |

Independent truth for T2 (neither whisper-1 nor the -30 dB silence spans): voice-band level of
the source puts "kilitli sistemler" at 12.30–13.40 s and the pause at 13.40–15.40 s; local
whisper.cpp on 11.30–13.50 s hears "Kilitli sistemler".

## The fix

- `app/pipeline/late_word_realign.py`: before planning, the cleanup analysis moves a run of
  words stamped wholly into a pause back onto the wordless voice right before it. It needs the
  silence spans and the KRI-1017 voicing detector to agree. Then the cut, and the stored words
  that the captions and list labels follow, are on time. `DETECTOR_VERSION`
  `mixed-gap-v10-late-words`. Kill switch: `SPEECH_CLEANUP_LATE_WORDS_ENABLED`.
- `brief_checks.render_promise_ids` + `planned_ids`: the draft reply (and "Make it without
  these") treats the Talking captions plan the way readiness does: "Will do at render", never a
  miss.

## Failure modes (written before the tests; each is pinned)

Late-word move (`tests/pipeline/test_late_word_realign.py`, replay in
`tests/tasks/test_kri1502_cusco_t2_replay.py`):

1. **Quiet speech the silence spans missed** is under the stamps; the move would make it a
   cuttable pause. Refused when the stamps hold voicing (`fm1`, replay control "voice under the
   stamps").
2. **The sound before the pause is not a voice** (a cough, a door, music). Refused when the
   island is not voiced (`fm2`, replay control "no voice in the gap").
3. **An earlier word already owns the voice** (its stamp reaches into it). Refused (`fm3`,
   replay control "a word already on the voice").
4. **The next word is also stamped in the pause**, so the whole stretch may be skewed another
   way. Refused (`fm4`).
5. **The words in the pause are not one run** (a word between them runs past the pause).
   Refused (`fm5`).
6. **The island cannot hold the run** (too short, too long against the stamps). Refused (`fm6`).
7. **Too many syllables for the island.** Refused above `SQUEEZE_MAX_SYLLABLES_PER_S` (`fm7`).
8. **Zero-length stamps** carry no length to map. Refused (`fm8`).
9. **The move reorders words** and breaks retake spans (they index words). The run never starts
   before the word before it ends (`fm9`).
10. **Decoding fails** or silence detection is not `ok`: nothing moves (code path:
    `_late_words_realigned`).
11. **The switch off changes anything**: the replay's switch-off control reproduces prod's nine
    cuts and label time exactly.
12. **Other takes change**: no move on 12 recorded word/silence sets in `tests/fixtures`, nor on
    the Cusco T3, Çiğdem S2 and S4 kit runs replayed on their real audio (offline, below).

Draft reply (`tests/kria/test_kri1508_draft_promises_reply.py`):

1. the captions promise still counts as a miss (the replay of event #7);
2. a captions ask Nova can't fully do (German with translation off, spellings still to write)
   becomes a promise: it stays "Partly" and keeps the opening line;
3. a real shortfall next to the promises no longer opens "Not everything ...";
4. the reply and readiness disagree about which rows are promises;
5. a Turkish or German chat still opens with the shortfall line;
6. the lead counts a promise as "Partly done".

## Evidence (offline, no paid calls)

Real T2 clip through the real analysis (words = the snapshot's stored words):

| | Before (prod, replayed exactly) | After |
| --- | --- | --- |
| "Kilitli" starts (source) | 13.96 s | 12.31 s (voice: 12.30 s) |
| Cut across the 2 s pause | none | 13.509–15.115 s |
| Kept of the 2.0 s pause | 2.00 s | 0.39 s |
| Other cuts | 9 | the same 8, plus the one before "Kilitli" ending at the word |
| Rule 2 filler candidate on the real words | yes (12.31–13.38 s) | none |

Worker (phone Talking job on that snapshot): label 2 at 12.86 s before (prod's), 11.27 s after;
the output's audio has "kilitli" at about 11.25 s. Labels 1, 3 and 4 stay on the same source
moment.

Other kit runs replayed on their real audio with the switch on and off: Cusco T3, Çiğdem S2
(2026-11-15), Çiğdem S4: identical plans, no move.

## What this does not fix

Pauses of about 1 s are still kept in T2 (voice-band, after the fix): 5.55–6.55 s ("Ne bunlar?"
→ "bir"), 9.25–10.30 s (inside "mütemadiyen … sertlikten"), 37.40–38.50 s ("Ve dörtte" →
"İnkalar"), 1.35–2.10 s (dropped by the voice guard: 0.11 s of voicing). Each is a different
stamp shape (a head stretched across a mid-sentence pause, a phrase stretched over one, a
sentence-head carve missed by 18 ms), not this one. `SPEECH_CLEANUP_LONG_PAUSES_ENABLED` (off in
prod) would cut only the 9.25–10.30 s one on top of this fix. They are a follow-up.

The "Done" receipt for a label still does not check the label's time against the audio
(KRI-1506's third expectation); with the words on their voice the T2 receipt is now true.
