# intro_writer rubric

The agent writes the opening on-screen hook for an edit made from the user's own
footage. Copy quality is the value prop. Score each output on these dimensions,
**integer 1-5**:

1. **hook_strength** — Would this line make a viewer stop scrolling? It should create a question or tension in the first 2-3 seconds.
   - 5: genuinely makes you want to see what happens next; reads like a human short-form editor wrote it
   - 3: serviceable but flat ("check out this video"); no real curiosity
   - 1: boring, generic, or a non-sequitur

2. **grounded_in_clip** — Is the hook actually about what the hero clip shows (per its subject/hook/transcript)? It must not invent facts or describe a different video.
   - 5: clearly tied to the hero clip's content; specific, not generic
   - 3: vaguely related; could describe many clips
   - 1: invents something not in the clip, or copies the clip's transcript verbatim instead of writing a hook

3. **craft** — Plain, punchy language. No emojis, hashtags, URLs, handles, or quotation marks wrapping the whole line. Appropriate length (short).
   - 5: tight and clean; every word earns its place
   - 3: a bit clunky or wordy but acceptable
   - 1: awkward phrasing, padded, or leaks artifacts (a stray URL/handle, ASS tags)

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"hook_strength": 4, "grounded_in_clip": 4, "craft": 4}, "reasoning": "<one sentence>"}
