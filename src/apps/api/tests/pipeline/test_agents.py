"""Unit tests for pipeline/agents/ — mock OpenAI responses."""

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.pipeline.agents.copy_writer import (
    PlatformCopy,
    _template_copy,
    _truncate,
    generate_copy,
)
from app.pipeline.agents.hook_scorer import (
    HookScoreList,
    _heuristic_score,
    score_hooks,
)


class TestHookScorer:
    def test_happy_path_returns_correct_count(self):
        sentences = ["This is amazing!", "What happens next?", "You won't believe this."]
        mock_result = HookScoreList(scores=[8.0, 9.0, 7.5])

        with patch("app.pipeline.agents.hook_scorer.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.beta.chat.completions.parse.return_value = MagicMock(
                choices=[MagicMock(message=MagicMock(parsed=mock_result))]
            )
            scores = score_hooks(sentences)

        assert len(scores) == 3
        assert scores[0] == pytest.approx(8.0)

    def test_scores_clamped_to_0_10(self):
        sentences = ["test"]
        mock_result = HookScoreList(scores=[15.0])  # out of range

        with patch("app.pipeline.agents.hook_scorer.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.beta.chat.completions.parse.return_value = MagicMock(
                choices=[MagicMock(message=MagicMock(parsed=mock_result))]
            )
            scores = score_hooks(sentences)

        assert scores[0] == pytest.approx(10.0)

    def test_api_failure_falls_back_to_heuristic(self):
        sentences = ["um so this happened", "What is this?"]

        with patch("app.pipeline.agents.hook_scorer.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.beta.chat.completions.parse.side_effect = Exception("API error")
            scores = score_hooks(sentences)

        assert len(scores) == 2
        # Filler word sentence should score lower
        assert scores[0] < scores[1]

    def test_length_mismatch_retries_then_pads(self):
        sentences = ["A", "B", "C"]
        # First call returns wrong count; second also returns wrong count → pad
        short_result = HookScoreList(scores=[5.0])

        with patch("app.pipeline.agents.hook_scorer.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.beta.chat.completions.parse.return_value = MagicMock(
                choices=[MagicMock(message=MagicMock(parsed=short_result))]
            )
            scores = score_hooks(sentences)

        assert len(scores) == 3

    def test_empty_input_returns_empty(self):
        assert score_hooks([]) == []

    def test_heuristic_question_mark_bonus(self):
        score_q = _heuristic_score("What is this?")
        score_plain = _heuristic_score("This is a thing.")
        assert score_q > score_plain

    def test_heuristic_filler_word_penalty(self):
        score_filler = _heuristic_score("um so basically this")
        score_clean = _heuristic_score("This is incredible")
        assert score_filler < score_clean


class TestCopyWriter:
    def test_happy_path_returns_generated_status(self):
        from app.pipeline.agents.copy_writer import InstagramCopy, TikTokCopy, YouTubeCopy

        mock_copy = PlatformCopy(
            tiktok=TikTokCopy(
                hook="Amazing hook",
                caption="Great caption",
                hashtags=["a", "b", "c", "d", "e"],
            ),
            instagram=InstagramCopy(
                hook="Amazing hook",
                caption="Great caption",
                hashtags=["a"] * 10,
            ),
            youtube=YouTubeCopy(
                title="YouTube title #shorts",
                description="Description here",
                tags=["tag"] * 15,
            ),
        )

        with patch("app.pipeline.agents.copy_writer.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.beta.chat.completions.parse.return_value = MagicMock(
                choices=[MagicMock(message=MagicMock(parsed=mock_copy))]
            )
            copy, status = generate_copy("Amazing hook", "transcript excerpt", ["instagram"])

        assert status == "generated"
        assert copy.tiktok.hook == "Amazing hook"

    def test_double_failure_returns_template_fallback(self):
        with patch("app.pipeline.agents.copy_writer.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.beta.chat.completions.parse.side_effect = Exception("API down")
            copy, status = generate_copy("hook text", "transcript", ["instagram"])

        assert status == "generated_fallback"
        assert "Auto-copy failed" in copy.instagram.caption

    def test_template_copy_contains_all_platforms(self):
        copy = _template_copy("My hook")
        assert copy.tiktok is not None
        assert copy.instagram is not None
        assert copy.youtube is not None

    def test_hashtag_count_enforced(self):
        # TikTok max is 5 hashtags
        from app.pipeline.agents.copy_writer import TikTokCopy

        t = TikTokCopy(hook="h", caption="c", hashtags=["a"] * 20)
        assert len(t.hashtags) == 5

    def test_caption_truncated_at_sentence_boundary(self):
        long_caption = "First sentence. " + "x" * 3000
        result = _truncate(long_caption, 2200)
        assert len(result) <= 2201  # +1 for ellipsis
        assert result.endswith("…") or len(result) <= 2200

    def test_short_text_not_truncated(self):
        short = "Hello world."
        assert _truncate(short, 2200) == short
