# conformance_feedback rubric

The agent receives a plan item's structured shot list (filming_guide), a text-only
digest of the uploaded clip's metadata (ClipMetadataOutput subset), and the plan
item's theme and idea. It must decide whether the clip matches the brief, articulate
what drifted, and suggest actionable re-shoot tips.

Read the input (filming guide + clip digest + theme/idea) and the ConformanceOutput.
Score each dimension, **integer 1-5**:

1. **verdict_accuracy** — Is the verdict (on_track / minor_drift / off_brief) correct
   given the clip digest vs the shot list?
   - 5: verdict precisely matches the degree of drift; a clip with the right subject
     but wrong angle is minor_drift not off_brief; a completely wrong subject is
     off_brief; a matching clip is on_track
   - 3: verdict is one step off (e.g. minor_drift instead of on_track for a very
     close match); or verdict on a borderline case is debatable
   - 1: verdict contradicts clear evidence (e.g. on_track for a completely wrong
     subject; off_brief for a matching clip)

2. **mismatch_specificity** — Are the listed mismatches specific, signal-bearing, and
   bounded (≤3)?
   - 5: each mismatch names a concrete gap between shot list and clip (e.g. "expected
     overhead kitchen shot, got eye-level selfie"); max 3 items; none duplicated
   - 3: mismatches are vague ("content doesn't match") or there are more than 3 items;
     or one mismatch duplicates another
   - 1: mismatches list is empty when the verdict is minor_drift or off_brief; or
     mismatches are hallucinated (claim facts not in the digest)

3. **suggestion_actionability** — Are the suggestions concrete re-shoot tips that
   directly address the identified mismatches?
   - 5: each suggestion references a specific shot in the filming guide and gives a
     clear corrective action (angle, subject, duration); max 3 items
   - 3: suggestions are generic ("try a different angle") without referencing which
     shot or what to film specifically
   - 1: suggestions are absent when mismatches exist; or suggestions are irrelevant to
     the noted mismatches

4. **summary_quality** — Does the one-line summary convey "this looks like X instead
   of Y; engagement risk Z" accurately and concisely?
   - 5: summary identifies what the clip actually is (X), what was asked for (Y), and
     a concrete engagement risk or consequence
   - 3: summary states the mismatch but omits the engagement risk; or is too long (>2
     sentences)
   - 1: summary is missing, empty, or does not describe the actual drift

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"verdict_accuracy": 4, "mismatch_specificity": 4, "suggestion_actionability": 3, "summary_quality": 4}, "reasoning": "<one sentence>"}
