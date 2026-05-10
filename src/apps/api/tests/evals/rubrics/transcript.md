# transcript rubric

Score each output on these dimensions, **integer 1-5**:

1. **timing_accuracy** — Do per-word `start_s` / `end_s` align with audible word boundaries?
   - 5: every word timestamp lands within ±150ms of where the word is actually spoken
   - 3: most timestamps are within ±400ms; a few drift further
   - 1: timestamps are arbitrary, fully out of sync, or piled up at slot boundaries

2. **fidelity** — Is the transcription verbatim?
   - 5: matches the audio word-for-word; preserves contractions, false starts, fillers if present
   - 3: paraphrases occasionally or auto-corrects clear speech
   - 1: invents content, drops sentences, or summarizes instead of transcribing

3. **confidence_calibration** — Does the `low_confidence` flag and per-word `confidence` reflect actual audio quality?
   - 5: clean audio yields `low_confidence=False` and high per-word confidences; noisy or muffled audio yields `low_confidence=True` or visibly lower confidences on the affected spans
   - 3: flag is in the right direction but per-word values look uniform
   - 1: flag contradicts the audio (clean audio marked low_confidence, or very noisy audio still confident)

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"timing_accuracy": 4, "fidelity": 5, "confidence_calibration": 4}, "reasoning": "<one sentence>"}
