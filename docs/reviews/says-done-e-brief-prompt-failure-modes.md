# Says-done group E: brief-writer teaching for Qendresa cases 6, 7, 8 — failure modes

Written before the change (repo rule: list how it can fail, then write it). Scope: the shared
`_BRIEF_PROMPT_SECTION` (Main Creator and brief extractor) and its replay fixtures. No server code
reads the creator's words (KRI-667); the receipts and render already act on the typed facts
(`caption_words`, `caption_box`, `pauses: "tight"`, #2104 / #2102).

## Acceptance

For her exact sentences the brief writer records:

| Sentence (kit) | Facts on ONE requirement |
|---|---|
| Q1 "Captions word by word in the middle of the screen, white, two words at a time" | `captions: "word_by_word"`, `caption_words: 2`, `caption_position: "middle"`, `caption_color: "#FFFFFF"` |
| Q2 "Captions word by word in a light grey box near the bottom" | `captions: "word_by_word"`, `caption_box: "#E5E5E5"`, `caption_position: "bottom"` |
| Q1 "Cut out all my pauses and mistakes so it's tight" | `speech_cleanup: true`, `pauses: "tight"`, `cut_restarts: true` |
| Q2 "Cut all my pauses" | `speech_cleanup: true`, `pauses: "tight"` |

Still wrong if: "word by word" alone sets a count; "cut the (long) pauses" sets `tight`; the caption
look lands on a requirement without the captions; a count other than 1-3 is written.

## Ways it could fail

1. **"Word by word" read as a count.** The model writes `caption_words: 1` for Q1 (word by word =
   one word) or drops the count as contradictory. Mitigation: teach that "word by word" names the
   karaoke style, never a count, and the stated count wins; Q1's sentence is the worked example.
2. **Look split off the captions.** The model (and the hand-authored item-23 golden) files
   "captions word by word" and "in a light grey box near the bottom" as two requirements and the box
   is lost on the way. Render merges looks across requirements (`brief_caption_look`), so a split
   box still draws, but a dropped one does not. Mitigation: teach one captions requirement carrying
   the style and its whole look.
3. **"Cut all my pauses" read as the plain cleanup.** The section lists "remove pauses ... =>
   speech_cleanup" before the tight rule; "all" is the only signal and the tight examples all say
   "tight" or "no pauses at all". Mitigation: name "cut all my pauses" as a tight example and say
   that "all"/"every" pause decides, while "the pauses" / "the long pauses" does not.
4. **Over-reach to plain asks.** The new tight wording makes "cut the pauses" or "cut my long
   pauses" tight, a behaviour change for every creator (`TIGHT_PAUSE_CUT_ENABLED` is on in prod).
   Mitigation: the plain counter-examples are in the same sentence; a decoy replay fixture pins
   "cut the long pauses" + "word by word captions" to no `pauses`, no `caption_words`, no box.
5. **"Mistakes" mis-filed.** Unnamed "mistakes" copied into `cut_lines` (a named cut the render
   can't find: a "can't confirm" or false "Done"), or silently dropped (cleanup "met" while her
   restarts stay). Mitigation: unnamed mistakes join the KRI-1189 unquoted-restart rule
   (`cut_restarts: true`, no `cut_lines`), whose receipt is honest (partial until the render finds
   one). A named or quoted mistake stays a cut line.
6. **Turkish / other language drift.** The teaching is English-led; a Turkish "bütün
   duraklamaları kes" must stay tight. The existing Turkish example stays; facts keys stay English.
7. **Replay proves nothing about the model.** The cassette ignores the prompt; hand-authored
   goldens prove only that the answer shape parses and that the check rejects a wrong answer.
   Mitigation: `prompt_contains` pins the teaching in the rendered prompt, a wrong-content control
   (the item-23 golden's split/dropped facts) must fail the new check, and the paid live run
   (written, not run; needs Yasin's OK) is the only measure of the model.
8. **Version / pin collisions.** Other open work bumps the same version. Mitigation: v137 / v69
   reserved in the shared notes; the brief-on prompt hashes are re-pinned, brief-off unchanged.
9. **Prompt length.** Added words push a long prompt past a limit. Mitigation: a few lines only;
   the section's object cap is unchanged.

## Not fixable here

A brief written without the fact (an older thread, or a model that still omits it) cannot be judged
against her count/box/"all" without reading her words (KRI-667). The receipts can only describe
what the video really holds (see the group E report).
