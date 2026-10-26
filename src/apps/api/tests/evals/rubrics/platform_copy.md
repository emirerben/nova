# platform_copy rubric

Score each output on these dimensions, **integer 1-5**:

1. **platform_native_voice** — Does each platform's copy feel native to that platform?
   - 5: TikTok hook is genuinely hooky (curiosity, stakes, or reveal in ≤150 chars), Instagram caption fits the creator’s voice and adds useful context, YouTube title is searchable (keywords, "#shorts", concrete subject)
   - 3: copy is competent but interchangeable across platforms
   - 1: same string in three places, or every platform sounds like a press release

2. **content_alignment** — Does the copy reflect the supplied evidence without confusing edit intent with observed footage?
   - 5: grounded specificity where evidence exists; modest accurate copy when only a title is supplied; caption complements the hook
   - 3: copy is generic but plausible for the topic
   - 1: copy is unrelated to the clip or contradicts it

3. **hashtag_quality** — Are hashtags relevant, varied, and non-spammy?
   - 5: only precise relevant tags, or zero when none add clarity; no filler or unsupported trend/reach claims
   - 3: mostly relevant tags with unnecessary generic filler
   - 1: irrelevant tags, keyword stuffing, duplicates, or invented trending claims

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"platform_native_voice": 4, "content_alignment": 4, "hashtag_quality": 3}, "reasoning": "<one sentence>"}
