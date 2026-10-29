# KRI-1042: Lisbon export text, reviewed observations

Job `3daccc8c-b263-430a-a62f-5821f7b3b647`, variant `guided_story`, attempt
`47a6d68d`. The stored `phone-simple-cut-v3` report rejected the export. Its text
rows failed at 0.45 s and 1.5 s and passed at 2.55 s and at the hidden 3.15 s sample.

The read-only investigation decoded the retained export and the analysis proxies,
then reviewed the verifier's read area (44,964,745,1367) at each sample:

- The title "The energy of / marathon / spectators." is drawn in white Inter Bold
  at 86 px, at full strength from frame 0. Near-white glyph coverage in the glyph
  box is 0.31 at 0.0, 0.45, 1.5 and 2.55 s, and 0.007 at 3.15 s, after the layer ends.
- At 0.45 s and 1.5 s, runners in white shirts stand behind the first letters of
  "marathon". The "m" joins a white shirt that touches the read area's left border.
  The overlay view drops border-touching blobs, so it erases the "m", and no view
  reads the exact copy.
- The renderer drew the text correctly. The failure is a verifier false negative.
- The phone drew no text shadow. That is a separate renderer gap.
- The picture check also failed on this export (min local SSIM 0.889). v4 does not
  change that rule.

The replay results on these crops are in
[kri-1042-text-proof-replay.json](kri-1042-text-proof-replay.json).
