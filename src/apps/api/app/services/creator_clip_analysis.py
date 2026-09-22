"""Generation-pinned analysis for creator source clips.

This module is deliberately small: proposal building owns concurrency and
durable checkpoints, while this service owns choosing the exact bytes and
deciding whether a stored analysis is safe to reuse.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from app.schemas.edit_proposal import MediaRef
from app.services.clip_understanding import clip_record


def source_media_kind(content_type: str, path: str) -> str:
    if content_type.startswith("image/") or Path(path).suffix.lower() in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".heic",
    }:
        return "image"
    return "video"


def clip_analysis_ready(
    raw: dict[str, Any],
    *,
    kind: str | None = None,
    current_generation: str | None = None,
) -> bool:
    """Return whether ``raw`` contains reusable semantic analysis.

    Probe-only metadata and empty legacy records are intentionally misses. The
    freshness check is kind-aware because the image analyzer has a lower
    version floor than the video analyzer.
    """

    media_kind = kind or str(raw.get("kind") or "video")
    analysis = raw.get("analysis")
    if not isinstance(analysis, dict) or not analysis:
        return False
    if analysis.get("source") in {"probe_only", "stub"}:
        return False

    registered_generation = raw.get("storage_generation")
    entry_generation = raw.get("generation")
    if entry_generation in (None, ""):
        return False
    if registered_generation not in (None, "") and entry_generation not in (None, ""):
        if str(registered_generation) != str(entry_generation):
            return False
    if current_generation is not None:
        expected = registered_generation or entry_generation
        if expected not in (None, "") and str(expected) != str(current_generation):
            return False

    from app.tasks import autoplace  # noqa: PLC0415

    if autoplace.analysis_is_stale(analysis, kind=media_kind):
        return False
    return not clip_record(analysis, kind=media_kind).is_empty()


def analyze_clip_assignment(
    raw: dict[str, Any],
    pool_by_path: dict[str, MediaRef],
    *,
    run_context: Any | None = None,
    require_semantic: bool = False,
) -> tuple[dict, MediaRef]:
    """Analyze one assignment using its registered storage generation.

    A registered generation is an immutable identity. If the object at that
    path has been replaced, fail instead of silently analyzing newer bytes and
    overwriting the assignment's identity.
    """

    from app import storage  # noqa: PLC0415
    from app.tasks import autoplace  # noqa: PLC0415

    entry = dict(raw)
    path = str(entry["gcs_path"])
    media_id = str(entry["media_id"])
    metadata = storage.object_metadata(path)
    current_generation = str(metadata.generation)
    registered_generation = entry.get("storage_generation")
    if registered_generation not in (None, "") and str(registered_generation) != current_generation:
        raise ValueError("registered clip generation changed")

    kind = source_media_kind(str(metadata.content_type or ""), path)
    pooled = pool_by_path.get(path)
    if (
        pooled is not None
        and str(pooled.generation) == current_generation
        and clip_analysis_ready(
            {
                "generation": pooled.generation,
                "storage_generation": pooled.generation,
                "analysis": pooled.analysis,
            },
            kind=pooled.kind,
            current_generation=current_generation,
        )
    ):
        ref = pooled.model_copy(update={"lane": "clip", "media_id": media_id})
        entry.update(
            {
                "generation": current_generation,
                "kind": ref.kind,
                "duration_s": ref.duration_s,
                "aspect": ref.aspect,
                "analysis": ref.analysis,
            }
        )
        return entry, ref

    cache_generation = entry.get("generation")
    cached = entry.get("analysis") if str(cache_generation) == current_generation else None
    analysis = dict(cached) if isinstance(cached, dict) else {}
    cache_is_probe_only = analysis.get("source") == "probe_only"
    cache_is_legacy_fallback = cache_is_probe_only and bool(analysis.get("analysis_version"))
    if not clip_analysis_ready(
        {
            "generation": cache_generation,
            "storage_generation": registered_generation,
            "analysis": analysis,
        },
        kind=kind,
        current_generation=current_generation,
    ) and not (cache_is_legacy_fallback and not require_semantic):
        analysis = {}

    duration = entry.get("duration_s")
    aspect = entry.get("aspect")
    if not analysis:
        with tempfile.TemporaryDirectory(prefix="edit-proposal-clip-") as tmpdir:
            local = os.path.join(tmpdir, Path(path).name or "media")
            storage.download_generation_to_file(path, local, generation=current_generation)
            if kind == "image":
                if run_context is None:
                    result, aspect, dims, has_alpha = autoplace.analyze_pool_image(local, media_id)
                else:
                    result, aspect, dims, has_alpha = autoplace.analyze_pool_image(
                        local, media_id, run_context=run_context
                    )
                analysis = result or {}
                if dims:
                    analysis.update({"width": dims[0], "height": dims[1]})
                analysis["has_alpha"] = has_alpha
            else:
                if run_context is None:
                    result, aspect, duration, dims = autoplace.analyze_pool_video(local)
                else:
                    result, aspect, duration, dims = autoplace.analyze_pool_video(
                        local, run_context=run_context
                    )
                analysis = result or {}
                if dims:
                    analysis.update({"width": dims[0], "height": dims[1]})
                analysis.setdefault("analysis_version", autoplace.ANALYSIS_VERSION)
                analysis.setdefault("source", "probe_only")

        final_metadata = storage.object_metadata(path)
        if str(final_metadata.generation) != current_generation:
            raise ValueError("clip generation changed during analysis")

    entry.update(
        {
            "generation": current_generation,
            "kind": kind,
            "duration_s": duration,
            "aspect": aspect,
            "analysis": analysis,
        }
    )
    raw_name = Path(path).name
    ref = MediaRef(
        lane="clip",
        media_id=media_id,
        gcs_path=path,
        generation=current_generation,
        kind=kind,
        source_filename=raw_name.split("-", 1)[-1],
        duration_s=float(duration) if duration else None,
        aspect=float(aspect) if aspect else None,
        user_context=str(entry.get("user_note") or ""),
        analysis=analysis,
    )
    return entry, ref
