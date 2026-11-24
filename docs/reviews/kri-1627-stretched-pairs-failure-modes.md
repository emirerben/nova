# KRI-1627 Cusco DJI T3: the 1.65 s pause both neighbouring words were stretched over

Run: prod job `a4f7b702`, chat "Sticker 1", build 2015, server `6d36200c4`, 2026-11-16 (parent
KRI-1509; recurrence comment on KRI-1507). Clean up speech cut ten pauses but kept a 1.65 s
mid-sentence silence between "…Ant Dağları'nın" and "bayırlarında" (source 42.00–43.65 s). On
today's main (detector `mixed-gap-v11-stretched-words`, after KRI-1512) it is still kept.

## The shape

All times are source seconds, from `tests/fixtures/kri1627_cusco_t3/pause.json`.

| What | Times |
| --- | --- |
| Voice band (300–3400 Hz, 50 ms RMS, speech = 10 dB over the 10th-percentile floor) | speech to 42.00, non-speech 42.00–43.65, speech from 43.65 |
| The analysis's own ambient silence span | 42.028–43.617 |
| whisper-1 "Dağları'nın" | 41.46–**42.76** (0.73 s of it in the silence) |
| whisper-1 "bayırlarında..." | **42.76**–44.50 (0.86 s of it in the silence) |

whisper stretched the TAIL of the word before the pause and the HEAD of the word after it into
the pause; the two stamps meet in its middle, so the stamped gap is 0 and rule 3 (cut a gap of
`MAX_PAUSE_S` or more) never sees it. KRI-1512's stretched-head trim matches "bayırlarında"
but refuses on its failure mode 6: "both edges in one pause: ambiguous, as rule 0's F6".

Rule 0's F6 is ambiguous for rule 0 because rule 0 has only silencedetect: two stamps in one
quiet span could be two quiet word edges. The head trim already has a second witness, the
KRI-1017 voicing detector on decoded PCM. With it, the pair is not ambiguous: the whole span is
voiceless, the previous word's voice ends right before it and the next word's voice starts right
after it. The change: when that holds, trim both stamps to the silence edges.

Switch: `SPEECH_CLEANUP_STRETCHED_PAIRS_ENABLED` (default on; needs
`SPEECH_CLEANUP_STRETCHED_WORDS_ENABLED`). Off is byte-identical to KRI-1512. `DETECTOR_VERSION`
`mixed-gap-v12-stretched-pairs`.

## Failure modes (written before the code; each is pinned)

Unit guards `pN` in `tests/pipeline/test_late_word_realign.py` (the T3 pair with one condition
broken); end to end in `tests/tasks/test_kri1627_cusco_t3_pause_replay.py`.

1. **Quiet voiced speech in the pause** (KRI-969: a quiet tail or onset under the silence floor,
   or KRI-1512's hum A). The whole span must be voiceless to the voicing detector, as for a head
   trim; otherwise nothing changes (`p1`, replay control "voice in the pause").
2. **An unvoiced tail under the floor** (a final s, ş, t, k, f, h, ç fading out), which the
   voicing detector cannot hear. The previous word's voice must END within
   `SENTENCE_TAIL_REACH_S` (0.2 s) before the silence start; the silence start already sits 80 ms
   clear of sound and rule 3 keeps `KEPT_GAP_S/2` (0.125 s) after the trimmed end, so a tail
   consonant of up to about 0.2 s stays. A longer whispered tail is the residual risk (`p2`).
3. **Sound between the previous voice and the silence** (a breath, a click, a long unvoiced
   syllable): the voice ends too early before the silence, refused (`p3`).
4. **The previous word's stamp before the silence is not its voice** (whisper stamped it mostly
   in the pause; its real voice is elsewhere or it was not said): the part before the silence
   must be voiced for at least `LATE_RUN_MIN_VOICED_FRAC` of its length, refused otherwise
   (`p4`, replay control "no voice before the pause").
5. **The part before the silence cannot hold the previous word** (under
   `SENTENCE_TAIL_MIN_VOICED_S`, or faster than `SQUEEZE_MAX_SYLLABLES_PER_S`): refused (`p5`).
6. **Several quiet patches in the previous word** (a second long span overlaps it): a mumbled
   word, refused (`p6`).
7. **Overlapping stamps** (the previous word ends after the next word starts): not two stretched
   edges, refused (`p7`).
8. **The next word's side fails a head gate** (voice in the rest of its stamp, its voice
   starting late, its remainder too short): every KRI-1512 head gate still applies, so the pair
   is refused as a whole and neither word moves (`p8`).
