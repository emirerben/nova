# Length ceilings are met by a shorter draft (KRI-847)

## Incident

Don Julio T2 (parent KRI-829) asks for a Talking edit of Lucas explaining the sauces,
cut from "Here we have traditional Argentinian sauces" to "Thank you", with photo
cut-aways, a hook title, word-by-word captions and "Under 10 seconds." The correct
window is about 7.5 s (about 9.1 s with the 1.6 s Kria ending). An offline trace on
main `75a9fff9d` found the brief stored `{"duration_s": 10}` and every length gate
judged it as 10 s +/- 10 %, so the draft receipt read "This draft is about 7.5s; you
asked for 10s." DJ T2 has never been sent on a phone.

## Acceptance (written before the fix)

- "Under 10 seconds" with a 7.5 s draft is met; an 11 s result is a miss.
- No gate asks the creator to lengthen, or declines, a draft that is shorter than the
  ceiling they typed: draft receipt, render length gate, length plan, choice cards,
  draft target, music window, speech-montage notes.
- An exact length ("make it 10 seconds") keeps its +/- 10 % judgement, wording and
  stored data byte for byte. Briefs, contracts and jobs without the new fact behave
  exactly as before.
- A later exact length ("make it 15 seconds", or a choice answer) ends the ceiling.

## What main already does (KRI-629, #1757)

Exclusive wording ("under / less than / shorter than N", Turkish "N saniyenin altında /
saniyeden kısa") is a strict whole-export cap read from the requirement description and
checked on the measured file, ending included. For the literal T2 wording that path
already gives: unmeasured draft "I'll check the full video length… after export" (never
"you asked for 10s"), measured 9.1 s met, measured 11 s a miss. This change leaves that
path untouched and does not create a hard cap from a model fact.

## Change

- The brief's timing rule teaches the model to record any ceiling, exclusive or
  inclusive ("at most", "max", "no more than", "up to", "or less", "en fazla",
  "geçmesin"), as `facts.duration_bound = "at_most"`. No creator-text regex (KRI-667).
- `at_most` only removes the too-short side of the existing check:
  `0 < actual <= N x 1.10`. It relaxes, never tightens.
- The bound reaches the receipts (wording "at most N s" / "en fazla N saniye"), the
  render length gate (sibling assembly key `creator_length_bounds`, so the frozen
  contract schema and stored policy payload are unchanged), length planning, the
  natural-length and clip-count cards, the draft target (`min(model, N)`), the music
  window and the speech-montage "shorter than you asked" note.

## Case matrix (offline, deterministic)

| Case | Expected | Test |
| --- | --- | --- |
| T2 "Under 10 seconds", Talking, unmeasured | unchecked, no "10s" target | `tests/services/test_length_upper_bound_kri847.py` |
| T2 measured 9.1 s / 11.0 s | met / miss "under 10s" | same |
| "at most / max / no more than 10", "en fazla 10 saniye", "10 saniyeyi geçmesin" + 7.5 s | met | same (parse → apply → check) |
| same + 11.5 s | miss "at most 10s" / "en fazla 10 saniye" | same |
| exact "make it 10 seconds" + 7.5 s (negative control) | miss "you asked for 10s" | same |
| legacy fact-less row, unknown bound value | exact | same |
| render gate, enforce mode: 7.5 s / 11.5 s under at_most | pass / decline naming the ceiling | `tests/services/test_length_upper_bound_contract_kri847.py` |
| stamping through dispatch and editor saves | bound kept; timeline Save becomes exact | `tests/tasks/test_length_upper_bound_dispatch_kri847.py`, `tests/services/test_length_upper_bound_editor_kri847.py` |
| length plan, cards, music window, speech note | shorter is not a shortfall | `*_planning_*`, `*_choices_*`, `*_music_*`, `*_speech_kri847.py` |
| a ceiling then "make it 15 seconds" (partial or complete change), or a choice answer | exact 15 | `tests/services/test_length_upper_bound_kri847.py`, `*_choices_kri847.py` |
| prompts teach the fact; extractor `.replace` anchors still match | guard | `tests/agents/test_duration_bound_prompt_kri847.py` |

Independent review mutated 20 pieces of the change; each was caught by these tests.
A fuzz of about 2M lengths found no case where an exact ask passes and a ceiling fails.

## Gaps

- Model compliance with `duration_bound` is not live-verified (no paid eval). Without
  the fact, inclusive or paraphrased ceilings ("10 seconds max", "below 10 seconds")
  keep the old exact judgement. Briefs stored before this change stay exact.
- `at_most` keeps the +10 % upper tolerance: "max 10 s" accepts up to 11.0 s. Only the
  KRI-629 exclusive wording is a strict cap.
- A continuous voice shorter than a ceiling still gets the "voice can't fill" card,
  because the voice-behind-footage compile builds exactly N.
- A strict "under N" phone montage still plans to N, then fails the strict check once
  the ending is added (KRI-629 left automatic budgeting out).
- Not replayed on an iPhone; DJ T2 should be sent after deploy.
