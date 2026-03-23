"""Tests for send_waitlist_confirmation Celery task."""

from unittest.mock import MagicMock, patch

import pytest


def test_email_skipped_when_no_api_key():
    """When RESEND_API_KEY is empty, task logs warning and returns without sending."""
    with patch("app.tasks.email.settings") as mock_settings:
        mock_settings.resend_api_key = ""

        from app.tasks.email import send_waitlist_confirmation

        # Should not raise — just log and return
        send_waitlist_confirmation("test@example.com")


def test_email_sent_successfully():
    """Happy path: Resend API returns 200 → email sent, logged."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "resend-123"}
    mock_response.raise_for_status = MagicMock()

    with (
        patch("app.tasks.email.settings") as mock_settings,
        patch("app.tasks.email.httpx.post", return_value=mock_response) as mock_post,
    ):
        mock_settings.resend_api_key = "re_test_key"

        from app.tasks.email import send_waitlist_confirmation

        send_waitlist_confirmation("user@example.com")

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[1]["json"]["to"] == ["user@example.com"]
        assert "Nova waitlist" in call_args[1]["json"]["subject"]


def test_email_api_failure_logged_not_raised():
    """Resend API returns 500 → error logged, no exception raised."""
    import httpx

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=mock_response)
    )

    with (
        patch("app.tasks.email.settings") as mock_settings,
        patch("app.tasks.email.httpx.post", return_value=mock_response),
    ):
        mock_settings.resend_api_key = "re_test_key"

        from app.tasks.email import send_waitlist_confirmation

        # Should NOT raise — fire-and-forget, errors are logged
        send_waitlist_confirmation("user@example.com")


def test_email_network_error_logged_not_raised():
    """Network error → logged, no exception raised."""
    with (
        patch("app.tasks.email.settings") as mock_settings,
        patch("app.tasks.email.httpx.post", side_effect=Exception("Connection refused")),
    ):
        mock_settings.resend_api_key = "re_test_key"

        from app.tasks.email import send_waitlist_confirmation

        # Should NOT raise
        send_waitlist_confirmation("user@example.com")


def test_email_html_contains_email():
    """Email HTML body includes the recipient's email address."""
    from app.tasks.email import _build_email_html

    html = _build_email_html("hello@test.com")
    assert "hello@test.com" in html
    assert "Nova" in html
