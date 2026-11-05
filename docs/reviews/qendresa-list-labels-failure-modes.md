# Numbered list labels on a phone Talking edit (Qendresa item 19): failure modes

Written 2026-11-05, before the code. Flag `SPOKEN_LIST_LABELS_ENABLED` (default off).
Related: KRI-972, KRI-747 (no ticket of its own).

## The ask
- **Q2 (screen walkthrough, her V5):** "Add a label when each one starts: "1. Spec-kit",
  "2. Impeccable", "3. TradingAgents", "4. Agents", "5. Claude code game studio"." Each label
  shows when that repo's part of the talk begins. The approved strategy already has a
  `background_media.sequence` with one entry per recording and the words that name it
  (`spoken_phrase`); `phone_background_sequence.lay_out` finds where each one is said.
- **Q1 (talking head, her V7):** "When I list the four things, show each one as big text, like
  "1. which company it sends data to?"". Only the first item is typed; the other three are in her
  speech. Needs a list finder over the take's timed words (second part, after Q2).

Today: a text requirement with no spoken trigger is not placed at all ("That exact text isn't in
this draft"); KRI-707 places a text only on a trigger word, for a fixed 2.0 s, at its first
hearing.

## Her measured look (kit `_src/measure/ocr`)
- V5: one label at a time, ~2.5-3 s each, appearing within ~0.7 s of the repo's name; bottom of
  the frame (y ~0.82-0.89), centred.
- V7: one item at a time, each replaced by the next as she says it (items ~1.2 s apart).

## Ways it can fail (Q2)
1. **Wrong hearing.** The label shows the first time the name is ever said: "agents" inside
   "trading agents" (before the fourth repo), or a name said in the intro ("five github
   repositories ... spec kit").
2. **Wrong clock.** Windows are laid out on the kept take; text rows play on the cut timeline and
   are warped again for inserts (KRI-1151). A label timed on the source clock lands late after a
   speech cut.
3. **Lead-in.** The first recording's window opens at 0 s (lay_out's lead-in rule), so "1. Spec-kit"
   would show over the opening title instead of when she starts Spec-kit.
4. **No pairing.** Her label spelling differs from the entry's `spoken_phrase` ("TradingAgents" /
   "trading agents", "Claude code game studio" / "claude code game studios", "4. Agents" /
   "number four agents"): no label.
5. **Wrong pairing.** "4. Agents" pairs with the "trading agents" recording (a substring), so two
   labels show at once or label 4 shows at repo 3.
6. **Unanchored recording.** A recording whose phrase was never heard, or a rotation window (not
   on a word), gets a label at an arbitrary time. It must place nothing and say so.
7. **Overlap / flicker.** A label held past the next one's start stacks two labels; a label shown
   for a fraction of a second flickers.
8. **Her face.** She is drawn small in the corner (`speaker_frame`); the title placer samples the
   full-frame face, so a label it calls "clear" can sit on her corner face.
9. **Captions and other text.** A label over the caption line (item 12's grey box sits at
   y 0.76-0.82 when on), the title, the closing text or another label.
10. **Too wide.** "5. Claude code game studio" at title size runs off the canvas.
11. **Swept-in texts.** The title ("5 PROJECTS / ..."), the closing "comment REPO" or any other text
    without a trigger is placed as a label at some recording's start.
12. **Double row.** A label that also has a spoken trigger (KRI-707) is drawn twice.
13. **Receipts.** The draft still says "That exact text isn't in this draft", or says done before
    the render; after the render it says done for a label that was not placed, or "isn't in this
    draft" for one that is.
14. **Flag off changes anything** (rows, receipts, recipe, variant fields, windows).
15. **Phone gate closed.** The background (or its windows) is stripped for an old app build, so
    there are no recordings to follow. Labels must not claim recordings that are not there.
16. **Old app.** Labels are ordinary text rows (KRI-467, `authoredText`); they must not need a new
    phone capability or protocol.
17. **Editor.** A label row the editor cannot list or save, or a Save that moves it.
18. **Take end.** A label for a repo named in the last second runs past the end or is a sliver.
19. **More labels than recordings / two labels for one recording.**
20. **Number formats.** "1.", "01:", "1)" and a label that is only a number. Not markers: "#1 PICK"
    (a badge), "3.5 million views", "10:30 morning routine", "1.5x speed hack" (a marker's
    punctuation must be followed by a space).

## Ways it can fail (Q1, list finder)
21. **No list found** in a take that lists the items fast with no pauses (her V7: four items in
    ~5 s).
22. **False list** in a take with no list (most talking videos): labels appear over random speech.
23. **Wrong boundaries.** An item cut mid-phrase or merged with the next ("what it saves on my
    machine what it reads from").
24. **Wrong count.** She says "four things" but the finder makes three or five.
25. **Item text.** The label shows words she did not say, or drops the number.
26. **Cards and captions.** Q1 cards (item 15) and the caption line (item 12, y ~0.62) share the
    screen; a label must avoid both and her full-frame face.
28. **Spoken list draws wrong text / does not stop** ("the first thing is ... the second thing
    is ...", or real items followed by "which is why ..."). Fixed by token rules only: the opener
    comes immediately after item 1 (or after one 1-3 letter joiner), items are
    `max(2, n1 // 2)`..`2 * n1 + 2` tokens, a 0.6 s pause inside an item ends the list, an opener
    led by a new short joiner is the last. Honest limit: "this matters because this ..." right
    after item 1 repeats an opener and is still read as items.
29. **Pairing steals an entry.** "3. TradingAgents" takes the "agents" recording before
    "4. Agents" can (brief order). Pairing is global by tier against all phrased entries.
30. **Only item 1 found, or an item on a face, reported "met".** `list_found == 1` and any item row
    with `face_collision` make the requirement `partial`.
31. **Recordings without item 17.** `lay_out` anchors "agents" inside "trading agents"; labels
    follow recordings only with item 17 on and only those still shown in the final lanes.
27. **English-only guesses.** A list finder built on English words breaks Turkish takes and adds
    intent regexes (KRI-667 ratchet).

## Checks
- Kit rule `labels` in `~/Downloads/kria-qendresa-test/tools/score.py` (each label on screen within
  1.5 s of its item being said, in list order, staying >= 1.5 s or until the next label replaces it).
- Server: worker-level tests (`_run_generative_job` with a fake pool), flag-off byte-identical guard.
- Device: simulator render through the production exporter, scored FAIL (flag off) then PASS
  (flag on) with no per-item trigger words in the prompt.
