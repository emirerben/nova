"""Shared admission and reference checks for temporary upload cleanup."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select, text

from app.models import TemporaryMediaUpload


def upload_is_available(receipt: TemporaryMediaUpload | None) -> bool:
    return receipt is None or (
        receipt.status in {"reserved", "adopted"}
        and receipt.cleanup_claimed_at is None
        and receipt.deleted_at is None
    )


async def require_upload_available(db, owner_id, path: str, *, renewing: bool = False):
    receipt = await db.scalar(
        select(TemporaryMediaUpload)
        .where(TemporaryMediaUpload.user_id == owner_id, TemporaryMediaUpload.object_path == path)
        .with_for_update()
    )
    if not upload_is_available(receipt) or (
        renewing and receipt is not None and receipt.status == "adopted"
    ):
        raise HTTPException(409, "Upload target is no longer available; upload a new source")
    return receipt


def owner_path_has_reference(db, owner_id, path: str) -> bool:
    """Use the existing retention path matcher, including stored signed URLs.

    These are the same reference-bearing tables fenced by migration 0103.
    Library sources are checked separately by the caller. Keeping any owner's
    Job binding protects independent editable posts, including legacy editors.
    """
    return bool(
        db.scalar(
            text("""SELECT EXISTS (
                SELECT 1 FROM jobs r WHERE r.user_id = :owner
                  AND project_media_row_names_path_0103(to_jsonb(r), :path)
                UNION ALL
                SELECT 1 FROM job_clips r JOIN jobs j ON j.id = r.job_id
                  WHERE j.user_id = :owner
                  AND project_media_row_names_path_0103(to_jsonb(r), :path)
                UNION ALL
                SELECT 1 FROM plan_items r JOIN content_plans p ON p.id = r.content_plan_id
                  WHERE p.user_id = :owner
                  AND project_media_row_names_path_0103(to_jsonb(r), :path)
                UNION ALL
                SELECT 1 FROM plan_item_assets r WHERE r.user_id = :owner
                  AND project_media_row_names_path_0103(to_jsonb(r), :path)
                UNION ALL
                SELECT 1 FROM content_plans r WHERE r.user_id = :owner
                  AND project_media_row_names_path_0103(to_jsonb(r), :path)
                UNION ALL
                SELECT 1 FROM personas r WHERE r.user_id = :owner
                  AND project_media_row_names_path_0103(to_jsonb(r), :path)
                UNION ALL
                SELECT 1 FROM tiktok_publications r WHERE r.user_id = :owner
                  AND project_media_row_names_path_0103(to_jsonb(r), :path)
            )"""),
            {"owner": owner_id, "path": path},
        )
    )
