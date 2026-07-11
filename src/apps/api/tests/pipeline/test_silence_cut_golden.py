"""Golden regression: the user-validated cut of the 2026-07-09 test clip.

Word timings + silencedetect ranges captured from the real WhatsApp talk clip
the silence-cut behavior was tuned and APPROVED on (local-test round 2 —
plans/010). No media in git: only the detection INPUTS are snapshotted. If a
detection-rule change alters this plan, that is a deliberate product-behavior
change and this pin must be updated consciously, with a re-render review.
"""

import pytest

from app.pipeline.silence_cut import (
    MIN_KEEP_SEGMENT_S,
    build_cut_plan,
    remap_words,
)

DURATION_S = 22.895

WORDS = [
    {
        "text": "Uh,",
        "start_s": 0.0,
        "end_s": 0.54,
        "confidence": 0.034,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "this",
        "start_s": 0.72,
        "end_s": 1.0,
        "confidence": 0.984,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "is",
        "start_s": 1.0,
        "end_s": 1.18,
        "confidence": 0.998,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "a",
        "start_s": 1.18,
        "end_s": 1.34,
        "confidence": 0.956,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "test.",
        "start_s": 1.34,
        "end_s": 1.78,
        "confidence": 0.997,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "Um,",
        "start_s": 2.68,
        "end_s": 2.94,
        "confidence": 0.456,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "I",
        "start_s": 3.16,
        "end_s": 3.38,
        "confidence": 0.952,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "am",
        "start_s": 3.38,
        "end_s": 3.76,
        "confidence": 0.769,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "trying",
        "start_s": 3.76,
        "end_s": 4.14,
        "confidence": 0.997,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "to",
        "start_s": 4.14,
        "end_s": 4.42,
        "confidence": 0.999,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "build",
        "start_s": 4.42,
        "end_s": 4.64,
        "confidence": 0.993,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "NOAA,",
        "start_s": 4.64,
        "end_s": 5.2,
        "confidence": 0.494,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "which",
        "start_s": 5.94,
        "end_s": 6.38,
        "confidence": 0.987,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "is",
        "start_s": 6.38,
        "end_s": 6.72,
        "confidence": 0.998,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "turning",
        "start_s": 6.72,
        "end_s": 7.84,
        "confidence": 0.949,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "raw",
        "start_s": 7.84,
        "end_s": 8.32,
        "confidence": 0.535,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "footage",
        "start_s": 8.32,
        "end_s": 8.86,
        "confidence": 0.995,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "into",
        "start_s": 8.86,
        "end_s": 10.32,
        "confidence": 0.95,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "final,",
        "start_s": 10.32,
        "end_s": 11.46,
        "confidence": 0.581,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "and",
        "start_s": 13.94,
        "end_s": 14.72,
        "confidence": 0.672,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "to",
        "start_s": 14.72,
        "end_s": 14.94,
        "confidence": 0.877,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "end,",
        "start_s": 14.94,
        "end_s": 15.28,
        "confidence": 0.893,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "ready,",
        "start_s": 15.54,
        "end_s": 15.94,
        "confidence": 0.978,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "um,",
        "start_s": 16.24,
        "end_s": 16.48,
        "confidence": 0.912,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "ready",
        "start_s": 18.28,
        "end_s": 18.72,
        "confidence": 0.994,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "to",
        "start_s": 18.72,
        "end_s": 18.94,
        "confidence": 0.994,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "post",
        "start_s": 18.94,
        "end_s": 19.4,
        "confidence": 0.993,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "videos,",
        "start_s": 19.4,
        "end_s": 21.04,
        "confidence": 0.942,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
    {
        "text": "um...",
        "start_s": 21.42,
        "end_s": 22.58,
        "confidence": 0.707,
        "segment_avg_logprob": -0.381,
        "segment_no_speech_prob": 0.022,
    },
]

SILENCES = [
    (0.0, 0.178),
    (0.687, 0.828),
    (1.756, 2.571),
    (5.313, 6.191),
    (6.734, 7.575),
    (9.079, 10.042),
    (10.578, 11.096),
    (11.608, 14.598),
    (15.339, 15.58),
    (16.709, 18.6),
    (19.385, 20.721),
    (21.219, 21.408),
    (21.988, 22.87),
]

# (start_s, end_s, reason) — the approved plan: 6 removals, 8.14s saved (36%).
EXPECTED_REMOVALS = [
    (0.0, 0.66, "filler_lexical"),
    (1.91, 3.06, "silence"),
    (5.33, 5.82, "silence"),
    (11.61, 13.81, "silence"),
    (16.12, 18.16, "filler_lexical"),
    (21.3, 22.89, "filler_lexical"),
]


class TestGoldenClip20260709:
    def test_exact_removal_plan(self):
        plan = build_cut_plan(WORDS, SILENCES, DURATION_S)
        assert plan.bailout_reason is None
        got = [(r.start_s, r.end_s, r.reason) for r in plan.removed]
        assert len(got) == len(EXPECTED_REMOVALS), got
        for (g_lo, g_hi, g_r), (e_lo, e_hi, e_r) in zip(got, EXPECTED_REMOVALS):
            assert g_lo == pytest.approx(e_lo, abs=0.02), got
            assert g_hi == pytest.approx(e_hi, abs=0.02), got
            assert g_r == e_r, got

    def test_no_micro_fragments_and_no_filler_words_survive(self):
        plan = build_cut_plan(WORDS, SILENCES, DURATION_S)
        for lo, hi in plan.keep_segments:
            assert hi - lo >= MIN_KEEP_SEGMENT_S - 1e-9, plan.keep_segments
        kept_texts = [w["text"].lower().strip(".,!?…") for w in remap_words(WORDS, plan)]
        assert not any(t in ("uh", "um") for t in kept_texts), kept_texts
        assert len(kept_texts) == 25  # 29 words - 4 fillers

    def test_time_saved_within_removal_cap(self):
        plan = build_cut_plan(WORDS, SILENCES, DURATION_S)
        assert plan.time_saved_s == pytest.approx(8.14, abs=0.05)
