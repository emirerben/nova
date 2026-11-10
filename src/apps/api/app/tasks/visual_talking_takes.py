"""KRI-1340: attach a talk-to-camera Visuals video as one of its Talking item's takes.

Enqueued by `autoplace.analyze_pool_asset` when a Visuals video's analysis turns ready
(``VISUAL_TALKING_TAKES_ENABLED``). Does what the phone does when it attaches footage: hashes
the original (here the pool bytes at their pinned generation), makes the analysis proxy, uploads
it under the thread, and appends the clip row (`visual_talking_takes.take_row`). The rule is
`app.services.visual_talking_takes`; this module is only its I/O.

Contract: background, idempotent, never fatal. Every condition is re-checked under the item
lock right before the write, so a redelivered or racing run adds the take at most once, and a
Visual removed meanwhile is never adopted. Cost: one download + one ffmpeg proxy encode, no
model call (the Visual's own analysis is reused); the footage understanding pass adds the
take's timed speech like any attach.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from app import storage
from app.config import settings
from app.database import sync_session
from app.kria.media_sources import MediaUploadContract
from app.models import CreationThread, PlanItem, PlanItemAsset
from app.pipeline.probe import ProbeError, probe_video
from app.services.phone_rollout import (
    TALKING_MULTI_CLIP_MAX_CLIPS,
    phone_talking_multi_clip_supported,
)
from app.services.phone_visuals import MAX_PHONE_VISUAL_VIDEO_BYTES, probed_phone_video
from app.services.visual_talking_takes import (
    proxy_media_id,
    proxy_video_filter,
    same_bytes_as_a_clip,
    skip_reason,
    take_row,
)
from app.worker import celery_app

log = structlog.get_logger()

_FFMPEG_TIMEOUT_S = 240


def _skip(asset: PlanItemAsset, item: PlanItem) -> str | None:
    return skip_reason(
        enabled=settings.visual_talking_takes_enabled,
        edit_format=getattr(item, "edit_format", None),
        multi_clip_supported=phone_talking_multi_clip_supported(),
        has_voiceover=bool(getattr(item, "voiceover_gcs_path", None)),
        asset_kind=asset.kind,
        asset_status=asset.status,
        analysis=asset.analysis,
        asset_id=str(asset.id),
        clip_assignments=item.clip_assignments,
        max_clips=TALKING_MULTI_CLIP_MAX_CLIPS,
    )


def _encode_proxy(source: Path, target: Path, *, fps: float, has_audio: bool) -> None:
    """The phone's analysis-proxy shape: upright pixels, long side 568, <= 30 fps."""
    command = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map_metadata",
        "-1",
        "-vf",
        proxy_video_filter(fps),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        *(["-c:a", "aac", "-b:a", "96k"] if has_audio else ["-an"]),
        "-movflags",
        "+faststart",
        str(target),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=_FFMPEG_TIMEOUT_S)


