# Narration annotation grounding

The response is a semantic hint pass for a deterministic transcript and timeline materializer.

Judge these points:

- Word anchors are copied from the supplied timed-word IDs.
- Score annotations cover an exact spoken score phrase and do not mark durations, dates, jersey numbers, or incidental counts.
- Topic text is anchored to words that actually say the topic.
- Participant annotations use supplied asset/timeline IDs and only claim `single_subject` when typed visual evidence explicitly supports it.
- The response is generic across requested topic kinds and respects negative requirements.
- Empty annotations are correct when the evidence is absent or ambiguous.
