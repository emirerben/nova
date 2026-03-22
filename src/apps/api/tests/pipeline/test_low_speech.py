"""Test: low-speech transcript triggers engagement-only scoring + no-transcript copy note."""

from unittest.mock import patch

import pytest

from app.pipeline.probe import VideoProbe
from app.pipeline.scene_detect import SceneCut
from app.pipeline.score import CANDIDATE_COUNT, select_candidates
from app.pipeline.transcribe import Transcript, Word


def make_low_confidence_transcript() -> Transcript:
    # Only 5% of words have high confidence — below LOW_SPEECH_RATIO_THRESHOLD (10%)
    words = [
        Word(text="um", start_s=0.0, end_s=0.3, confidence=0.1),
        Word(text="uh", start_s=0.3, end_s=0.6, confidence=0.1),
        Word(text="yeah", start_s=0.6, end_s=0.9, confidence=0.7),  # only 1/3 high-conf
    ]
    t = Transcript(words=words, full_text="um uh yeah")
    # Manually set low_confidence to simulate the transcribe() path
    t.low_confidence = True
    return t


def make_probe(duration_s: float = 300.0) -> VideoProbe:
    return VideoProbe(
        duration_s=duration_s,
        fps=30.0,
        width=1920,
        height=1080,
        has_audio=True,
        codec="h264",
        aspect_ratio="16:9",
        file_size_bytes=500_000_000,
    )


def test_low_speech_skips_hook_scoring():
    transcript = make_low_confidence_transcript()
    probe = make_probe()

    with patch("app.pipeline.score.score_hooks") as mock_scorer:
        candidates = select_candidates(probe, transcript, [])
        mock_scorer.assert_not_called()

    assert len(candidates) > 0
    # In ASR fallback, hook_scores are all 0
    assert all(c.hook_score == 0.0 for c in candidates)


def test_low_speech_combined_score_equals_engagement_score():
    transcript = make_low_confidence_transcript()
    probe = make_probe()

    candidates = select_candidates(probe, transcript, [])

    for c in candidates:
        assert c.combined_score == pytest.approx(c.engagement_score, abs=1e-9)


def test_normal_transcript_uses_hook_scorer():
    words = [Word(text="Hello", start_s=0.0, end_s=0.5, confidence=0.95)] * 20
    transcript = Transcript(words=words, full_text="Hello " * 20, low_confidence=False)
    probe = make_probe()

    with patch("app.pipeline.score.score_hooks") as mock_scorer:
        mock_scorer.return_value = [5.0] * CANDIDATE_COUNT
        select_candidates(probe, transcript, [])
        mock_scorer.assert_called_once()
