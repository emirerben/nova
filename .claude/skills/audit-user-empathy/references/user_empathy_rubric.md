# User-empathy rubric

This rubric grades a single question or copy string that Nova's AI shows to a real user.
The one question behind every score: **would a real everyday creator (café owner, student,
solo traveler — NOT a marketer) answer this comfortably and feel the product gets them?
Or would they hesitate, feel dumb, feel judged, or close the app?**

Nova's user is not a marketer and shouldn't have to think like one. They don't know who
their "target audience" is. They shouldn't have to name their "tone of voice", their
"content pillars", or decide their "brand positioning". The product does the strategy; the
user just shows up and talks about their actual life.

You are given the source of the question (which surface/prompt it came from), the
conversation context up to that point (if any), and the persona the AI was talking to.
Judge each question in context — a follow-up question is worse if the user already gave
the information it's asking for.

## Score (1–5) — how well this serves a real everyday user

- **5** — A concrete, easy, life-grounded question anyone can answer. The user knows
  exactly what to say without overthinking. Example: "What footage is sitting on your
  phone right now that you haven't posted?"
- **4** — Good and answerable, maybe slightly abstract but still clearly about the user's
  actual life. Most users wouldn't hesitate.
- **3** — Borderline. Requires a moment of thought the user shouldn't need, OR leans on
  a concept the user might not have. A marketer would answer easily; a café owner might
  pause.
- **2** — Mostly burdensome: requires marketing or strategy knowledge, makes the user feel
  on the spot, or asks them to do the product's job.
- **1** — The "close-the-app" question: demands the user know who their target audience is,
  name their brand, define their positioning, or answer something only a trained marketer
  could answer confidently.

Anything scoring < 4 is treated as flagged. Independently, set every failure flag that
applies (a question can score 3 with one flag, or 1 with several).

## Failure flags (set all that apply)

- **assumes_targeting_knowledge** — asks who the user's audience / target / "who it's for"
  is. The canonical offender: "Who are you secretly filming for?" / "Who do you imagine
  watching your videos?" Nova is supposed to infer the audience from what the user says
  about their life — asking the user to name a target audience is the product doing its job
  in reverse. *This is the flagship flag.*
- **assumes_marketer_knowledge** — needs niche/positioning/pillars/brand/tone concepts to
  answer well. Examples: "What's your tone of voice?", "What are your content pillars?",
  "How would you describe your brand?". A café owner has no idea what "content pillars" means.
- **product_should_infer_this** — asks the user to supply information the product should
  derive from context. If Nova already has footage, location, or prior answers, asking for
  things it could infer is friction, not onboarding.
- **puts_user_on_the_spot** — emotionally demanding, makes the user perform, or positions
  them as failing if they can't answer. Examples: "What do you want to be known for?",
  "Who are you secretly filming for?" (implies they must have a hidden audience). Makes
  the user feel judged for not having a brand strategy.
- **jargon_or_survey_language** — uses internal product jargon or survey phrasing that
  sounds like a marketing questionnaire rather than a natural conversation. Examples:
  "content pillars", "cadence", "tone of voice", "niche", "target demographic", or
  "what is your posting strategy?".
- **vague_or_unanswerable** — too abstract or open-ended for a real person to answer
  concretely without freezing. "What do you want your content to achieve?" has no good
  answer for a café owner.
- **presumptuous** — assumes ambitions, means, or scale not in evidence: fame, a big
  following, production equipment, a team, disposable travel budget. A student asked
  "what kind of studio setup do you have?" would close the app.
- **redundant** — re-asks something already answered or clearly established earlier in the
  same conversation. Wastes the user's time and signals the AI wasn't listening.
- **not_actionable** — (for copy/feedback, not questions) the user can't tell what to do
  next after reading this. Includes empty acknowledgments that don't move the user forward.

## Calibration examples

PASS (score 5, no flags):
> "What footage is sitting on your phone right now that you haven't posted?"

PASS (score 4, no flags):
> "What would you keep filming even if nobody ever watched?"

FAIL (score 1, assumes_targeting_knowledge + puts_user_on_the_spot):
> "Who are you secretly filming for?"

FAIL (score 1, assumes_targeting_knowledge + product_should_infer_this):
> "Who do you imagine watching your videos?"

FAIL (score 2, assumes_marketer_knowledge + jargon_or_survey_language):
> "What are your content pillars?"

FAIL (score 2, jargon_or_survey_language):
> "What's your tone of voice?"

FAIL (score 2, puts_user_on_the_spot + vague_or_unanswerable):
> "What do you want to be known for?"

FAIL (score 3, jargon_or_survey_language):
> "Who is this actually for?" (in a context where no framing has been established)

PASS (score 5, no flags — style agent):
> "Did you mean the font size or the font style?"

PASS (score 4, no flags — static form):
> "What do you do for fun?"

Pass threshold: avg ≥ 4.0
