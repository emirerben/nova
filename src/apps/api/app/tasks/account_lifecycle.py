"""Celery tasks backing account deletion (privacy-policy §9 / DELETE /me/account).

send_account_deletion_email  — fire-and-forget confirmation email (Resend), mirrors
                                the pattern in tasks/email.py.
purge_user_storage           — async GCS walk deleting everything under
                                users/{user_id}/ and generative-jobs/{job_id}/ for
                                each of the user's jobs, plus a best-effort delete
                                of each job's raw_storage_path (covers the legacy
                                dev-user/ and {user_id}/{job_id}/ upload shapes that
                                predate the users/ prefix — see infra/README.md).

Split from the request path (routes/me.py) because a user with a large media
footprint could hold the request past FastAPI's own timeout — the DB rows are
deleted synchronously (cheap), the GCS bytes are swept here (potentially slow).
"""

from __future__ import annotations

import structlog

from app.config import settings
from app.worker import celery_app

log = structlog.get_logger()


@celery_app.task(name="tasks.send_account_deletion_email", max_retries=0)
def send_account_deletion_email(email: str, confirm_token: str) -> None:
    """Email the one-time confirmation code for POST /me/account/delete-confirm.

    Uses Resend's HTTP API directly, matching tasks/email.py's waitlist pattern.
    If RESEND_API_KEY is not configured, logs a warning and returns — deletion is
    still blocked without the code (fails closed, not open).
    """
    api_key = getattr(settings, "resend_api_key", "")
    if not api_key:
        log.warning("resend_api_key_not_configured", email=email, task="account_deletion")
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
                "from": "Kria <hello@usekria.com>",
                "to": [email],
                "subject": "Confirm your Kria account deletion",
                "html": _build_email_html(confirm_token),
            },
            timeout=10.0,
        )
        response.raise_for_status()
        log.info("account_deletion_email_sent", email=email, resend_id=response.json().get("id"))
    except httpx.HTTPStatusError as exc:
        log.error(
            "account_deletion_email_failed",
            email=email,
            status_code=exc.response.status_code,
            detail=exc.response.text[:500],
        )
    except Exception as exc:  # noqa: BLE001 — fire-and-forget, never retried
        log.error("account_deletion_email_error", email=email, error=str(exc))


def _build_email_html(confirm_token: str) -> str:
    return f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                max-width: 480px; margin: 0 auto; padding: 40px 20px;">
        <h1 style="font-size: 22px; margin-bottom: 16px;">Confirm account deletion</h1>
        <p style="color: #555; line-height: 1.6;">
            Someone (hopefully you) asked to permanently delete your Kria account and
            all associated data — your persona, content plans, uploaded footage, and
            rendered videos. This cannot be undone.
        </p>
        <p style="color: #555; line-height: 1.6;">
            To confirm, paste this code back into the account deletion screen. It
            expires in 1 hour.
        </p>
        <p style="font-family: monospace; font-size: 13px; word-break: break-all;
                   background: #f4f4f5; padding: 12px; border-radius: 8px;">
            {confirm_token}
        </p>
        <p style="color: #999; font-size: 12px;">
            If you didn't request this, ignore this email — your account is unaffected.
        </p>
    </div>
    """


@celery_app.task(
    name="tasks.purge_user_storage",
    bind=True,
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=1500,
    time_limit=1740,
)
def purge_user_storage(
    self, user_id: str, job_ids: list[str], raw_storage_paths: list[str]
) -> dict:
    """Delete every GCS object belonging to a deleted user.

    Runs AFTER the DB rows are already gone (routes/me.py commits the DB delete
    before dispatching this), so the ids passed in are the only record of what to
    clean up — they can't be re-derived from the database once this runs.

    Sweeps three targets:
      - users/{user_id}/            (plan clips, plan-pool, seed batches — never
                                       lifecycle-swept, see infra/gcs-lifecycle.json)
      - generative-jobs/{job_id}/    for each job (lifecycle-exempt outputs + sources)
      - each job's raw_storage_path directly (covers legacy dev-user/ and
        {user_id}/{job_id}/ shapes that predate the users/ prefix)

    Best-effort throughout — a partial failure costs orphaned storage, never
    retried automatically (retrying a delete is safe, but a stuck GCS outage
    shouldn't hold a Celery worker slot). Logs counts for observability.
    """
    from app.storage import delete_object_best_effort, delete_prefix_best_effort  # noqa: PLC0415

    user_deleted = delete_prefix_best_effort(f"users/{user_id}/")

    job_deleted = 0
    for job_id in job_ids:
        job_deleted += delete_prefix_best_effort(f"generative-jobs/{job_id}/")

    raw_deleted = 0
    for path in raw_storage_paths:
        if path and delete_object_best_effort(path):
            raw_deleted += 1

    log.info(
        "purge_user_storage_done",
        user_id=user_id,
        job_count=len(job_ids),
        user_prefix_objects_deleted=user_deleted,
        job_prefix_objects_deleted=job_deleted,
        raw_paths_deleted=raw_deleted,
    )
    return {
        "user_prefix_objects_deleted": user_deleted,
        "job_prefix_objects_deleted": job_deleted,
        "raw_paths_deleted": raw_deleted,
    }
