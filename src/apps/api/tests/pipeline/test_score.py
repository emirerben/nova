"""Unit tests for pipeline/score.py"""

from unittest.mock import patch

import pytest

from app.pipeline.probe import VideoProbe
from app.pipeline.scene_detect import SceneCut
from app.pipeline.score import (
    CANDIDATE_COUNT,
    TOP_N,
    _adjust_hook_score,
    _first_sentence,
    _generate_segments,
    _normalize_to_10,
    select_candidates,
)
from app.pipeline.transcribe import Transcript, Word


def make_transcript(words: list[tuple[str, float, float]], low_confidence: bool = False) -> Transcript:  # noqa: E501
    ws = [Word(text=t, start_s=s, end_s=e, confidence=1.0) for t, s, e in words]
    return Transcript(words=ws, full_text=" ".join(t for t, _, _ in words), low_confidence=low_confidence)  # noqa: E501


def make_probe(duration_s: float = 300.0, aspect_ratio: str = "16:9") -> VideoProbe:
    return VideoProbe(
        duration_s=duration_s,
        fps=30.0,
        width=1920,
        height=1080,
        has_audio=True,
        codec="h264",
        aspect_ratio=aspect_ratio,
        file_size_bytes=500_000_000,
    )


class TestGenerateSegments:
    def test_short_video_returns_one_segment(self):
        segs = _generate_segments(30.0)
        assert len(segs) == 1
        assert segs[0][0] == pytest.approx(0.0)

    def test_long_video_returns_nine_segments(self):
        segs = _generate_segments(600.0)
        assert len(segs) == CANDIDATE_COUNT

    def test_segments_do_not_exceed_duration(self):
        segs = _generate_segments(300.0)
        for start, end in segs:
            assert end <= 300.0 + 0.01  # float tolerance
            assert start >= 0.0


class TestAdjustHookScore:
    def test_scene_cut_proximity_adds_bonus(self):
        score = _adjust_hook_score(5.0, "Hello world", 10.0, {10.5})
        assert score == pytest.approx(6.0)

    def test_filler_word_deducts(self):
        score = _adjust_hook_score(5.0, "um so basically", 10.0, set())
        assert score == pytest.approx(4.0)

    def test_clamped_at_zero(self):
        score = _adjust_hook_score(0.5, "um so basically", 0.0, set())
        assert score >= 0.0

    def test_clamped_at_ten(self):
        score = _adjust_hook_score(9.5, "Great hook", 5.0, {5.0})
        assert score <= 10.0


class TestFirstSentence:
    def test_extracts_first_sentence(self):
        transcript = make_transcript([
            ("Hello", 10.0, 10.5),
            ("world.", 10.5, 11.0),
            ("Next", 11.0, 11.5),
            ("sentence.", 11.5, 12.0),
        ])
        result = _first_sentence(transcript, 10.0, 15.0)
        assert result == "Hello world."

    def test_empty_window_returns_empty_string(self):
        transcript = make_transcript([("Hello", 5.0, 5.5)])
        result = _first_sentence(transcript, 100.0, 200.0)
        assert result == ""

    def test_no_sentence_boundary_returns_words(self):
        transcript = make_transcript([("Hello", 0.0, 0.5), ("world", 0.5, 1.0)])
        result = _first_sentence(transcript, 0.0, 5.0)
        assert "Hello" in result


class TestNormalizeToTen:
    def test_all_same_values_returns_five(self):
        result = _normalize_to_10([3.0, 3.0, 3.0])
        assert all(v == pytest.approx(5.0) for v in result)

    def test_min_is_zero_max_is_ten(self):
        result = _normalize_to_10([0.0, 5.0, 10.0])
        assert min(result) == pytest.approx(0.0)
        assert max(result) == pytest.approx(10.0)

    def test_empty_returns_empty(self):
        assert _normalize_to_10([]) == []


class TestSelectCandidates:
    def test_returns_nine_candidates_for_long_video(self):
        probe = make_probe(duration_s=600.0)
        transcript = make_transcript([
            ("This is amazing!", 0.0, 3.0),
            ("More content here.", 50.0, 53.0),
        ])
        cuts = [SceneCut(timestamp_s=45.0, score=1.0)]

        # No source_ref → heuristic path (no Gemini calls)
        candidates = select_candidates(probe, transcript, cuts)

        assert len(candidates) == CANDIDATE_COUNT

    def test_candidates_sorted_by_combined_score_descending(self):
        probe = make_probe(duration_s=600.0)
        transcript = make_transcript([])

        candidates = select_candidates(probe, transcript, [])

        scores = [c.combined_score for c in candidates]
        assert scores == sorted(scores, reverse=True)

    def test_asr_fallback_skips_hook_scoring(self):
        probe = make_probe(duration_s=300.0)
        transcript = make_transcript([], low_confidence=True)

        # source_ref=None + low_confidence → engagement-only scoring
        candidates = select_candidates(probe, transcript, [])

        # In ASR fallback, all hook_scores are 0
        assert all(c.hook_score == 0.0 for c in candidates)

    def test_top_n_have_rank_lte_top_n(self):
        probe = make_probe(duration_s=600.0)
        transcript = make_transcript([])

        candidates = select_candidates(probe, transcript, [])

        top = candidates[:TOP_N]
        assert all(c.rank <= TOP_N for c in top)

    def test_gemini_source_ref_calls_analyze_clip(self):
        """When source_ref is provided, Gemini analyze_clip is called per segment."""
        probe = make_probe(duration_s=600.0)
        transcript = make_transcript([("Hello", 0.0, 0.5)])
        from app.pipeline.agents.gemini_analyzer import ClipMeta

        mock_meta = ClipMeta(
            clip_id="files/abc",
            transcript="hello",
            hook_text="This is amazing!",
            hook_score=8.0,
            best_moments=[],
        )
        mock_source_ref = object()

        with patch("app.pipeline.agents.gemini_analyzer.analyze_clip", return_value=mock_meta) as mock_analyze:
            candidates = select_candidates(probe, transcript, [], source_ref=mock_source_ref)

        assert mock_analyze.call_count == CANDIDATE_COUNT
        assert all(c.hook_score == pytest.approx(8.0) for c in candidates)