def _prepare(
    asset_path: str, generation: str, user_id: str, thread_id: str, asset_id: str
) -> tuple[str, str, str, dict[str, Any], dict[str, Any]] | str:
    """Download, hash, probe and proxy the take. Returns (media id, proxy path, proxy
    generation, proxy descriptor, original descriptor), or a skip reason."""
    with tempfile.TemporaryDirectory(prefix="kria_visual_take_") as directory:
        local = Path(directory) / "original"
        storage.download_generation_to_file(asset_path, str(local), generation=generation)
        size = local.stat().st_size
        if not 0 < size <= MAX_PHONE_VISUAL_VIDEO_BYTES:
            return "size_out_of_bounds"
        with local.open("rb") as handle:
            sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
        try:
            shape = probed_phone_video(local)  # the same checks a Visuals cutaway passes
            probe = probe_video(str(local))
        except (ValueError, ProbeError):
            return "unreadable_video"
        original = {
            "kind": "video",
            "sha256": sha256,
            "byte_count": size,
            "duration_s": float(shape["duration_s"]),
            "width": int(shape["width"]),
            "height": int(shape["height"]),
            "orientation_degrees": int(shape["orientation_degrees"]),
            "has_audio": bool(probe.has_audio),
        }
        proxy_file = Path(directory) / "proxy.mp4"
        try:
            _encode_proxy(local, proxy_file, fps=float(probe.fps or 0), has_audio=probe.has_audio)
            proxy_probe = probe_video(str(proxy_file))
        except (subprocess.SubprocessError, ProbeError, OSError):
            return "proxy_failed"
        proxy = {
            "timing_version": 1,
            "duration_s": float(proxy_probe.duration_s),
            "width": int(proxy_probe.width),
            "height": int(proxy_probe.height),
            "frame_rate": round(min(30.0, max(1.0, float(proxy_probe.fps or 30.0))), 3),
            "orientation_degrees": 0,
        }
        try:
            MediaUploadContract.model_validate(
                {"purpose": "analysis_proxy", "proxy": {**proxy, "original": original}}
            ).proxy.verify_registered(proxy["duration_s"], bool(proxy_probe.has_audio))
        except ValueError:
            return "proxy_timing_mismatch"
        media_id = proxy_media_id(asset_id, generation)
        proxy_path = f"users/{user_id}/creation-threads/{thread_id}/{media_id}"
        try:
            metadata = storage.upload_local_file_immutable(str(proxy_file), proxy_path, "video/mp4")
        except ValueError:
            # An earlier run uploaded this proxy (same Visual, same generation) with
            # different encoder bytes: reuse that object, measured again.
            storage.download_to_file(proxy_path, str(proxy_file))
            proxy_probe = probe_video(str(proxy_file))
            proxy["duration_s"] = float(proxy_probe.duration_s)
            metadata = storage.object_metadata(proxy_path)
        return media_id, proxy_path, str(metadata.generation), proxy, original


@celery_app.task(
    name="app.tasks.visual_talking_takes.adopt_visual_talking_take",
    soft_time_limit=600,
    time_limit=660,
)
def adopt_visual_talking_take(asset_id: str) -> str:
    """Attach one ready Visuals video as a Talking take when the rule says it is one."""
    try:
        return _adopt(uuid.UUID(str(asset_id)))
    except Exception:  # noqa: BLE001 - background enhancement; the Visual stays a Visual
        log.warning("visual_talking_take_failed", asset_id=str(asset_id), exc_info=True)
        return "failed"


