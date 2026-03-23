"""Test: Gemini fails twice → template copy, copy_status=generated_fallback."""

from unittest.mock import patch

from app.pipeline.agents.copy_writer import generate_copy


def test_copy_fallback_on_double_api_failure():
    with patch("app.pipeline.agents.copy_writer._get_client") as mock_get_client:
        mock_client = mock_get_client.return_value
        mock_client.models.generate_content.side_effect = [
            Exception("first failure"),
            Exception("second failure"),
        ]
        copy, status = generate_copy(
            hook_text="Something went wrong",
            transcript_excerpt="",
            platforms=["instagram", "youtube"],
        )

    assert status == "generated_fallback"
    assert "Auto-copy failed" in copy.instagram.caption
    assert "Auto-copy failed" in copy.tiktok.caption
    assert "Auto-copy failed" in copy.youtube.description


def test_copy_status_generated_on_success():
    import json

    mock_data = {
        "tiktok": {"hook": "h", "caption": "c", "hashtags": ["a"] * 5},
        "instagram": {"hook": "h", "caption": "c", "hashtags": ["a"] * 10},
        "youtube": {"title": "t #shorts", "description": "d", "tags": ["t"] * 15},
    }

    with patch("app.pipeline.agents.copy_writer._get_client") as mock_get_client:
        mock_client = mock_get_client.return_value
        response = mock_client.models.generate_content.return_value
        response.text = json.dumps(mock_data)
        copy, status = generate_copy("hook text", "transcript", ["instagram"])

    assert status == "generated"
