# "No captions" steals her text's place and colour — failure modes (written before the code)

Case 3 of the Qendresa "says done but isn't" set (`~/Downloads/kria-qendresa-test/handoff/says_done_fixes.md`,
group D). Her Q3 message:

> Just put this text in the middle for the whole video: "The prompt I run at the end of every Claude
> session". Small white text like TikTok's, no captions and nothing else. Keep the whole clip, don't
> cut it. Mute my sound, I'll add a song on TikTok.

The phone text-style pass reads unambiguous style words from the job's request
(`schemas.phone_text_style.explicit_text_rule`, read by `services.phone_text_styling.requested_style` at
dispatch). Its role scan looks for a role word anywhere in the message; "captions" (in "no captions") is
the only one, so "in the middle" and "white" are filed under the CAPTION role, and `requested_style` also
strips the strategy's own white `text_color` from `all_text`. Her one line (a pinned row) gets neither and
stays at the top (y 0.15).

Observable acceptance: with her exact message, `requested_style` files `{"position": "middle", "color":
"#FFFFFF"}` under `all_text`; the strategy's white `text_color` survives; her pinned line lands centred in
the middle band (x 0.4–0.6, y 0.25–0.65), white. A caption-styling ask ("Captions small, white, in the
middle") still styles the captions. Still wrong after this fix: anything the brief writer must type (case 7's
box is group E).

## Decisions that remove whole classes of bugs

- **A place or colour belongs to the text it is written next to.** The scan already splits the message into
  segments (commas, colons, "and", sentences) and keeps only the segments about text (`_text_scope`). Each
  kept segment now also carries the role it names ("caption", "title", …), or `all_text` for plain text
  words. The rule's role comes from the segments that hold the place/colour words, not from every role
  word in the message. Same patterns, moved into helpers: no new regex, no new word list (KRI-667).
- **Only the bug shape changes.** The new role is used only where the old scan named exactly one role and
  none of the place/colour words sits in a segment about it. Two named roles stay `None`; a role the
  place/colour words sit next to stays that role.
- **"Captions off" is read from the typed brief fact, not her words.** Text alone cannot tell "no captions,
  white text" from "captions word by word, black text" without a negation pattern. The pinned brief's
  `captions: "off"` (Main Creator prompt) can: at dispatch a caption-role rule is filed under `all_text`
  when the brief turns captions off.
- **Kill switch.** `TEXT_STYLE_SEGMENT_ROLE_ENABLED` (default true, a bug fix). False = the old whole-message
  role scan and no typed backstop, byte-identical.

