"""Pin approved Visuals-pool photos to exact bytes for phone renders (KRI-121).

Only the phone-planning worker binds. The editor and the device-render grant
reuse the persisted ``PhoneVisualBinding`` receipts and never re-hash.
"""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from google.api_core.exceptions import NotFound
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import storage
from app.models import Job, PlanItemAsset
from app.services.phone_sources import PhoneVisualBinding

# Mirrors the pool upload cap (routes.plan_items._MAX_POOL_IMAGE_BYTES).
# Importing the route module from a service would invert the dependency.
MAX_PHONE_VISUAL_BYTES = 25 * 1024 * 1024


def _timeline_photos(story_timeline: Iterable[Any]) -> dict[str, tuple[str, str]]:
    """Map each timeline photo to its one pinned (path, generation).

    Only photos the story actually shows are bound: the pool can hold up to
    100 large visuals, and editor additions fail closed without a receipt.
    """
    photos: dict[str, tuple[str, str]] = {}
    for moment in story_timeline:
        if moment.lane != "asset" or moment.kind != "image":
            continue
        pin = (moment.gcs_path, moment.generation)
        if photos.setdefault(moment.media_id, pin) != pin:
            raise ValueError("one phone photo cannot pin two sources")
    return photos


def _owned_pool_path(path: str, prefix: str) -> bool:
    return (
        path.startswith(prefix)
        and "\\" not in path
        and all(part not in {"", ".", ".."} for part in path[len(prefix) :].split("/"))
    )


def bind_phone_visuals(
    open_session: Callable[[], AbstractContextManager[Session]],
    *,
    job_id: str,
    story_timeline: Iterable[Any],
) -> tuple[PhoneVisualBinding, ...]:
    """Hash each timeline photo at its approved generation.

    Rows are re-checked against the job's owner and plan item in one short
    session that closes before any storage I/O, the same split as the music
    bed. Raises ``ValueError`` for any stale, foreign, or unreadable photo.
    """
    photos = _timeline_photos(story_timeline)
    if not photos:
        return ()
    ids: dict[str, uuid.UUID] = {}
    for media_id in photos:
        try:
            ids[media_id] = uuid.UUID(media_id)
        except ValueError as exc:
            raise ValueError("phone photo identity is invalid") from exc
        # Manifest identity keys compare this string, so one row gets one spelling.
        if str(ids[media_id]) != media_id:
            raise ValueError("phone photo identity is invalid")
    with open_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None or job.content_plan_item_id is None:
            raise ValueError("phone photos require the job's plan item")
        plan_item_id, user_id = job.content_plan_item_id, job.user_id
        rows = {
            str(row.id): (
                row.plan_item_id,
                row.user_id,
                row.status,
                row.kind,
                row.gcs_path,
                str(row.gcs_generation or ""),
            )
            for row in db.execute(select(PlanItemAsset).where(PlanItemAsset.id.in_(ids.values())))
            .scalars()
            .all()
        }
    prefix = f"users/{user_id}/plan/{plan_item_id}/pool/"
    for media_id, (path, generation) in photos.items():
        if rows.get(media_id) != (plan_item_id, user_id, "ready", "image", path, generation) or (
            not _owned_pool_path(path, prefix)
        ):
            raise ValueError("approved photo is no longer this story's ready visual")
    visuals = []
    with tempfile.TemporaryDirectory(prefix="kria_visual_") as directory:
        for index, (media_id, (path, generation)) in enumerate(photos.items()):
            local = Path(directory) / f"visual-{index}"
            try:
                storage.download_generation_to_file(path, str(local), generation=generation)
            except (FileNotFoundError, NotFound) as exc:
                raise ValueError("approved photo bytes are unavailable") from exc
            size = local.stat().st_size
            if not 0 < size <= MAX_PHONE_VISUAL_BYTES:
                raise ValueError("approved photo size is out of bounds")
            with local.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            local.unlink()
            visuals.append(
                PhoneVisualBinding(
                    media_id=media_id,
                    gcs_path=path,
                    generation=generation,
                    sha256=digest,
                    byte_count=size,
                )
            )
    return tuple(visuals)