def _adopt(asset_id: uuid.UUID) -> str:
    with sync_session() as db:
        asset = db.get(PlanItemAsset, asset_id)
        item = db.get(PlanItem, asset.plan_item_id) if asset is not None else None
        if asset is None or item is None:
            return "missing"
        if (reason := _skip(asset, item)) is not None:
            return reason
        thread = db.execute(
            select(CreationThread)
            .where(CreationThread.active_plan_item_id == item.id)
            .order_by(CreationThread.updated_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if thread is None:
            return "no_thread"
        pinned = (str(asset.gcs_path), str(asset.gcs_generation or ""))
        user_id, thread_id = str(asset.user_id), str(thread.id)
    if not pinned[1]:
        return "no_generation"
    prepared = _prepare(pinned[0], pinned[1], user_id, thread_id, str(asset_id))
    if isinstance(prepared, str):
        log.info("visual_talking_take_skipped", asset_id=str(asset_id), reason=prepared)
        return prepared
    media_id, proxy_path, proxy_generation, proxy, original = prepared
    with sync_session() as db:
        asset = db.get(PlanItemAsset, asset_id)
        if asset is None:
            return "missing"
        row = take_row(
            asset=asset,
            media_id=media_id,
            proxy_path=proxy_path,
            proxy_generation=proxy_generation,
            proxy=proxy,
            original=original,
        )
        item_id = asset.plan_item_id
        item = db.get(PlanItem, item_id)
        if item is not None and same_bytes_as_a_clip(
            str(original["sha256"]), item.clip_assignments
        ):
            log.info("visual_talking_take_skipped", asset_id=str(asset_id), reason="duplicate")
            return "duplicate"
    # The timed sentences footage gets (KRI-282), outside the item lock: best effort.
    from app.tasks.kria_clip_understanding import _with_speech_segments  # noqa: PLC0415

    row = _with_speech_segments(
        {**row, "generation": proxy_generation}, owner_id=user_id, item_id=str(item_id)
    )
    row.pop("generation", None)
    return _write(asset_id, pinned, row, str(original["sha256"]))


def _write(asset_id: uuid.UUID, pinned: tuple[str, str], row: dict[str, Any], sha256: str) -> str:
    from app.services.plan_item_media import (  # noqa: PLC0415
        current_detector_policy,
        mutate_plan_item_media,
    )
    from app.services.speech_cleanup_preflight import (  # noqa: PLC0415
        mutation_current_analysis_sync,
    )

    with sync_session() as db:
        asset = db.get(PlanItemAsset, asset_id)
        if asset is None:
            return "missing"
        item = db.execute(
            select(PlanItem).where(PlanItem.id == asset.plan_item_id).with_for_update()
        ).scalar_one_or_none()
        if item is None:
            return "missing"
        db.refresh(asset)
        if (str(asset.gcs_path), str(asset.gcs_generation or "")) != pinned:
            return "visual_changed"
        if (reason := _skip(asset, item)) is not None:
            return reason
        rows = [dict(r) for r in (item.clip_assignments or []) if isinstance(r, dict)]
        if same_bytes_as_a_clip(sha256, rows):
            log.info("visual_talking_take_skipped", asset_id=str(asset_id), reason="duplicate")
            return "duplicate"
        mutate_plan_item_media(
            item,
            detector_policy=current_detector_policy(),
            clip_assignments=[*rows, row],
            current_analysis=mutation_current_analysis_sync(db, item.id, for_update=True),
        )
        thread = db.execute(
            select(CreationThread)
            .where(CreationThread.active_plan_item_id == item.id)
            .order_by(CreationThread.updated_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        item_id = item.id
        retire = (
            (thread.id, thread.creator_id)
            if thread is not None and int(thread.runtime_version or 0) == 2
            else None
        )
        db.commit()
    log.info(
        "visual_talking_take_adopted",
        asset_id=str(asset_id),
        item_id=str(item_id),
        media_id=row["media_id"],
        duration_s=row["duration_s"],
    )
    from app.tasks.kria_clip_understanding import enqueue_clip_understanding  # noqa: PLC0415

    enqueue_clip_understanding(item_id)
    if retire is not None:
        _retire_pending_approvals(*retire)
    return "adopted"


def _retire_pending_approvals(thread_id: uuid.UUID, creator_id: uuid.UUID) -> None:
    """A new take changes the footage like an attach: close approvals made before it."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    from app.kria.runtime import retire_pending_approvals_for_source_change  # noqa: PLC0415

    async def _run() -> None:
        engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                await retire_pending_approvals_for_source_change(
                    db, thread_id=thread_id, creator_id=creator_id
                )
        finally:
            await engine.dispose()

    try:
        asyncio.run(_run())
    except Exception:  # noqa: BLE001 - stale approvals are refused at decide time anyway
        log.warning("visual_talking_take_retire_failed", thread_id=str(thread_id), exc_info=True)


def enqueue_visual_talking_take(asset_id: object) -> None:
    """Fire-and-forget after a Visual's analysis committed; never raises."""
    if not settings.visual_talking_takes_enabled:
        return
    try:
        adopt_visual_talking_take.apply_async(
            args=[str(asset_id)], queue=settings.pool_asset_analysis_queue, expires=3600
        )
    except Exception:  # noqa: BLE001
        log.warning("visual_talking_take_enqueue_failed", asset_id=str(asset_id), exc_info=True)
