# Camera emphasis placement rubric (KRI-7)

The agent is a camera operator deciding where a push-in helps the viewer. It
picks candidate indexes from the supplied spoken phrases — it authors no text,
no times, and no geometry.

Score these dimensions 1–5:

**Grounding.** Every `candidate_index` exists in the supplied candidates, none
repeats, and the count respects the caller's `max_effects`. An index that was
never offered is a hard failure, not a style choice.

**Choice.** The emphasised phrases are the ones carrying the video's substance:
the claim, the number, the turn in the argument, the first beat of a list the
viewer must hold on to. Greetings, filler ("um", "so", "basically"), setup that
only exists to reach the real line, repetition of the previous phrase, and
sign-offs earn nothing. A `preset_pick` flag is a hint — confirming a weak
preset pick, or ignoring a strong phrase because it was not flagged, both score
poorly.

**Restraint.** Two well-chosen pushes read as direction; five read as a nervous
camera. Emphases sit at least one unemphasised phrase apart. At most one
`strong` — the single biggest moment of the video. An empty list is the RIGHT
answer for footage that is all setup and no claim; do not penalise it, and do
not reward padding a weak set up to `max_effects`.

**Shape.** `zoom_in` (push in and hold) for a phrase that carries substance;
`pulse` (one quick accent) for a short beat that punctuates — a single word, a
name, a count-off. A pulse on the video's main claim, or a held zoom on a
throwaway aside, is the wrong instrument.

**Reason.** Each `reason` is short and names what makes the moment matter, in
terms recoverable from the phrase itself — not a restatement of the phrase and
not invented context.

Pass threshold: avg ≥ 3.5
