"""Celery task: send waitlist confirmation email via Resend.

Fire-and-forget — errors are logged but never retried.
The waitlist signup succeeds regardless of email delivery.
"""

import structlog

from app.config import settings
from app.worker import celery_app

log = structlog.get_logger()


@celery_app.task(name="tasks.send_waitlist_confirmation", max_retries=0)
def send_waitlist_confirmation(email: str) -> None:
    """Send a transactional confirmation email to a new waitlist signup.

    Uses Resend's HTTP API directly (no SDK dependency needed).
    If RESEND_API_KEY is not configured, logs a warning and returns.
    """
    api_key = getattr(settings, "resend_api_key", "")
    if not api_key:
        log.warning("resend_api_key_not_configured", email=email)
        return

    import httpx

    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": "Nova <hello@nova.video>",
                "to": [email],
                "subject": "You're on the Nova waitlist",
                "html": _build_email_html(email),
            },
            timeout=10.0,
        )
        response.raise_for_status()
        log.info("waitlist_confirmation_sent", email=email, resend_id=response.json().get("id"))
    except httpx.HTTPStatusError as exc:
        log.error(
            "waitlist_confirmation_failed",
            email=email,
            status_code=exc.response.status_code,
            detail=exc.response.text[:500],
        )
    except Exception as exc:
        log.error("waitlist_confirmation_error", email=email, error=str(exc))


def _build_email_html(email: str) -> str:
    """Minimal HTML email body — value prop + 'we'll reach out when your spot opens'."""
    return f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                max-width: 480px; margin: 0 auto; padding: 40px 20px;">
        <h1 style="font-size: 24px; margin-bottom: 16px;">You're on the list! 🎬</h1>
        <p style="color: #555; line-height: 1.6;">
            Thanks for signing up for <strong>Nova</strong> — the AI tool that transforms
            your raw videos into viral-ready short-form content.
        </p>
        <p style="color: #555; line-height: 1.6;">
            We'll reach out to <strong>{email}</strong> when your spot opens.
            Early access is coming soon.
        </p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 32px 0;" />
        <p style="color: #999; font-size: 12px;">
            Nova — AI-powered raw video to viral short-form content
        </p>
    </div>
    """
