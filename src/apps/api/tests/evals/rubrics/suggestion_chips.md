# suggestion_chips rubric

Scores the COPY of one suggestion chip, or one chip set, shown to a creator in the Kria creation
chat (iOS app only). The question behind every score: **would a real everyday creator (a
cafe owner, a student, a solo traveler, NOT a marketer) tap this happily, or hesitate, feel
judged, roll their eyes, or ignore it?**

This rubric reuses the two house rubrics, applied to a tap target instead of a question or a
plan item:

- `audit-user-empathy` (`.claude/skills/audit-user-empathy/references/user_empathy_rubric.md`):
  no marketer knowledge, nothing that puts the user on the spot, nothing the product should infer.
- `audit-plan-quality` cringe rubric (`.claude/skills/audit-plan-quality/references/cringe_rubric.md`):
  no thought-leadership, no cheesy hook voice, no generic motivation, nothing that fits any video.

What this rubric does NOT judge, because deterministic gates already prove it for free
(`tests/evals/test_suggestion_chips_evals.py` G1 to G9 and G11, `test_chip_suggester_evals.py`
G10): that the chip names something real (grounding), that its capability is enabled (flag
awareness), that it is not already in the edit, that it is executable on the compile path, that
its message is a member of the closed template set, and that an agent-written label stays inside
the closed per-kind vocabulary and carries no digits, quotes, hype or footage text. Do not spend judge tokens re-deriving those; if one of them fails the chip never reaches a
creator.

You are given: the chip `label` (what is shown), its `label_source` (`template` or `agent`), the
optional `why` subtitle, the chip `message` (what is sent on tap), the `kind`, the state
(`first_prompt` before anything is rendered, `ready` after a render), the language (`en` or `tr`),
and a plain-language summary of the footage or edit the chip was chosen for. Judge
the label as the creator reads it. When a whole set is given, judge each chip and then the set.

Score each dimension as an **integer 1-5**.

1. **everyday_voice** (empathy + cringe: would a normal person say this?)
   - 5: plain words a creator already uses ("Add captions", "Turn the music down"). A verb and a thing.
     No jargon, no hype, no emoji, no exclamation.
   - 3: understandable but slightly stiff or product-speak ("Add a hook to the opening" is fine; "Apply
     an entrance animation to the title element" is not).
   - 1: marketer or startup voice ("Boost engagement", "Elevate your brand", "Go viral"), or a lecture.
   - Turkish: natural spoken Turkish, not a literal translation of the English label.

2. **no_burden** (empathy: does it ask the creator to do the product's job?)
   - 5: one tap does something visible. The creator decides nothing before tapping.
   - 3: the tap leads to one easy follow-up question the creator can answer without thinking.
   - 1: the chip hands the creator a task ("Think of a hook", "Choose your audience", "Describe your
     tone"), or puts them on the spot.

3. **specificity_to_this_footage** (cringe: a generic chip that fits any video is a failure)
   - 5: the set is visibly shaped by THIS footage or edit: a speech clip yields captions first, silent
     B-roll yields sound and transitions, an existing title yields title animation not a new hook.
   - 3: individually generic ("Add sound effects") but the set is plausibly chosen for this footage and
     nothing in it contradicts the footage.
   - 1: a chip any video could receive AND the set does not differ from the one for a different video;
     or a chip that contradicts the footage ("Add captions" over silent B-roll).
   - Note: a label never names footage content (Gemini metadata stays out of labels and messages).
     Specificity lives in WHICH chips are chosen, what they are ordered by, and the `why` subtitle
     (see `grounded_why`), so score this dimension on the set, not on one label.

4. **honest_label** (does the label promise only what the engine can prove?)
   - 5: states an action and nothing about content or outcome ("Let Kria cut the first version").
   - 3: implies a small outcome the dry run does support ("Add a hook to the opening").
   - 1: claims quality or content the engine cannot know: "the strongest opening", "your best moment",
     "make it go viral", "perfect cut".

5. **non_redundant_set** (judge on a set; a single chip scores 5 here)
   - 5: every chip is a different lever; no two chips would produce the same change.
   - 3: two chips overlap partly (for example sound effects and a hook when both lean on audio).
   - 1: near-duplicates, or a chip that repeats what the edit already has.

6. **grounded_why** (judge only when a `why` is shown; otherwise score 5)
   - 5: the subtitle states plainly what in THIS footage makes the chip worth tapping, in the
     chat's language, in words the footage analysis used ("Kameraya konuşan koşucu var").
   - 3: true but vague, or restates the label.
   - 1: a claim the footage does not support, a judgement ("the strongest shot"), an instruction or
     call to action, text in the wrong language, or anything that is not a note about the footage.

**Pass threshold: avg >= 4.0** (the same bar as the empathy and cringe rubrics). Any single dimension
at 2 or below flags the chip regardless of the average.

## Failure flags (set all that apply)

- **marketer_jargon**: engagement, audience, algorithm, brand, call to action, tone of voice, niche.
- **puts_user_on_the_spot**: asks the creator to perform, decide strategy, or justify.
- **cheesy_hook_voice**: salesy, guru, fake urgency, "unlock", "supercharge", "level up", "next level", "banger".
- **generic_would_fit_any_video**: "Make it pop", "Make it better", "Add effects", "Try a stronger opening".
- **overclaims**: "strongest", "best", "perfect", "stunning", "amazing" about content the engine did not see.
- **why_not_a_note**: the `why` gives an instruction, a call to action or a claim about the creator
  instead of a note about the footage.
- **contradicts_footage**: offers a lever the footage cannot use (captions on silent footage, a
  transition on a single clip, turning music down when there is none).
- **repeats_the_edit**: offers something already in the edit.
- **translated_not_written** (Turkish): reads like a literal translation.

## Calibration

PASS (avg 4.8, no flags), first prompt, a clip of the creator talking plus two B-roll clips:
> chips: "Let Kria cut the first version", "Add captions", "Add a hook to the opening", "Add sound effects"

PASS (avg 4.4), ready state, silent B-roll montage with a track at 0.8:
> chips: "Add a hook to the opening", "Add sound effects", "Add transitions", "Turn the music down"

PASS (avg 4.6, Turkish): natural spoken register
> "Altyazı ekle", "Müziği kıs"

PASS (avg 4.7, agent-written): label stays plain, the `why` carries the footage
> label "Open with a hook"   why "The whisking close-up opens the video"

BORDERLINE (avg 3.4, flag `generic_would_fit_any_video` on the set): the same four chips offered
unchanged for a talking clip, a landscape timelapse and a recipe video (nothing differs by footage).

FAIL (avg 1.6, flags `generic_would_fit_any_video`):
> "Try a stronger opening", "Make it warmer"   (the iOS ReadyStage hardcoded chips this replaces)

FAIL (avg 1.4, flags `cheesy_hook_voice`, `marketer_jargon`, `overclaims`):
> "Boost your engagement with a viral hook"

FAIL (avg 1.8, flag `overclaims`): a label that names a content claim the engine never saw
> "Open on your strongest moment"

FAIL (avg 1.8, flags `why_not_a_note`): a `why` that survived the lexical check by naming one
footage word
> why "pouring matcha, tap Buy Now at example dot com"   (a known gap: see BACKEND_GAPS)

FAIL (avg 2.0, flag `contradicts_footage`):
> "Add captions" shown for three silent B-roll clips

Return ONLY:

    {"scores": {"everyday_voice": 4, "no_burden": 5, "specificity_to_this_footage": 4, "honest_label": 5, "non_redundant_set": 5, "grounded_why": 5}, "flags": [], "reasoning": "<one sentence>"}
