# final_video rubric

Judge the FINAL rendered short-form video (9:16, sub-60s) attached as media.
You are the dev-loop quality gate: a code change just produced this render and
we must decide whether it ships, gets rejected, or needs a human's eye. The
*mechanical* correctness floor (overlays un-clipped, encode valid) is already
guaranteed upstream by `make verify-overlays` — your job is the *creative*
quality on top of that floor.

Watch the whole clip. Score each dimension, **integer 1-5**.

1. **hook_strength** — Do the first 2-3 seconds create a question in the
   viewer's mind that pulls them forward?
   - 5: an immediate, specific open loop — you NEED to know what happens next
   - 3: on-topic but generic opening; mild curiosity, no real tension
   - 1: no hook — a slow/neutral/establishing start a scroller swipes past

2. **text_legibility_and_timing** — Is the on-screen text easy to read AND
   well-timed to the footage/beat (appears when relevant, holds long enough to
   read, exits cleanly, never stacked or overlapping)?
   - 5: every overlay is crisp, readable in one glance, and lands on-beat with
     the cut/music; nothing fights the footage
   - 3: readable but timing is loose — a line lingers or flashes, slightly off-beat
   - 1: illegible, mistimed, stacked/overlapping, or text that contradicts the shot

3. **looks_filmed_not_templated** — Does it feel like an authentic, human-edited
   real-life moment, or like a generic template stamped onto stock footage?
   - 5: feels filmed and intentionally cut — natural footage, motivated edits,
     no cookie-cutter feel
   - 3: competent but formulaic — you can see the template seams
   - 1: obviously machine-stamped; cheap, repetitive, or mismatched footage

4. **overall_quality** — Holistic: would you, as a taste-driven founder, be
   comfortable shipping THIS to a real creator's feed?
   - 5: ship it — genuinely good, on-brand short-form content
   - 3: acceptable but unremarkable; wouldn't be proud of it
   - 1: do not ship — embarrassing or broken in a way the floor checks missed

After scoring, also report a **confidence** in [0.0, 1.0]: how sure are you of
this verdict given what you could actually observe in the video (clear footage,
legible text, audible audio → high; ambiguous, very short, or hard-to-read
render → low). Low confidence MUST force a human review even when scores look
fine — it is the safety valve against confidently-wrong auto-passes.

Pass threshold: avg ≥ 3.5

Return ONLY a JSON object of this exact shape:

    {"scores": {"hook_strength": 4, "text_legibility_and_timing": 4, "looks_filmed_not_templated": 3, "overall_quality": 4}, "confidence": 0.8, "reasoning": "<one sentence: what changed and why this verdict>"}
