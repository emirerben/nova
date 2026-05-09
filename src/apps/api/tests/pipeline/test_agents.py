"""Unit tests for pipeline/agents/ — mock Gemini responses."""

import json
from unittest.mock import MagicMock, patch

from app.pipeline.agents.copy_writer import (
    _template_copy,
    _truncate,
    generate_copy,
)


def _make_gemini_response(data: dict) -> MagicMock:
    """Build a mock Gemini response with the given JSON payload."""
    response = MagicMock()
    response.text = json.dumps(data)
    candidate = MagicMock()
    candidate.finish_reason.name = "STOP"
    response.candidates = [candidate]
    return response


class TestCopyWriter:
    def test_happy_path_returns_generated_status(self):
        mock_data = {
            "tiktok": {
                "hook": "Amazing hook",
                "caption": "Great caption",
                "hashtags": ["a", "b", "c", "d", "e"],
            },
            "instagram": {
                "hook": "Amazing hook",
                "caption": "Great caption",
                "hashtags": ["a"] * 10,
            },
            "youtube": {
                "title": "YouTube title #shorts",
                "description": "Description here",
                "tags": ["tag"] * 15,
            },
        }

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(mock_data)

            copy, status = generate_copy("Amazing hook", "transcript excerpt", ["instagram"])

        assert status == "generated"
        assert copy.tiktok.hook == "Amazing hook"

    def test_template_tone_included_in_prompt(self):
        """template_tone param should appear in the prompt sent to Gemini."""
        mock_data = {
            "tiktok": {"hook": "h", "caption": "c", "hashtags": ["a"] * 5},
            "instagram": {"hook": "h", "caption": "c", "hashtags": ["a"] * 10},
            "youtube": {"title": "t #shorts", "description": "d", "tags": ["t"] * 15},
        }

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(mock_data)

            generate_copy("hook", "transcript", ["tiktok"], template_tone="casual energetic")

        call_args = mock_client.models.generate_content.call_args
        prompt_text = call_args.kwargs["contents"][0]
        assert "casual energetic" in prompt_text

    def test_double_failure_returns_template_fallback(self):
        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client, \
             patch("time.sleep"):  # don't sleep through the agent's transient backoff
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.side_effect = Exception("API down")

            copy, status = generate_copy("hook text", "transcript", ["instagram"])

        assert status == "generated_fallback"
        assert "Auto-copy failed" in copy.instagram.caption

    def test_template_copy_contains_all_platforms(self):
        copy = _template_copy("My hook")
        assert copy.tiktok is not None
        assert copy.instagram is not None
        assert copy.youtube is not None

    def test_hashtag_count_enforced(self):
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
