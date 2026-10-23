# KRI-837: one-word quoted start / end line (Don Julio T1)

Offline verification of a bounded local fix. It does not establish fulfillment of the
whole Don Julio T1 request or a physical-iPhone export. Parent KRI-829 keeps those gaps.

## Request and failure

Kit `T1_lucas_full_take_talking` (one 25.733 s take): "Start at my "What's your name?"
and end right after the "Cheers!", drop all the table chatter after it. ... About 15
seconds." On main 004f38182 the end line was never read: `_fact_line` and
`refile_take_trim` required `MIN_QUOTE_TOKENS` (2) words and "Cheers!" is one. The draft
card named only the start, the length line said "about 24.8s and you asked for 15s", the
worker kept 0.8–25.733 s (the Turkish table chatter included) and the rendered receipt
said `met`.

## Fix

- `opening_cut_ask`: the dedicated `start_line` / `end_line` facts accept one word. The
  legacy `start` / `end` keys (a route's places on `order` rows) and the description-cue
  reader keep the two-word floor. `MIN_QUOTE_TOKENS` and `spoken_quotes` are unchanged, so
  quote re-grounding of clip intents, closing-quote receipts and style asks are untouched.
- `take_trim`: a key-less legacy `order` reply on one video re-files a one-word quoted
  line as the trim; with a `key` (clip order / route) the floor stays two words.
- `brief_checks`: a side whose stored line cannot be read (no words in `start_line` /
  `end_line`, one word under a legacy `end`) keeps the receipt unchecked, so the other
  side's cut never certifies the whole ask.

## Evidence (offline replay)

Expectation source: the kit's reviewed transcript timings ("What's your name?"
≈0.95–1.7 s, "Cheers!" ≈13.8–14.8 s, chatter from ≈16 s), encoded word by word in
`src/apps/api/tests/kria/test_kri837_one_word_end_line.py`. The real `take_window_ask`,
`estimate_take_window`, `_phone_opening_cut` (cleanup off and on), `apply_take_window`,
`build_receipts` and `refile_take_trim` run; no planner, parser or verifier is mocked.

| | main 004f38182 | this branch |
| --- | --- | --- |
| Ask | start only | "What's your name?" → "Cheers!" |
| Draft card | "from “What's your name?” (0:01) on, if I find that line" | "from “What's your name?” (0:01) to right after “Cheers!” (0:15), if I find those lines" |
| Length line | "about 24.8s and you asked for 15s" | no untrimmed-take length |
| Worker keep (cleanup off) | 0.8–25.733 s, `opening_cut` only | 0.8–15.05 s, `opening_cut` + `end_cut` applied |
| Rendered receipt | `met` (end never judged) | `met` only with both cuts applied |
| End not found / said only before the start / older render with no `end_cut` / legacy `order` row with a one-word `end` | `met` | `partial` |

The card's end clock is the cut point (15.05 s), so it prints 0:15. The word itself ends at
14.8 s.

Wrong-content controls in the same file: the overlay quotes "LUCAS" / "CHIMICHURRI" and the
title never become cut points; a one-word line is exact, never fuzzy ("Cheese!", "Chairs",
"Sagolun" do not match); a "Cheers!" said before the start line is skipped; keyed one-word
route places stay an `order`; two-word lines keep their timings. The KRI-789 "one-word
quote stays `order`" case moved to the re-filed side, and a keyed one-word pair still stays
`order`.

Mutation: the new file on unmodified main fails 19 of 32 (the 13 guards pass) and passes
32 of 32 on this branch. In the reviewer's scratch run on the first revision, reverting any
one of the three production changes alone turned the tests pinned to it red.

Source-footage check (local FFmpeg, free): the kit take cut to the branch's keep window
0.8–15.05 s is 14.27 s long. Its last frame shows the man in the pastel shirt drinking from
the raised water glass under the DON JULIO / PARRILLA sign, not the table shots that start
≈16.6 s. The last loud speech burst ends ≈14.78 s; 14.8–15.05 s is room tone (−26 to −31
dB), and the next vocal onset (15.1 s) and the table chatter are cut away. The end lands
0.25 s after "Cheers!". Media stays outside git.

## Remaining gaps

- **Transcript language (KRI-843 / KRI-844).** The phone worker matches the lines in the
  whisper words of the speech preflight, with language auto-detected. Auto-detect hears this
  take as Turkish ("Cheers!" as "Sağolun"). Then both lines are `not_found`, the take runs
  whole and the receipt says `partial`, never `met`.
- **Live model shape.** There is no recorded Main Creator reply for this prompt. The tests
  use the `timing` row that prompt v69 teaches, plus the key-less `order` fallback.
- **Speech cleanup.** If the creator accepts the pause-cleanup card, its own cuts shorten
  the video below the 13.5–15.5 s length check.
- **Word timing.** "Cheers!" has two bursts (≈13.85–14.2 s and ≈14.6–14.78 s). A transcript
  that ends the word at the first one cuts ≈14.45 s.
- **Accepted risk.** A one-word filler end line ("yeah") ends at its first occurrence after
  the start line. The draft card shows that time before approval.
- **iPhone export.** Original-footage phone export is not verified. Re-send T1 after deploy.
