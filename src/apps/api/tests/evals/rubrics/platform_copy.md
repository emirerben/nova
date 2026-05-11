# platform_copy rubric

Score each output on these dimensions, **integer 1-5**:

1. **platform_native_voice** — Does each platform's copy feel native to that platform?
   - 5: TikTok hook is genuinely hooky (curiosity, stakes, or reveal in ≤150 chars), Instagram caption uses Instagram voice (more polished, hashtag-front), YouTube title is searchable (keywords, "#shorts", concrete subject)
   - 3: copy is competent but interchangeable across platforms
   - 1: same string in three places, or every platform sounds like a press release

2. **content_alignment** — Does the copy actually reflect the clip's transcript and energy?
   - 5: copy references something specific from the clip (action, surprise, line); tone matches the energy
   - 3: copy is generic but plausible for the topic
   - 1: copy is unrelated to the clip or contradicts it

3. **hashtag_quality** — Are hashtags relevant, varied, and non-spammy?
   - 5: mix of broad-reach + niche tags; all relevant; no banned/spam tags
   - 3: tags relevant but all generic ("#fyp #viral #foryou")
   - 1: irrelevant tags, keyword stuffing, or empty/duplicate

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"platform_native_voice": 4, "content_alignment": 4, "hashtag_quality": 3}, "reasoning": "<one sentence>"}
