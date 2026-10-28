# camera_roll_stories rubric (voice)

The agent proposes up to three short-form content ideas for a creator's camera-roll event:
a title, an angle, the on-screen opening text, optional closing text and a posting caption.
This rubric measures one failure: ideas that read like the creator's private diary instead
of a post a stranger would stop for. Score the WHOLE set of ideas as one output, **integer
1-5** per dimension.

Context you are given: the candidate footage (analysis prose, no GPS), the creator's query,
and, when present, `event_title` / `event_about`. Judge only the proposed wording and angles,
not the edit's pacing or footage selection.

1. **diary_voice** — LOWER IS BETTER. Does the wording recap the creator's day for themselves?
   - 1: no diary voice. Every idea speaks to a viewer: a claim, a reason to watch, a question
     or a tip that a stranger could use.
   - 3: mixed. Some ideas lead with a viewer takeaway; others are "my day"-style recap, or the
     copy is neutral description with no point of view.
   - 5: pure diary. First-person recap ("my day", "we spent", "today I"), a mood with no
     claim, "a relaxed day exploring ..." as the whole idea. A viewer learns nothing and has
     no reason to keep watching.

2. **viewer_value** — Within about 2 seconds, does a stranger know why this is worth watching?
   - 5: each opening names a concrete payoff (why to visit, what they will see that is
     unusual, a recommendation, a verdict, a genuine curiosity gap).
   - 3: a plausible reason exists but it is vague ("explore the island's charm").
   - 1: no reason at all, or the only hook is a mood or an emotion about the creator.

3. **specificity** — Does the copy name the place and concrete features when the context
   gives them (event title, the footage's windmill, stone houses, olive trees, harbour, cat)?
   - 5: names the place from `event_title`/query when supplied and at least one concrete
     feature in each idea. With no place in context, concrete footage features are named
     instead of generic words.
   - 3: some ideas specific, others could caption any holiday.
   - 1: generic filler ("stunning views", "hidden gem", "perfect day by the sea") that fits
     any trip. If a place IS in context and no idea names it, cap at 2.

4. **grounding** — HIGHER IS BETTER. Is everything claimed supported by the candidate
   descriptions, the query, or `event_about`?
   - 5: nothing invented. Claims about the place come only from the supplied context;
     opinions are phrased as the creator's own take without fake facts.
   - 3: one soft embellishment (e.g. a mood or opinion stated as fact).
   - 1: invented facts (history, dates, prices, names, events, motion/sound from a photo) or
     identities the evidence does not support.

Do NOT reward length, hashtags or emoji. Do NOT penalize an idea for being a guide or a
"why visit" pitch when the event context asks for it; do penalize it for brochure slogans
that say nothing about THIS footage.

Do not average these scores: diary_voice points the opposite way from the other three. The
harness computes `composite = mean(6 - diary_voice, viewer_value, specificity, grounding)`.

Return ONLY:

    {"scores": {"diary_voice": 2, "viewer_value": 4, "specificity": 4, "grounding": 5}, "reasoning": "<one sentence>"}
