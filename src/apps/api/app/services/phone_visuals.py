"""Pin approved Visuals-pool photos and videos to exact bytes for phone renders
(KRI-121).

Only the phone-planning worker binds. The editor and the device-render grant
reuse the persisted ``PhoneVisualBinding`` receipts and never re-hash.
"""

from __future__ import annotations

import hashlib
import math
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
from app.pipeline.probe import ProbeError, probe_video
from app.services.phone_sources import PhoneVisualBinding

# Mirror the pool upload caps (routes.plan_items._MAX_POOL_IMAGE_BYTES and
# _MAX_POOL_VIDEO_BYTES). Importing the route module from a service would
# invert the dependency.
MAX_PHONE_VISUAL_BYTES = 25 * 1024 * 1024
MAX_PHONE_VISUAL_VIDEO_BYTES = 512 * 1024 * 1024
# The device recipe's 30-minute source bound, which footage originals
# (OriginalMediaDescriptor) already carry. The pool itself caps only bytes.
MAX_PHONE_VISUAL_VIDEO_S = 1800

# What the iPhone engine can compose with no conversion step: the leading
# ISO-BMFF boxes VisualVideoFile.fileExtension accepts, and 4:2:0 H.264/HEVC,
# which AVComposition decodes in hardware.
PHONE_VIDEO_LEADING_BOXES = frozenset(
    {b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"}
)
PHONE_VIDEO_PIX_FMTS = {
    "h264": frozenset({"yuv420p", "yuvj420p"}),
    "hevc": frozenset({"yuv420p", "yuvj420p", "yuv420p10le"}),
}

_NOUNS = {"image": "photo", "video": "video"}


def phone_composable_video(codec: str | None, pix_fmt: str | None) -> bool:
    """Whether the iPhone engine can compose a video of this codec and pixel format.

    ``None`` is a fact nobody recorded (a pool row analyzed before autoplace
    kept these) and passes; binding re-checks the exact bytes as the backstop.
    """
    if codec is None:
        return pix_fmt is None or any(pix_fmt in fmts for fmts in PHONE_VIDEO_PIX_FMTS.values())
    fmts = PHONE_VIDEO_PIX_FMTS.get(codec)
    return fmts is not None and (pix_fmt is None or pix_fmt in fmts)


def _timeline_visuals(
    story_timeline: Iterable[Any], kinds: frozenset[str]
) -> dict[str, tuple[str, str, str]]:
    """Map each timeline visual to its one pinned (kind, path, generation).

    Only visuals the story actually shows are bound: the pool can hold up to
    100 large visuals, and editor additions fail closed without a receipt. A
    kind outside ``kinds`` is left unbound so the compiler fails closed on it.
    """
    visuals: dict[str, tuple[str, str, str]] = {}
    for moment in story_timeline:
        if moment.lane != "asset" or moment.kind not in kinds:
            continue
        pin = (moment.kind, moment.gcs_path, moment.generation)
        if visuals.setdefault(moment.media_id, pin) != pin:
            raise ValueError(f"one phone {_NOUNS[moment.kind]} cannot pin two sources")
    return visuals


def _owned_pool_path(path: str, prefix: str) -> bool:
    return (
        path.startswith(prefix)
        and "\\" not in path
        and all(part not in {"", ".", ".."} for part in path[len(prefix) :].split("/"))
    )


def _probed_video(local: Path) -> dict[str, Any]:
    """What the compiler needs to treat a pool video like bound footage.

    Coded size plus a right-angle orientation: the convention a device
    original's descriptor uses (AVFoundation naturalSize and preferredTransform),
    which is also what the engine's exact-canvas look guard compares. A file
    the engine could not compose fails here, not after the phone downloads it.
    """
    with local.open("rb") as source:
        header = source.read(8)
    if len(header) < 8 or header[4:8] not in PHONE_VIDEO_LEADING_BOXES:
        raise ValueError("approved video is not an MP4 or MOV file")
    try:
        probe = probe_video(str(local))
    except ProbeError as exc:
        raise ValueError("approved video could not be read") from exc
    # The container duration, as the planner measured it (autoplace stores it
    # as the pool row's duration_s). The engine already clamps each insert to
    # the file's real video track, so binding the shorter stream duration
    # would only make the compiler truncate a window drawn inside the file.
    duration = probe.duration_s
    if not (math.isfinite(duration) and duration > 0 and probe.width > 0 and probe.height > 0):
        raise ValueError("approved video could not be read")
    if not phone_composable_video(probe.codec, probe.pix_fmt):
        raise ValueError("approved video must be H.264 or HEVC with 4:2:0 color")
    # ffprobe reports the display matrix counter-clockwise; preferredTransform's
    # angle, which originals carry, runs clockwise. Phones only write right
    # angles, and AVFoundation would apply anything else on device, so a skewed
    # matrix fails closed instead of being flattened to zero.
    if not math.isfinite(probe.rotation_degrees):
        raise ValueError("approved video rotation is unsupported")
    orientation = -round(probe.rotation_degrees) % 360
    if orientation not in {0, 90, 180, 270}:
        raise ValueError("approved video rotation is unsupported")
    return {
        "duration_s": duration,
        "width": probe.width,
        "height": probe.height,
        "orientation_degrees": orientation,
    }


def bind_phone_visuals(
    open_session: Callable[[], AbstractContextManager[Session]],
    *,
    job_id: str,
    story_timeline: Iterable[Any],
    kinds: frozenset[str] = frozenset({"image"}),
) -> tuple[PhoneVisualBinding, ...]:
    """Hash each timeline photo or video of ``kinds`` at its approved generation.

    Rows are re-checked against the job's owner and plan item in one short
    session that closes before any storage I/O, the same split as the music
    bed. A video is also probed from those exact bytes. Raises ``ValueError``
    for any stale, foreign, or unreadable visual.
    """
    pins = _timeline_visuals(story_timeline, kinds)
    return bind_phone_visual_assets(open_session, job_id=job_id, pins=pins)


def bind_phone_visual_assets(
    open_session: Callable[[], AbstractContextManager[Session]],
    *,
    job_id: str,
    pins: dict[str, tuple[str, str, str]],
) -> tuple[PhoneVisualBinding, ...]:
    """Bind selected ready pool assets without requiring a story timeline.

    Admission uses this same receipt path for editor-added photos and videos.
    Callers pass only server-derived rows; it still rechecks owner, item, exact
    generation, and the pool namespace before downloading any bytes.
    """
    if not pins:
        return ()
    ids: dict[str, uuid.UUID] = {}
    for media_id, (kind, _, _) in pins.items():
        try:
            ids[media_id] = uuid.UUID(media_id)
        except ValueError as exc:
            raise ValueError(f"phone {_NOUNS[kind]} identity is invalid") from exc
        # Manifest identity keys compare this string, so one row gets one spelling.
        if str(ids[media_id]) != media_id:
            raise ValueError(f"phone {_NOUNS[kind]} identity is invalid")
    with open_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None or job.content_plan_item_id is None:
            raise ValueError("phone visuals require the job's plan item")
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
    for media_id, (kind, path, generation) in pins.items():
        if rows.get(media_id) != (plan_item_id, user_id, "ready", kind, path, generation) or (
            not _owned_pool_path(path, prefix)
        ):
            raise ValueError(f"approved {_NOUNS[kind]} is no longer this story's ready visual")
    visuals = []
    with tempfile.TemporaryDirectory(prefix="kria_visual_") as directory:
        for index, (media_id, (kind, path, generation)) in enumerate(pins.items()):
            noun = _NOUNS[kind]
            local = Path(directory) / f"visual-{index}"
            try:
                storage.download_generation_to_file(path, str(local), generation=generation)
            except (FileNotFoundError, NotFound) as exc:
                raise ValueError(f"approved {noun} bytes are unavailable") from exc
            size = local.stat().st_size
            cap = MAX_PHONE_VISUAL_VIDEO_BYTES if kind == "video" else MAX_PHONE_VISUAL_BYTES
            if not 0 < size <= cap:
                raise ValueError(f"approved {noun} size is out of bounds")
            # Streamed: a pool video can be half a gigabyte.
            with local.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            probed = _probed_video(local) if kind == "video" else {}
            # One visual on disk at a time keeps worker temp space bounded.
            local.unlink()
            visuals.append(
                PhoneVisualBinding(
                    media_id=media_id,
                    gcs_path=path,
                    generation=generation,
                    sha256=digest,
                    byte_count=size,
                    kind=kind,
                    **probed,
                )
            )
    return tuple(visuals)
