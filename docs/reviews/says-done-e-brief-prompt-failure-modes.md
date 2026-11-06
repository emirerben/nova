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

## Case 10 (added on request of the says-done orchestrator): a title / ending hold read as the video's length

Her Q1 says `Start with a big title "lets build an agent that reads an APP before I trust it!!" for
the first 3 seconds` and `End with "comment READ" and an arrow for the last 3 seconds`. Filed as
`timing`/`global` `{"duration_s": 3}`, either one has the typed shape of a whole-video length ask
(`length_policy.creator_duration`), and code may not read the words to tell them apart (KRI-667).
Group B (346d640be) makes a `title`-scoped hold one moment; a `global` one stays a length.

Acceptance: for her two sentences the brief holds the title as a `text`/`title` requirement and the
ending as a `text`/`global` requirement with the seconds only in their descriptions, and NO
`timing` requirement with `duration_s` (`creator_duration(brief)` is None). Control: "... for the
first 3 seconds and make the video 30 seconds long" keeps the title AND a `timing`/`global`
`{"duration_s": 30}` (`creator_duration` = 30).

Ways it could fail:
10a. **The length rule wins.** "ALWAYS add a timing requirement ... any number of seconds" has no
     exception, so the model still files `duration_s: 3`. Mitigation: an explicit exception with
     her two sentences as worked examples, placed right after the length rule.
10b. **Over-correction.** The model stops recording real lengths when a message also has a timed
     title ("title for 3 seconds, make it 30 seconds"). Mitigation: the example says a length of the
     whole video still records `duration_s`; the control fixture pins both in one message.
10c. **The hold moves to a scope the server reads as a length.** `timing`/`title` `duration_s` is a
     length on main until group B merges. Mitigation: teach no `timing` row at all for a one-moment
     hold; the strategy (`opening_title_duration_s`, `pinned_texts` `end_s`) carries the seconds.
10d. **Clip holds caught by the new rule.** "show this clip for 2 seconds" is a clip hold
     (`timing`/`clip:<id>`, KRI-824), not a title. Mitigation: the rule names only an opening title
     and the ending words.
10e. **Ending filed without a typed ending fact.** (Corrected after Emir's review of #2131 on
     main 6fa6105a1.) This change first dropped `closing` from the ending text, reading that fact
     as "end on a shot". After #2129, `brief_checks._asks_whole_video_text` judges a `text`/
     `global` line with no fact beyond its look as a line for the WHOLE video, so "End with THANK
     YOU for the last 2 seconds" with `closing_title` shown 9-11 s of 11 s was reported "on screen
     for 2 s of the 11 s video". Mitigation: the ending words carry `{"closing": "<the words>"}`
     (the contract #2129's own ending control uses); on a `text` requirement `closing` does not
     start the closing-photo check (`_wants_closing` reads style/audio/select only). Regression:
     `tests/kria/test_says_done_e_ending_receipt.py` (real planner + compile + final review), with
     the same words without the fact as the contract control and a genuine whole-video line as
     the preserved negative control.
10f. **Kit test stays red.** `test_q1_title_hold_length` builds the `timing`/`global` rows itself;
     a prompt cannot turn it green. That is expected: it pins what code does with a wrong brief.
