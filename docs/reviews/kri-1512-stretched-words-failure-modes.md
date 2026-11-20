# KRI-1512 Cusco DJI T2: the four ~1 s pauses Clean up speech still kept after #2374

Run: prod job `887f89d6`, thread `ca6a8138`, build 2015, server `6d36200c4`, 2026-11-16 (parent
KRI-1502, follow-up to KRI-1507). PR #2374 moved "Kilitli sistemler" back onto its voice, so the
take's 2 s pause is now cut. Four pauses of about 1 s were still kept, each a different stamp
shape. All times are source seconds. Voice-band measure: highpass 300 / lowpass 3400, 50 ms RMS,
speech = 10 dB over the 10th-percentile floor. Voicing: the KRI-1017 detector
(`speech_voice_guard.voiced_runs`). Silence: the analysis's own ambient spans
(`detect_silences_with_status`, threshold -35.9 dB on this take).

## The four pauses

| Pause (voice-band) | Words (whisper-1 stamps) | Silence span | Voicing | Why it was kept |
| --- | --- | --- | --- | --- |
| A 1.35–2.10 | "bahsettik." 0.72–1.38 → "Modern" 2.16 | 1.175–2.061 | 0.955–1.005, **1.615–1.675, 1.72–1.77**, 2.065– | rule 3 proposed 1.505–2.035; the KRI-1017 voice guard dropped it (0.11 s voiced) |
| B 5.55–6.55 | "bunlar?" 5.30–5.64 → "1." 6.20–6.84 | 5.582–6.547 | 4.895–5.545, 6.565–6.715 | stamped gap 0.56 s (< 0.6, rule 3 blind); rule 0's sentence-head carve (KRI-1492) refused: the silence starts 0.282 s after "bunlar?" starts, under 0.3 |
| C 9.25–10.30 | "mütemadiyen" 8.20–9.50 → "sertlikten" 9.50–10.82 | 9.241–10.243 | 8.995–9.265, 10.305– | stamped gap 0; mid-sentence, so no sentence carve; rule 0 refuses edge carves (KRI-969) |
| D 37.40–38.50 | "oluyor." –35.84, "Ve" 37.00–37.60, "4'te" 37.60–38.20 → "İNKA'lar" 38.20 | 35.785–36.839, 37.413–38.486 | 36.825–37.325 ("Ve dörtte"), 38.475– | "4'te" is wholly in the pause, but "Ve" starts on the voice and runs into the pause, so the late-word move (#2374) saw an earlier word reaching into the voice and refused |

Independent read of the words (local whisper.cpp large-v3-turbo on short excerpts, no paid call):
"ne bunlar? bir", "mütemadiyen sertlikten daha güçlü", "ve dörtte İnkalar belki". On the A
stretch alone (1.40–2.06 s, +20 dB) it hears only "Evet", its usual answer to near-silence.

## Decisions

- **B and C: cut.** whisper stretched the head of the word after the pause back over the pause
  ("1." and "sertlikten"), which hid the pause from rule 3. A new preflight step,
  `late_word_realign.realign_stretched_heads`, trims such a head to the silence end, and the
  previous word's stamped end inside the silence back to its start. It needs the same two
  witnesses as #2374: the silence spans and the KRI-1017 voicing detector.
- **D: cut.** The late-word move learns one more shape: a run whose first word starts on the
  wordless voice and runs into the pause (a "straddling" run). The rest of #2374's gates are
  unchanged.
- **A: kept on purpose.** The voice guard is right. 1.45–2.03 s holds a steady periodic sound
  (pitch 103–125 Hz, periodicity 0.6–0.8 in most frames, 3–6 dB over the take's floor) for
  0.55 s. No other pause on the take has it: B, C, D and the 2 s pause sit about 1 dB over the
  floor, mean periodicity about 0.4. It is lower than the speaker's voice (about 140 Hz) and
  may be a hum, a far voice or a machine; neither whisper pass has a word there. The guard
  exists to keep exactly this kind of quiet voiced sound (KRI-1017: a far guide 2–4 dB over the
  plaza), and a rule that let a cut through it would weaken that everywhere.

Switch: `SPEECH_CLEANUP_STRETCHED_WORDS_ENABLED` (default on). Off is byte-identical to #2374.
`DETECTOR_VERSION` `mixed-gap-v11-stretched-words`.

## Failure modes (written before the code; each is pinned)

Stretched heads (B, C), `tests/pipeline/test_late_word_realign.py` (`hN`) and the T2 replay:

1. **A quiet voiced onset under the silence floor** (KRI-969's "quiet onset" attack: the word
   really starts in the "silence"). The whole pause must be voiceless to the voicing detector;
   otherwise nothing changes (`h1`, replay control "voice in the pause").
2. **An unvoiced onset under the floor** (s, ş, f, h, k, t, p, ç), which the voicing detector
   cannot hear. The voice must start within `SENTENCE_TAIL_REACH_S` (0.2 s) of the silence end,
   the silence end already sits 80 ms clear of sound, and rule 3 keeps `KEPT_GAP_S/2` (0.125 s)
   before the trimmed start: an onset consonant of up to about 0.2 s stays. Whispered speech
   longer than that is the residual risk (`h2`).
3. **Sound between the silence and the voice** (a click, a breath, a long unvoiced syllable):
   the voice starts too late after the silence, refused (`h3`).
4. **The remainder is not the word's voice** (no voicing after the silence inside the stamp):
   refused (`h4`, both tests: no voice at all, and voice at the onset filling under 30% of the
   rest; replay control "no voice after the pause").
5. **The pause is already visible to rule 3** (stamped gap of 0.6 s or more): left alone; the
   step only reveals hidden pauses and never lengthens a cut rule 3 already makes (`h5`).
6. **Both edges in one pause** (the previous word overlaps the silence by 0.3 s or more):
   ambiguous, refused, as rule 0's F6 (`h6`).
7. **Another word in the pause, or the previous word wholly inside it**: a different shape
   (late or early run), refused (`h7`).
8. **The remainder cannot hold the word** (under `SENTENCE_HEAD_MIN_VOICED_S`, or faster than
   `SQUEEZE_MAX_SYLLABLES_PER_S`): refused (`h8`).
9. **Several quiet patches in the word** (two long spans overlap it): a mumbled word, refused
   (`h9`).
10. **The word is the first of the take**: the lead-silence trim owns it, refused (`h10`). (The
    check also keeps Python's `words[-1]` from posing as the previous word; mode 7 would refuse
    that too, so removing it changes no outcome.)
11. **Order**: the trimmed start is the silence end, after the previous word's (trimmed) end and
    before the word's own end; retake spans keep indexing the same words (`h11`).
12. **Rule 0 carves again**: after the trim no long span overlaps the word, so the KRI-1492 head
    carve cannot double-trim it (replay: no `trim_sentence_head` at B).
13. **Decoding fails or silence detection is not `ok`**: nothing changes (code path shared with
    the late-word move). A failed decode is remembered, so a hung ffmpeg times out once for the
    three readers, not three times (replay "cannot be decoded").
14. **A second caption pass** stamps the same head into the now-cut pause: a word only partly
    in a removal survives `remap_words`, starting at the cut, so no follow step is needed
    (`h14`).

Straddling late runs (D), `sN`:

15. **The straddling word is really said where it is stamped** and only the next word is late.
    The run's stamps inside the pause must hold no voicing and the island must be voiced, as
    for every late run (`s1`, replay control "voice under the stamps").
16. **The word before the straddler reaches into the island**: refused, the #2374 fm3 check now
    judges the word before the straddler (`s2`).
17. **The straddler starts before the island** (in or before the previous pause): it is not a
    straddler, refused (`s3`).
18. **A word that ends on the island without reaching the pause**: not a straddler; the
    earlier-word refusal of #2374 stands (`s4`). A word whose end only drifts into the pause
    (under `LATE_RUN_EDGE_TOL_S`, the slack #2374 already gives a drifting end) is not one either
    (`s9`).
18a. **The mirror shape** (review of this commit): a word stamped in the pause BEFORE its voice,
    after a word whose end drifts into the pause. Taking that word as a straddler would move the
    late word onto the wrong voice and leave its own voice wordless. Beyond the reach above, the
    word after the run must start by the pause end plus `LATE_RUN_STRADDLE_NEXT_REACH_S`
    (0.2 s), so the voice after the pause is that word's, never the run's (`s8`, `s10`).
19. **Length**: a straddling run is its voice stretched over its pause, so its stamps run longer
    than the island (D: 2.09 times). Straddlers get `LATE_RUN_STRADDLE_LENGTH_RATIO` 2.5; the
    syllable gate still binds and whole-in-pause runs keep 2.0 (`s5`).
20. **A second caption pass** puts the straddler on the voice and the next word in the pause:
    `follow_late_runs` moves a word that starts after the recorded stamps' start and ends inside
    the pause only for a run recorded as a straddle, only when it reaches into the pause by the
    same slack, and only when the pass's next word lies in the pause. The worker also runs the
    follow on the analysis's own words, whose moved run ends exactly at the pause start the
    receipt rounds to 3 decimals: those must not read as straddlers and shrink to nothing. A
    correctly stamped pass is left alone. #2374's runs follow exactly as before (`s6`).
21. **Order**: the run never starts before the word before it ends (`s7`).

Both:

21a. **The voicing read misses voice at a window edge.** The detector band-passes the window it
    reads, so a run within about 0.1 s of the window's edge can go unheard (Çiğdem S2: 0.17 s of
    voicing inside a 0.65 s pause read as none over the pause alone; Çiğdem S4: "göndermek" voiced
    55 ms into the silence span). With the switch on, every read takes the voice guard's 0.1 s of
    context on each side and counts the voicing inside the span (replay control "voice running
    into the pause").
22. **Switch off changes anything**: the replay with `SPEECH_CLEANUP_STRETCHED_WORDS_ENABLED`
    off reproduces #2374's ten cuts exactly. (The `DETECTOR_VERSION` bump and the fingerprint
    entry re-analyze every source on deploy either way, as with every detector change; the
    analysis output with the switch off is #2374's.)
23. **Other takes change**: the 12 recorded word/silence sets in `tests/fixtures` and the Cusco
    T3, Çiğdem S2 and S4 kit runs, replayed on their real audio where the audio exists.
24. **A cut lands in the hum at A** (the guard is what keeps A): replay asserts A is still kept.

## Self-review

An independent reviewer read this commit's diff. Real findings, each fixed with a test written
first: the mirror shape (18a), a second pass or the analysis's own words shrunk by the straddle
follow (20), failure mode 4 and the late step's context read not pinned by any test (`h4`, replay
"voice running into the straddle pause"), and a hung decode timing out three times (13). A
mutation pass then broke each gate in turn: every one fails a test except the first-word guard
(mode 10), which mode 7 duplicates.

## Evidence (offline, no paid calls)

T2 on the real clip, the job's stored words, real silence detection and voice guard (the
re-synthesized take in the tests gives the same plan, cut for cut):

| Pause | Kept with #2374 alone | Kept now | New cut | Voice after the cut |
| --- | --- | --- | --- | --- |
| A 1.35–2.10 | 0.75 s | 0.75 s (on purpose) | none (voice guard) | |
| B 5.55–6.55 | 1.00 s | 0.34 s | 5.707–6.365 | 6.565 |
| C 9.25–10.30 | 1.05 s | 0.30 s | 9.366–10.118 | 10.305 |
| D 37.40–38.50 | 1.10 s | 0.56 s | 37.538–38.075 | 38.475 |

D keeps more because "İNKA'lar" still has 0.29 s of its stamp in the pause, under the 0.3 s
overlap the head trim needs. The cut before "Ve" (35.965–36.70 s, trimmed by the voice guard) now
ends at 36.714 s, 0.11 s before the voice, without the guard. Every other cut is #2374's. No cut
overlaps a voiced run; inside each new cut the voice band is at most 3.3 dB over the take's floor
(speech around it: 30–35 dB). The four list labels stay on the same source moment, and the
captions of "sertlikten" and "Ve" start on their voice.

Other takes:

- The 12 recorded word/silence sets in `tests/fixtures` (Don Julio T1 three passes, Plaza T2 five
  passes and maximo 2, provolone T1/T3, Merope café): neither shape matches, so the audio is
  never read and nothing can change.
- Real-audio replays (job words, kit clip, switch on vs off):
  - Çiğdem S2 (2026-11-15): unchanged. Its "story. So" pause (KRI-1492's own case) matches the
    head shape, but the context read hears voice at both ends of the span and refuses; rule 0's
    sentence-head carve still cuts it as before.
  - Çiğdem S4: one new cut, 51.233–51.782 s ("ve … yaşam"; voice-band gap 51.10–51.90 s, voice
    after it from 51.923 s). Its 2.30–3.32 s span is refused: "göndermek" is voiced 55 ms into it.
  - Cusco T3: two new cuts, 30.609–31.052 s ("taş … işçiliğinden", gap 30.45–31.15 s, the "ş" of
    "taş" fades to the background by 30.56 s) and 38.848–39.357 s ("sizi … Cusco", gap
    38.70–39.55 s, voice from 39.548 s). Both are far from the stickers (16–23 s).
  - Inside each of these three cuts the voice band is at most 4.2 dB over the floor and the
    2.5–8 kHz band sits at the level of the cuts prod already made on the same take. Local
    whisper.cpp on each excerpt hears the expected words around the pause and only its usual
    near-silence hallucinations ("Şş...", "Altyazı M.K.") on the pause alone, boosted 20 dB.

The brief expected no change on T3/S2/S4. S2 is unchanged; T3 and S4 gain cuts of the same hidden
pause shape, each checked silent as above.