| # | Failure mode | Treatment | Pinned by |
|---|---|---|---|
| D1 | Her phrasing: "middle"/"white" sit in plain-text segments, "captions" only in "no captions"; captions steal them | Role from the segments holding the place/colour words → `all_text` | ported kit tests `test_middle_and_white_style_her_text_not_the_captions`, `test_her_pinned_line_lands_centred_in_the_middle_band` |
| D2 | The strategy's white `text_color` is still stripped from `all_text` | Follows from D1: the parsed role is `all_text`, nothing is stripped | ported `test_strategy_white_text_colour_survives_a_no_captions_ask` |
| D3 | A real caption-styling ask stops styling captions ("White captions please", Green 3, Turkish "Altyazılar küçük, beyaz, ortada") | The place/colour sits in the caption segment → caption, as before | existing `test_caption_parser_neighbours`, `test_green3_prompt_parses_to_the_captions_ask_not_the_speakers_corner` (unchanged) |
| D4 | Plain "text" words that describe the captions ("Captions word by word, black bold text in a light grey box") move to all text, recolouring titles and labels | A plain-text segment AFTER a named role continues that role → caption, as before | new neighbour test |
| D4b | The same across sentences ("Make the title bold. The text should be white.", "Captions please. Make the text yellow.", "Captions with no outline. White text."): found by the independent review of the first cut, which kept the role only within one sentence and so recoloured every text lane | The role named earlier anywhere in the message carries to later plain-text segments; only plain text named before any role is `all_text` | new neighbour tests |
| D5 | Plain text before the caption segment that also styles captions ("White text, captions in the middle") loses the caption role | The caption segment holds a place word, so caption is the styled role and the plain segment follows it → caption, as before | new neighbour test |
| D6 | Reversed order "No captions, white text in the middle" / "No captions. White text in the middle." / "Make text white, no captions, in the middle": by D4 the words go to captions (text alone cannot see the "no") | Typed backstop: brief `captions: "off"` files the caption rule under `all_text`. Without that fact it stays as before (gap: the brief writer must record it; KRI-667 forbids reading the "no") | new stamp test (captions off → all_text; captions on → caption, negative control) |
| D7 | Same segment "white text in the middle with no captions": one segment names captions | Same typed backstop as D6 | new stamp test |
| D8 | Outline / shadow / font words are read from the whole message, so their role is unknown when the place/colour role differs ("Small white text in the middle, no captions, no shadows") | Return `None` (nothing styled from words) instead of guessing; before, all went to captions. Only reachable now when the plain text comes before every role word (D4b keeps the role otherwise) | new neighbour test |
| D9 | A role word inside her quoted line ("My captions diary") files her style under captions | Quotes are already plain text in `_text_scope`; the segment role ignores them → `all_text` | new neighbour test |
| D10 | Turkish phrasing ("Bu yazıyı ortaya koy, beyaz olsun, altyazı olmasın") still goes to captions | Same segment logic (Turkish subject words already in the scan) → `all_text` | new neighbour test |
| D11 | Other readers of the parser change behaviour: durable rules (`creator_direction.infer_typed_direction`) and the title look (`phone_title_look.stamp_title_look`) | Intended: her text words now style all text there too (a saved "text_style_all_text" rule; a title reads `all_text` position/colour). Two-role and caption-adjacent asks unchanged (D3–D5) | existing `tests/services/test_creator_direction*.py`, `tests/tasks/test_title_look_worker.py` stay green |
| D12 | The typed backstop fires on a brief that turns captions ON, or with no caption fact | Only the LAST live `captions` fact in brief order equal to "off" counts (`brief_checks._caption_fact`) | stamp test negative control |
| D13 | An unreadable brief binding crashes the stamp | `_brief_captions_off` catches it like the KRI-1177 `_brief_caption_ask`: unreadable = no fact | code review (same try/except as `_brief_caption_ask`) |
| D14 | Old jobs restyle on retry | The stamp runs once at dispatch and is skipped for a job already stamped or a legacy generation (unchanged) | existing stamp guard |
| D15 | A new intent regex or word-membership test creeps in | Patterns moved, not added; the backstop reads a typed fact only | `tests/kria/test_kri667_no_new_intent_regexes.py` |
| D16 | Kill switch off is not byte-identical | Off path uses the old whole-message role and skips the backstop | `test_switch_off_is_the_old_whole_message_role`, `test_switch_off_skips_the_typed_backstop`; item 21's recorded golden rows for this exact message (`test_clip_text_tiktok_look.py::test_flag_off_rows_match_the_recorded_golden`, `::test_flag_off_middle_look_pin_is_drawn_like_top_center`) now run with the switch off and still match byte for byte |
| D17 | Collides with group A (`_ensure_generation_environment`) | Not touched | diff review |

## Not done here

- **Case 7's parser half** ("black bold text in a light grey box" keeps only the colour): drawing a box needs a
  box field and box-colour words the parser does not have; adding them is a new word list on creator text
  (KRI-667). Left to group E's typed `caption_box` fact. `test_the_style_parser_keeps_the_box_not_only_the_colour`
  stays red.
- D6/D7 without the typed `captions: "off"` fact keep today's behaviour.
- Review evidence: with the switch off, `explicit_text_rule` matched the 05d9720d8 code on 51,714 generated
  EN/TR styling sentences (0 differences); with it on, the differences were the intended `all_text` moves plus
  the cross-sentence regression fixed as D4b.
