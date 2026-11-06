# suggestion_agent rubric

Scores one answer of the suggestion agent (`SuggestionAgentOutput`: up to five picks of
`{kind, target_ids, why, copy, rank}`) for one creator's footage and edit in the Kria iPhone chat.
The agent picks and the server builds, so every pick is already executable. The question behind
every score: **would a real everyday creator (a cafe owner, a student, a solo traveler, NOT a
marketer) tap the top card happily, or hesitate, feel judged, roll their eyes, or ignore the set?**

Companion to `tests/evals/test_edit_improver_skill.py`, whose model-free checker already proves
the structural rules for free: kind and targets were issued, at most five cards, ranks contiguous,
tier order (fix, essential, polish), nothing already in the edit or dismissed, no card that undoes
the creator's request, evidence behind every target, `why` <= 48 chars with no hype, invented
number, footage quote or wrong language, and hook copy that is short, grounded, not reused from
the transcript or on-screen text and free of the banned cringe phrases. Do not spend judge tokens
re-deriving those. This rubric scores what a checker cannot: taste.

You are given: the language (`en` or `tr`), the state (`first_prompt` or `ready`), the creator's
latest request, a plain summary of the footage and edit, the evidence facts per target, what is
already in the edit, and the answer. Judge the set as the creator reads it.

Score each dimension as an **integer 1-5**.

1. **impact_order** (does the top card matter most?)
   - 5: the first card fixes the biggest real problem the evidence shows (a noisy clip, a missing
     opening, twin shots) and nice-to-haves come last.
   - 3: right cards, but a smaller fix outranks a larger one.
   - 1: polish first while a visible defect sits lower, or the order looks arbitrary.

2. **evidence_grounding** (is every card about THIS footage?)
   - 5: each `why` states one concrete fact the evidence supports, in plain words.
   - 3: true but vague ("Some noise in the video"), or restates the title.
   - 1: a claim the evidence does not support, or a card any video could receive.

3. **restraint** (does it know when to stop?)
   - 5: only cards worth a tap. An empty set when nothing is worth suggesting.
   - 3: one marginal card that adds little.
   - 1: filler cards to look busy, or repeating what the edit already has.

4. **request_fidelity** (the creator's request wins)
   - 5: nothing offered undoes or second-guesses what the creator asked for.
   - 1: offers to remove their sound, text, clips or music after they asked to keep it.

5. **why_voice** (how the subtitle reads)
   - 5: calm, concrete, in the creator's language and register; spoken Turkish, not translated.
   - 3: stiff or product-speak.
   - 1: hype, a quality verdict, a call to action, marketer words, or wrong language.

6. **hook_craft** (judge only when a hook card is present; otherwise score 5)
   - 5: the line names what the camera shows, reads like a person noticing something, promises
     only what the opening delivers, and stays short. It would not embarrass the creator.
   - 3: grounded but flat, or leans on the account niche slightly.
   - 1: thought-leadership, a lesson or secret, guru or salesy voice, a claim the footage does not
     show, or words lifted from the transcript or on-screen text.

7. **one_change_per_card** (is each card a single, clear change?)
   - 5: each card does one thing the creator can picture before tapping.
   - 1: a card that bundles two changes or is too vague to picture.

8. **contract** (shape; the checker proves it, score 5 unless the answer is malformed)

**Pass threshold: avg >= 4.0**, the same bar as the empathy and cringe rubrics. Any single
dimension at 2 or below flags the answer regardless of the average.

## Failure flags (set all that apply)

- **polish_before_fix**: a nice-to-have outranks a defect the evidence shows.
- **filler_cards**: cards with no real reason, offered to fill the set.
- **undoes_request**: offers the opposite of what the creator asked for.
- **invented_fact**: a count, time or clip number the evidence does not contain.
- **cheesy_hook_voice**: salesy, guru, fake urgency, a secret, a lesson, a life-changing claim.
- **copy_from_footage**: hook copy repeats the transcript or on-screen text.
- **verdict_why**: the `why` judges quality instead of stating a fact.
- **translated_not_written** (Turkish): reads like a literal translation.

## Calibration

PASS (avg 4.8, no flags), ready, wind in clip 3 and twin shots in clips 2 and 5, sparse silent b-roll:
> 1 mute_clip "Wind noise in clip 3", 2 duplicates "Clips 2 and 5 are near-identical", 3 sfx
> "6 seconds of silent b-roll"

PASS (avg 4.9), nothing worth suggesting (candidates have no evidence): an empty ranked list.

PASS (avg 4.6, Turkish): fix then essential, spoken register
> 1 filler_words "7 yerde dolgu sözcüğü var", 2 captions "1. ve 4. klipte konuşma var"

PASS (avg 4.5): hook copy "Matcha, poured slow" over an opening that shows hands pouring matcha.

BORDERLINE (avg 3.4, flag `polish_before_fix`): sound effects ranked above the noisy-clip fix.

FAIL (avg 1.6, flags `cheesy_hook_voice`): hook copy "The secret nobody tells you about matcha".

FAIL (avg 1.8, flags `copy_from_footage`): hook copy that is the creator's spoken first line.

FAIL (avg 2.0, flags `undoes_request`): a mute card after "keep the original sound".

FAIL (avg 1.8, flags `filler_cards`): sound effects offered with no silent stretch in the footage.

FAIL (avg 1.4, flags `verdict_why`): why "Amazing fix for the wind in clip 3!"

Return ONLY:

    {"scores": {"impact_order": 4, "evidence_grounding": 5, "restraint": 5, "request_fidelity": 5, "why_voice": 5, "hook_craft": 5, "one_change_per_card": 5, "contract": 5}, "flags": [], "reasoning": "<one sentence>"}
