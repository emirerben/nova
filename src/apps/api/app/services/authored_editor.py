"""An explicit creator timeline, independent of the original generated cut.

Only a supported, baseline-checked draft restore may enter this mode. The mode
is sticky: later renders must never re-run an archetype's original assignment.
"""

from __future__ import annotations

import copy
from typing import Any

from fastapi import HTTPException

AUTHORED_TIMELINE_MODE = "authored"


def is_authored_timeline(variant: dict) -> bool:
    return variant.get("editor_timeline_mode") == AUTHORED_TIMELINE_MODE


def explicit_authored_slots(variant: dict) -> list[dict]:
    """No AI, previous recipe, or composite fallback is permitted here."""
    timeline = variant.get("user_timeline")
    rows = timeline.get("slots") if isinstance(timeline, dict) else None
    if not isinstance(rows, list):
        raise ValueError("authored timeline is missing")
    active = [
        copy.deepcopy(row) for row in rows if isinstance(row, dict) and not row.get("removed")
    ]
    if not active:
        raise ValueError("authored timeline is empty")
    return active


def prepare_authored_editor_commit(
    job: Any, variant_id: str, payload: Any, *, validation_arguments: dict[str, Any]
) -> dict:
    """Prepare on a staging copy supplied by the saved-draft transaction.

    The caller has loaded the creator-owned immutable snapshot and checked its
    generation. The ordinary commit validator still owns every section, source
    bound, and generation stamp. Only eligibility dispatch recognizes this mode.
    """
    from app.routes.generative_jobs import (
        _find_variant,
        _prepare_editor_commit,
        variant_render_baseline,
    )
    from app.services.phone_editor import _StagedJob

    original_job = job
    job = _StagedJob(job)
    variant = _find_variant(job, variant_id)
    if variant is None:
        raise HTTPException(404, detail="Variant not found")
    if payload.base_generation != variant_render_baseline(variant):
        raise HTTPException(409, detail="baseline_conflict")
    if not is_authored_timeline(variant) and (
        payload.timeline_slots is None or not any(not row.removed for row in payload.timeline_slots)
    ):
        raise HTTPException(422, detail={"code": "TIMELINE_EMPTY"})
    if payload.timeline_slots is not None and not any(
        not row.removed for row in payload.timeline_slots
    ):
        raise HTTPException(422, detail={"code": "TIMELINE_EMPTY"})
    # A cloud composite is a display-only track. Phone speech segments,
    # however, map to exact original windows; retain only server-derived
    # identities when deleting another segment from that locked track.
    from app.routes.generative_jobs import editor_deletion_timeline

    canonical = {row.get("slot_id"): row for row in editor_deletion_timeline(job, variant)}
    for row in payload.timeline_slots or []:
        if row.removed or not row.slot_id:
            continue
        if row.slot_id.startswith("native-composite-base"):
            raise HTTPException(422, detail={"code": "authored_source_required"})
        if row.slot_id.startswith("native-phone-talking-source"):
            baseline = canonical.get(row.slot_id)
            if (
                baseline is None
                or row.clip_index != baseline["clip_index"]
                or any(
                    value is None or abs(float(value) - float(baseline[key])) > 1e-6
                    for key, value in (("in_s", row.in_s), ("duration_s", row.duration_s))
                )
            ):
                raise HTTPException(409, detail={"code": "TIMELINE_STALE"})
    variant["editor_timeline_mode"] = AUTHORED_TIMELINE_MODE
    if variant.get("render_destination") == "device":
        from app.services.phone_editor import prepare_phone_editor_commit

        prep = prepare_phone_editor_commit(
            job,
            variant_id,
            prepare=lambda staged: _prepare_editor_commit(
                staged, variant_id, payload, **validation_arguments
            ),
        )
    else:
        prep = _prepare_editor_commit(job, variant_id, payload, **validation_arguments)
    prep["authored_timeline"] = True
    for name, value in vars(job).items():
        if name != "_job":
            setattr(original_job, name, value)
    return prep


