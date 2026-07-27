# Edit Director Rubric

Score each fixture from 1-5 on:

- Editorial usefulness: every idea addresses a concrete weakness visible in the supplied draft.
- Creative specificity: treatments feel authored for this video, not generic advice.
- Coherence: operations inside each suggestion reinforce one intent and never contradict each other.
- Range and asset grounding: clip indices, time ranges, effect windows, transitions, and asset IDs are valid and available.
- Category balance: the set covers at least three useful dimensions across hook/pacing, text, audio, visual treatment, and transitions without token variety.
- Restraint: effects, text, and transitions support the story without overwhelming it.
- Explanation quality: titles, rationales, and expected benefits are concise, legible, and decision-ready.
- Language fidelity: suggestions preserve the creator's English or Turkish voice and do not introduce awkward translated copy.

Any duplicate, contradictory, unavailable, out-of-range, unsafe, or structurally invalid suggestion is a critical miss.

Judge operation validity against the exact editor contract present in the agent
input and output, not an assumed video API:

- Each suggestion card is an independent alternative. Users accept cards one at
  a time, so do not apply clip-timing changes from one card when judging the
  timeline positions in another card. Operations within a single card must
  still be mutually coherent.
- `allowed_op_families` contains family aliases, not literal operation names.
  For example, `text` permits `edit_text`, `patch_text_style`,
  `set_text_timing`, `add_text`, and `remove_text`; `timeline` permits clip and
  transition operations; `effect` permits camera-effect operations.
- `add_camera_effect` is complete with `start_s`, `end_s`, and optional
  `intensity`; it has no effect type or name field.
- `add_text` creates a new text bar with `text`, `start_s`, and `end_s`; it
  intentionally has no `bar_index`.
- Suggestion `start_s`/`end_s` describe the affected assembled-timeline range
  and may span multiple clips. They need only remain within
  `total_duration_s`; they do not need to equal one slot boundary.
- Asset IDs are grounded when copied exactly from the corresponding catalog or
  candidate list in the input.

Pass threshold: avg >= 4.0
