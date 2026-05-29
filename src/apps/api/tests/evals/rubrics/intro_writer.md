# intro_writer rubric

The agent writes the opening on-screen hook for an edit made from the user's own
footage. Copy quality is the value prop. Score each output on these dimensions,
**integer 1-5**.

## Bilingual evaluation rule (read first)

Outputs may be in English OR Turkish (the `language` field on the input — `en`
or `tr` — specifies which). Evaluate the output **in the language it is written
in**: apply the same craft criteria (hook strength, grounding, voice, lowercase
default, no clickbait clichés) regardless of language. The reference voice
descriptions below use English exemplars; the equivalent Turkish creator voice
is **lowercase, casual `sen`-form (NOT formal `siz`), specific, in-the-moment**.
Do NOT penalize a Turkish output for being Turkish; DO penalize an output that
mixes English and Turkish, transliterates instead of using proper Unicode
diacritics (ç ş ğ ı İ ö ü), or uses formal `siz` when casual `sen` is the norm.
A Turkish hook that nails the voice scores **5** on voice_match, same as the
English reference hooks.

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
   - EN: "POV: you found people who say yes"
   - EN: "imagine traveling solo in berlin and ending up here"
   - EN: "this and no job"
   - TR: "bu saçla iş bambaşka" (informal sen-form, specific, lowercase)
   - TR: "keşke daha önce konuşsaydım" (curiosity-gap, intimate, lowercase, diacritics intact)

   Score:
   - 5: nails the voice — lowercase, specific, in-the-moment, makes the viewer want THIS life/place/moment. For TR: also casual sen-form with correct Unicode diacritics.
   - 3: right register but generic, or slightly off (e.g. Title Case, a touch of ad-speak)
   - 1: wrong voice entirely — clickbait cliché, ALL-CAPS line, Title Case Sentence, corporate/ad phrasing, OR (TR) ASCII-folded diacritics / formal siz-form / EN-TR mixing

5. **persona_coherence** — Does the hook fit the creator's persona + this specific video, WITHOUT sacrificing footage grounding? Read the input's `tone`, `content_pillars`, `theme`, and `idea`.
   - **Not applicable (score 5):** if the input carries NO persona/series context (`content_pillars` empty AND `theme` AND `idea` empty), there is nothing to cohere with — score **5** and do NOT penalize. This is the public one-off-edit case.
   - 5: clearly belongs to THIS creator on THIS day — the voice matches the stated tone, it nods to a content pillar or the theme/idea, and it is still truthfully about the hero clip. Coherent AND grounded.
   - 3: loosely on-persona/on-theme but generic, OR leans on the theme while only weakly tied to the footage.
   - 1: ignores the persona/theme entirely when one was given, OR — worse — invents a fact/place/event to force the theme that the hero clip does not support (grounding violation in service of coherence).

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"hook_strength": 4, "grounded_in_clip": 4, "craft": 4, "voice_match": 4, "persona_coherence": 4}, "reasoning": "<one sentence>"}