def resolve_authored_phone_slots(job: Any, variant: dict, slots: list[Any]) -> list[dict]:
    """Resolve explicit placements against the original/admitted receipt catalog."""
    import math
    import uuid

    from app.routes.generative_jobs import _TIMELINE_MAX_SLOTS, TIMELINE_MAX_TOTAL_S
    from app.services.phone_editor_sources import phone_editor_source_revision

    catalog = (phone_editor_source_revision(job, variant) or {}).get("sources") or []
    from app.routes.generative_jobs import editor_deletion_timeline

    current = editor_deletion_timeline(job, variant)
    known = {row.get("slot_id") for row in current}
    resolved = []
    total = 0.0
    for slot in slots:
        if slot.removed:
            continue
        if slot.slot_id is not None and slot.slot_id not in known:
            raise HTTPException(409, detail={"code": "TIMELINE_STALE"})
        if not 0 <= slot.clip_index < len(catalog):
            raise HTTPException(422, detail={"code": "TIMELINE_UNKNOWN_CLIP"})
        source = catalog[slot.clip_index]
        duration = slot.duration_s
        rate = slot.playback_rate or 1.0
        if slot.duration_beats is not None or duration is None:
            raise HTTPException(422, detail={"code": "authored_seconds_required"})
        minimum_duration = (
            0.001
            if slot.slot_id
            and slot.slot_id.startswith("native-phone-talking-source")
            and slot.slot_id in known
            else 0.1
        )
        if (
            not all(math.isfinite(value) for value in (slot.in_s, duration, rate))
            or slot.in_s < 0
            or duration < minimum_duration
        ):
            raise HTTPException(422, detail={"code": "TIMELINE_INVALID_WINDOW"})
        bound = source.get("duration_s")
        if source["kind"] == "video" and (
            bound is None or slot.in_s + duration * rate > float(bound) + 0.001
        ):
            raise HTTPException(422, detail={"code": "TIMELINE_OUT_OF_BOUNDS"})
        if slot.look_preset not in (None, "none") or slot.look_adjustments is not None:
            raise HTTPException(422, detail={"code": "phone_edit_unsupported"})
        total += duration
        resolved.append(
            {
                **slot.model_dump(mode="json"),
                "slot_id": slot.slot_id or str(uuid.uuid4()),
                "source_gcs_path": source["gcs_path"],
                "source_duration_s": bound,
                "media_id": source["media_id"],
                "order": len(resolved),
                "duration_s": duration,
            }
        )
    if not resolved:
        raise HTTPException(422, detail={"code": "TIMELINE_EMPTY"})
    if len(resolved) > _TIMELINE_MAX_SLOTS or total > TIMELINE_MAX_TOTAL_S:
        raise HTTPException(422, detail={"code": "TIMELINE_TOO_LONG"})
    return resolved


def authored_cloud_source_catalog(job: Any, variant: dict) -> list[dict]:
    """Stable guided identities first, then owned job-pool additions in append order."""
    from app.pipeline.image_clip import is_image_file
    from app.routes.generative_jobs import _guided_v2_revision, _timeline_parts

    candidates = job.all_candidates or {}
    revision = (
        _guided_v2_revision(job, variant)
        if variant.get("resolved_archetype") == "guided_story"
        else None
    )
    if revision:
        catalog = [
            {
                "source_gcs_path": row["gcs_path"],
                "source_duration_s": row.get("duration_s"),
                "source_kind": row.get("kind", "video"),
                "media_id": row.get("media_id"),
                "source_generation": row.get("generation"),
            }
            for row in revision["sources"]
        ]
        known_paths = {row["source_gcs_path"] for row in catalog}
        for path in candidates.get("clip_paths") or []:
            if path not in known_paths:
                catalog.append(
                    {
                        "source_gcs_path": path,
                        "source_kind": "image" if is_image_file(path) else "video",
                    }
                )
                known_paths.add(path)
    else:
        catalog = [
            {"source_gcs_path": path, "source_kind": "image" if is_image_file(path) else "video"}
            for path in (job.all_candidates or {}).get("clip_paths") or []
        ]
        ai, user, _ = _timeline_parts(variant)
        for row in [*ai, *user]:
            index = row.get("clip_index")
            if isinstance(index, int) and 0 <= index < len(catalog):
                for key in ("source_gcs_path", "source_duration_s", "source_kind"):
                    if row.get(key) is not None:
                        catalog[index][key] = row[key]
    metadata = candidates.get("editor_source_metadata") or {}
    for row in catalog:
        receipt = metadata.get(row["source_gcs_path"]) or {}
        for key in ("source_kind", "source_generation", "source_duration_s"):
            if row.get(key) is None and receipt.get(key) is not None:
                row[key] = receipt[key]
        if receipt.get("source_kind"):
            row["source_kind"] = receipt["source_kind"]
    return catalog


def authored_cloud_timeline_clips(
    job: Any, variant: dict, *, sign_url, image_preview_paths: dict | None = None
) -> list[dict]:
    """Project exactly the same source indexes accepted by authored Save."""
    from app.routes.generative_jobs import PLAYBACK_URL_TTL_MIN, _native_timeline_source

    used = {
        row.get("clip_index")
        for row in (variant.get("user_timeline") or {}).get("slots") or []
        if not row.get("removed")
    }
    result = []
    for index, source in enumerate(authored_cloud_source_catalog(job, variant)):
        path = source["source_gcs_path"]
        preview_path = (image_preview_paths or {}).get(path, path)
        try:
            url = sign_url(preview_path, PLAYBACK_URL_TTL_MIN)
        except Exception:
            url = None
        result.append(
            {
                "clip_index": index,
                "signed_url": url,
                "native_source": _native_timeline_source(
                    job,
                    path,
                    source.get("media_id") or f"clip-{index}",
                    sign_url=sign_url,
                    variant=variant,
                ),
                "duration_s": source.get("source_duration_s"),
                "kind": source["source_kind"],
                "media_id": source.get("media_id"),
                "generation": source.get("source_generation"),
                "used": index in used,
            }
        )
    return result


