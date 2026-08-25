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

5. **transition_continuity** — Do cuts and transitions join adjacent shots
   cleanly without flashes, broken overlap, or a transition that obscures the
   intended moment?
   - 5: every boundary is clean and motivated
   - 3: one boundary feels loose or over-treated but remains understandable
   - 1: a boundary visibly fails, flashes, or hides required content

6. **optional_overlay_sfx_quality** — Do optional picture-in-picture overlays
   and licensed sound effects render cleanly and support rather than obstruct
   the footage/audio? Score only observed optional treatments; use 5 when none
   are present.
   - 5: clean, legible, audible, and supportive (or no optional treatment)
   - 3: distracting or mistimed but still usable
   - 1: visibly/audibly broken, obscuring, or clearly attached to the wrong beat

7. **speech_cut_integrity** — Where speech is present, are silence/retake cuts
   clean, preserving complete words and natural cadence? Use 5 when no speech
   cut is present or observable.
   - 5: natural cadence with no clipped words (or no speech cut)
   - 3: an audible seam or rushed pause, but meaning remains intact
   - 1: clipped words, duplicated speech, or a clearly broken cut

After scoring, also report a **confidence** in [0.0, 1.0]: how sure are you of
this verdict given what you could actually observe in the video (clear footage,
legible text, audible audio → high; ambiguous, very short, or hard-to-read
render → low). Low confidence MUST force a human review even when scores look
fine — it is the safety valve against confidently-wrong auto-passes.

Pass threshold: avg ≥ 3.5

Also return 1-8 **timecoded evidence** observations grounded in moments you
actually watched. Each observation must reference one score dimension, use
seconds from the start of the attached video, and describe only what is visible
or audible in that window. Never invent evenly spaced timestamps or infer a
moment you could not observe. `kind` is one of `visual`, `audio`, `timing`,
`caption`, or `structure`.

Return ONLY a JSON object of this exact shape:

    {"scores": {"hook_strength": 4, "text_legibility_and_timing": 4, "looks_filmed_not_templated": 3, "overall_quality": 4, "transition_continuity": 5, "optional_overlay_sfx_quality": 5, "speech_cut_integrity": 5}, "confidence": 0.8, "reasoning": "<one sentence: what changed and why this verdict>", "evidence": [{"dimension": "text_legibility_and_timing", "kind": "caption", "start_s": 1.2, "end_s": 2.8, "observation": "The opening caption remains readable against the moving background."}]}
