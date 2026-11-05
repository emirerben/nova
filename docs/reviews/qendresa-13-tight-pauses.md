# Tight pauses on phone Talking (Qendresa build list item 13)

Flag: `TIGHT_PAUSE_CUT_ENABLED` (default off; off is byte-identical). Related: KRI-969 (cleanup cut a
real word), KRI-1017 (voice guard), KRI-970/978, KRI-234, KRI-748 (pause receipt).

## The ask and acceptance (written before the code)

A creator who talks at 224-245 words/min with no pause over 0.31 s asks a phone Talking edit to
"cut out all my pauses so it's tight". Acceptance, from her own videos (test kit answer key,
`max_pause_s` 0.35):

- the longest pause inside the talk is at most 0.35 s, measured with
  `ffmpeg -af highpass=f=90,silencedetect=n=-38dB:d=0.12` between the first and last heard word
  (whisper.cpp), ignoring Kria's 1.6 s brand outro;
- every word heard before is still heard after (whisper.cpp, before vs after);
- "Clean up speech" is already selected on the approval card when she asked for it in chat;
- the finished-video receipt reports what was measured, never a bare "Done" with long pauses left;
- nothing cuts without her consent; flag off and older iPhone builds keep today's behaviour.

## Ways it could fail (listed before implementation)

Cutting real speech: quiet word endings/onsets under the -30 dB silence floor; words the
transcript never stamped; squeezed or stretched stamps (KRI-970); ambient "silence" over soft
speech (KRI-234/1017); held vowels; breaths and laughter cut in half. Joins: clicks (the phone
crossfades 25 ms per cut), room-tone level steps, music beds. Picture: micro jump cuts, very short
shots, the phone track's 100-clip limit. Other cuts: consented fillers/retakes/lead-trail trims,
restart cuts (KRI-1189), take window and named cuts (KRI-628/790/821) after it, captions, the
editor's `silence_cut` record (KRI-965), retry determinism. Consent and honesty: running without
consent, false positive/negative asks, pre-selecting on "keep my pauses", a bare Done with long
pauses left, flag-off identity, old clients, measurement failures, multi-clip/cloud/Narrated,
already-tight takes, and the kit's -38 dB measure vs Kria's -30 dB spans.

## Design and why

Real word stamps stretch over pauses (on a real take, 17 of 21 long pauses sat inside one stamp,
usually the next word's start pulled back), so a word-gap rule like today's rule 3 leaves them all.
The cutter therefore works from the silence spans' audio edges, then refuses anything that is not
clearly room tone:

- keep 0.12 s (`PAD_S`) of quiet at each audio edge and around every word's audible core; a fully
  silent stamp is protected whole;
- cut only audio within 15 dB of the take's quiet floor, 0.12 s clear of anything louder, and
  within 10 dB in the 0.2 s before / 0.3 s after a word's own audible core inside its stamp
  (soft onsets and endings; room tone flickers up to about +8 dB);
- then the KRI-1017 voice guard; no shot under 0.6 s next to a new cut; at most 85 keep segments;
  no tight cuts on takes whose floor is above -45 dBFS;
- no tight cuts when the pause measurement fails (`reason: "unmeasured"`): the receipt then says
  nothing was shortened, so the video must match it (review of #2102).

A 12 dB prototype without the floor rule clipped a soft -38 dBFS word ending ("etmek" became "et"
to whisper); the floor rule is what fixed it. It runs at render (like KRI-821 named cuts), on top
of the consented plan, so the snapshot, fingerprints and card numbers are unchanged.

The ask is a recorded brief fact (`pauses: "tight"`, Main Creator v121 / Brief extractor v56), per
KRI-667. The card pre-selects only on a recorded `speech_cleanup: true`.

## Evidence

Numbers: `qendresa-13-tight-pauses-evidence.json`. Harness:
`scripts/ios/qendresa-13-tight-pauses-e2e.py` (real analysis engine with local word stamps, real
worker glue, real phone compiler, iPhone engine on the simulator via
`DeviceMontageRenderE2ETests/testQendresa13*`).

Limits:
- the take is a stand-in (a real Turkish talking take, quiet room), not her footage;
- word stamps come from local faster-whisper, not production whisper-1;
- whisper.cpp proves whole words survive, but it cannot see a partly clipped word (a 0.4 s
  mid-word and a 0.15 s tail clip were both missed). The quiet-floor rule and a listening check
  cover that, and the listening check is owed before rollout;
- a two-voice take kept 9 pauses over the limit because the voice guard kept murmurs in the gaps,
  and its receipt says so.