9. **Switch off** (`SPEECH_CLEANUP_STRETCHED_PAIRS_ENABLED=false`, or the stretched-words
   switch off): exactly KRI-1512's behaviour, `h6` still refuses (`p9`, replay control "switch
   off keeps the pause").
10. **A small tail overlap** (the previous word reaches into the silence by under
    `TOKEN_SILENCE_MIN_OVERLAP_S`) is KRI-1512's plain head trim, unchanged, and its receipt
    stays byte-identical (no `tail` key) (`p10`).
11. **Order**: the previous word ends at the silence start, the next word starts at the silence
    end; both keep their other edge; every other word keeps its times (`p11`).
12. **Rule 0 carves again**: after the trim neither word overlaps a long span, so the sentence
    carves cannot double-trim them (replay: no `trim_sentence_*` adjustment at the pause).
13. **The voice guard** still checks the new cut (KRI-1017): the cut planner runs unchanged on
    the trimmed words; the replay asserts no cut overlaps a voiced run.
14. **A second caption pass** stamps the old tail into the now-cut pause: a word only partly in
    a removal survives `remap_words`, ending at the cut (`p14`).
15. **Other takes change**: the recorded word/silence sets in `tests/fixtures` and the real-audio
    kit takes (Cusco T2, T3, Çiğdem S2, S4 where the audio exists), switch on vs off. Every new
    cut is listed below and checked silent.
16. **The stickers move** (T3's whole point): the trim moves only the end of "Dağları'nın" and
    the start of "bayırlarında"; the stickers are pinned to "yılanla", "puma" and "kondor"
    (12–17 s, untouched). The replay asserts their words keep their times.

## Seen, not changed

- A lone stretched TAIL (the word before a pause stretched over it, the word after it starting
  at the silence end): T3 "İnkalar'ın" 33.68–34.90 over the span 34.215–34.907 (voice-band gap
  34.15–34.90, 0.75 s). It is the mirror of KRI-1512's head trim with no head; it would cut about
  0.5 s. Not this ticket's finding; left for a follow-up so this change stays one shape.

## Evidence (offline, no paid calls)

Real-audio replays: the kit clip's 16 kHz mono audio, the job's stored words, real ambient silence
detection, real voicing detector and voice guard, `SPEECH_CLEANUP_STRETCHED_PAIRS_ENABLED` off vs
on (stretched words on). "Over floor" is the loudest 50 ms voice-band frame inside the new cut over
the take's 10th-percentile floor; speech on these takes sits 25–40 dB over it.

| Take | Cuts off → on | New cut | Over floor | Words moved | Local whisper.cpp on the cut (+20 dB) |
| --- | --- | --- | --- | --- | --- |
| Cusco T3 (this ticket) | 12 → 13 | 42.153–43.492 | 0.8 dB | "Dağları'nın" end 42.76 → 42.028, "bayırlarında" start 42.76 → 43.617 | "Altyazı M.K." |
| Çiğdem S4 | 11 → 13 | 32.002–33.385 ("yılda … bu") | 4.2 dB | "yılda" end 32.70 → 31.877, "bu" start 32.70 → 33.51 | "Altyazı M.K." |
| | | 67.382–68.141 ("burayı … buradan") | 3.4 dB | "burayı" end 67.80 → 67.257, "buradan" start 67.80 → 68.266 | "Altyazı M.K." |
| Cusco T2 | 13 → 13 | none | | none | |
| Çiğdem S2 (2026-11-15) | 4 → 4 | none | | none | |
| Don Julio T1, all three passes (tr, es, en), real and recorded silences | unchanged | none | | none | |

"Altyazı M.K." is whisper.cpp's usual answer to near-silence; on the context around each cut it
hears the expected words ("enfes ant dağlarının bayrılarında", "son 20 yılda bu devlet
boşaltmıştır", "Buradan çıkmak zorundayız"). Both S4 stretches are at the floor for 1.6 s and 0.9 s
between speech 30–40 dB over it.

Recorded word/silence sets in `tests/fixtures` (19 scanned): besides T3's own, only Don Julio T1's
forced-English pass matches the pair geometry ("it's … good." over table chatter); the voicing
detector hears voice there and refuses, as the replay above shows.

Every new gate was broken in turn (mutation pass): each one fails a `p*` test.

T3 in the test suite (`test_kri1627_cusco_t3_pause_replay.py`, re-synthesized take): with both word
fixes off the analysis reproduces prod's ten cuts exactly; with the pair trim on it adds one cut
inside 42.00–43.65 s and keeps 0.31 s of the pause; no cut touches a voiced run; the sticker words
("yılanla", "puma", "kondor") keep their times; only the two pair words move.