def resolve_authored_cloud_slots(job: Any, variant: dict, slots: list[Any]) -> list[dict]:
    """Preserve exact authored source identities and per-occurrence geometry."""
    import math
    import uuid

    from app import storage
    from app.config import settings
    from app.pipeline.look_presets import (
        EDIT_WIDE_LOOK_PRESETS,
        normalize_look_adjustments,
        normalize_look_preset,
    )
    from app.routes.generative_jobs import (
        TIMELINE_MAX_TOTAL_S,
        _durable_sources_prefix,
        _timeline_parts,
        editor_deletion_timeline,
    )

    baseline_rows = editor_deletion_timeline(job, variant)
    baseline = {row.get("slot_id"): row for row in baseline_rows if row.get("slot_id")}
    catalog = authored_cloud_source_catalog(job, variant)
    _, _, grid = _timeline_parts(variant)
    cursor = 0
    resolved = []
    total = 0.0
    for slot in slots:
        if slot.removed:
            continue
        if slot.slot_id is not None and slot.slot_id not in baseline:
            raise HTTPException(409, detail={"code": "TIMELINE_STALE"})
        if not 0 <= slot.clip_index < len(catalog):
            raise HTTPException(422, detail={"code": "TIMELINE_UNKNOWN_CLIP"})
        old = baseline.get(slot.slot_id) or {}
        source = catalog[slot.clip_index]
        # Absent means preserve; explicit null resets crop/rate/layout.
        controls = {}
        for key, default in (
            ("playback_rate", 1.0),
            ("source_crop", None),
            ("layout", "fullscreen"),
        ):
            value = getattr(slot, key) if key in slot.model_fields_set else old.get(key)
            controls[key] = value if value is not None else default
        rate = float(controls["playback_rate"])
        duration = slot.duration_s
        if slot.duration_beats is not None:
            unchanged = (
                old.get("duration_beats") == slot.duration_beats and old.get("in_s") == slot.in_s
            )
            if unchanged and old.get("duration_s") is not None:
                duration = float(old["duration_s"])
            elif slot.duration_beats > 0 and cursor + slot.duration_beats < len(grid):
                duration = grid[cursor + slot.duration_beats] - grid[cursor]
            else:
                raise HTTPException(422, detail={"code": "TIMELINE_BEATS_EXHAUSTED"})
            cursor += slot.duration_beats
        if (
            duration is None
            or not all(math.isfinite(v) for v in (slot.in_s, duration, rate))
            or slot.in_s < 0
            or duration < 0.1
        ):
            raise HTTPException(422, detail={"code": "TIMELINE_INVALID_WINDOW"})
        bound = source.get("source_duration_s")
        if (
            source["source_kind"] == "video"
            and bound is not None
            and slot.in_s + duration * rate > float(bound) + 0.05
        ):
            raise HTTPException(422, detail={"code": "TIMELINE_OUT_OF_BOUNDS"})
        preset = normalize_look_preset(
            slot.look_preset if slot.look_preset is not None else old.get("look_preset")
        )
        if (
            preset in EDIT_WIDE_LOOK_PRESETS[1:]
            and not settings.edit_wide_looks_enabled
            and preset != normalize_look_preset(old.get("look_preset"))
        ):
            raise HTTPException(422, detail={"code": "LOOK_PRESET_NOT_AVAILABLE"})
        adjustments = (
            slot.look_adjustments
            if "look_adjustments" in slot.model_fields_set
            else old.get("look_adjustments")
            if preset == normalize_look_preset(old.get("look_preset"))
            else None
        )
        normalized = normalize_look_adjustments(preset, adjustments)
        resolved.append(
            {
                **slot.model_dump(mode="json"),
                **source,
                **controls,
                "slot_id": slot.slot_id or str(uuid.uuid4()),
                "duration_s": float(duration),
                "look_preset": preset,
                "look_adjustments": normalized.model_dump() if normalized else None,
                "order": len(resolved),
            }
        )
        total += duration
    if not resolved:
        raise HTTPException(422, detail={"code": "TIMELINE_EMPTY"})
    if total > TIMELINE_MAX_TOTAL_S:
        raise HTTPException(422, detail={"code": "TIMELINE_TOO_LONG"})
    prefix = _durable_sources_prefix(job)
    for path in {row["source_gcs_path"] for row in resolved}:
        if path.startswith(prefix) and not storage.object_exists(path):
            raise HTTPException(422, detail={"code": "sources_expired"})
    return resolved
