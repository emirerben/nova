"""Test: GPT-4o fails twice → template copy, copy_status=generated_fallback."""

from unittest.mock import patch

from app.pipeline.agents.copy_writer import generate_copy


def test_copy_fallback_on_double_api_failure():
    with patch("app.pipeline.agents.copy_writer.OpenAI") as mock_openai:
        mock_client = mock_openai.return_value
        mock_client.beta.chat.completions.parse.side_effect = [
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
    from app.pipeline.agents.copy_writer import InstagramCopy, PlatformCopy, TikTokCopy, YouTubeCopy

    mock_copy = PlatformCopy(
        tiktok=TikTokCopy(hook="h", caption="c", hashtags=["a"] * 5),
        instagram=InstagramCopy(hook="h", caption="c", hashtags=["a"] * 10),
        youtube=YouTubeCopy(title="t #shorts", description="d", tags=["t"] * 15),
    )

    with patch("app.pipeline.agents.copy_writer.OpenAI") as mock_openai:
        mock_client = mock_openai.return_value
        mock_client.beta.chat.completions.parse.return_value.__class__ = object
        mock_client.beta.chat.completions.parse.return_value = type(
            "R", (), {"choices": [type("C", (), {"message": type("M", (), {"parsed": mock_copy})()})()]}
        )()
        copy, status = generate_copy("hook text", "transcript", ["instagram"])

    assert status == "generated"
