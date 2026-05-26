# intro_writer rubric

The agent writes the opening on-screen hook for an edit made from the user's own
footage. Copy quality is the value prop. Score each output on these dimensions,
**integer 1-5**:

1. **hook_strength** — Would this line make a viewer stop scrolling? It should create a question or tension in the first 2-3 seconds.
   - 5: genuinely makes you want to see what happens next; reads like a human short-form editor wrote it
   - 3: serviceable but flat ("check out this video"); no real curiosity
   - 1: boring, a non-sequitur, OR a generic clickbait cliché ("you won't believe", "wait for it", "this is everything") that signals nothing about THIS clip

2. **grounded_in_clip** — Is the hook actually about what the hero clip shows (per its subject/hook/transcript)? It must not invent facts or describe a different video.
   - 5: clearly tied to the hero clip's content; specific, not generic
   - 3: vaguely related; could describe many clips
   - 1: invents something not in the clip, or copies the clip's transcript verbatim instead of writing a hook

3. **craft** — Plain, punchy language. No emojis, hashtags, URLs, handles, or quotation marks wrapping the whole line. Appropriate length (short).
   - 5: tight and clean; every word earns its place
   - 3: a bit clunky or wordy but acceptable
   - 1: awkward phrasing, padded, or leaks artifacts (a stray URL/handle, ASS tags)

4. **voice_match** — Does it sound like a real creator captioning their own clip, in the target voice? The voice is lowercase-by-default, second-person or in-the-moment ("you", "imagine ...", "POV: ...", "this and ..."), specific, and aspirational — envy or curiosity, never ad-copy. Calibrate against these reference hooks, which are all a **5**:
   - "POV: you found people who say yes"
   - "imagine traveling solo in berlin and ending up here"
   - "this and no job"

   Score:
   - 5: nails the voice — lowercase, specific, in-the-moment, makes the viewer want THIS life/place/moment
   - 3: right register but generic, or slightly off (e.g. Title Case, a touch of ad-speak)
   - 1: wrong voice entirely — clickbait cliché, ALL-CAPS line, Title Case Sentence, or corporate/ad phrasing

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"hook_strength": 4, "grounded_in_clip": 4, "craft": 4, "voice_match": 4}, "reasoning": "<one sentence>"}
