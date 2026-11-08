"""Atomic editor admission for timeline-owned split sections."""

from fastapi import HTTPException

from app.kria.split_screen import layout_split_timeline
from app.services.phone_rollout import split_screen_client_supported, split_screen_supported


def require_split_screen_commit(variant, payload):
    saved = (variant.get("user_timeline") or {}).get("split_screen_sections") or []
    proposed = payload.split_screen_sections
    members = any(row.split_section_id is not None for row in payload.timeline_slots or [])
    saved_policies = any(
        row.get("speaker_frame_override") is not None
        for row in (variant.get("user_timeline") or {}).get("slots") or []
    )
    proposed_policies = any(
        row.speaker_frame_override is not None for row in payload.timeline_slots or []
    )
    if not saved and not proposed and not members and not saved_policies and not proposed_policies:
        return
    if not split_screen_client_supported():
        raise HTTPException(
            426,
            detail={
                "code": "client_update_required",
                "feature": "split_screen",
                "minimum_client_protocol": 46,
            },
        )
    if variant.get("render_destination") != "device":
        raise HTTPException(422, detail={"code": "split_screen_requires_device"})
    if (proposed or proposed_policies or saved_policies) and not split_screen_supported(
        require_client=False
    ):
        raise HTTPException(422, detail={"code": "split_screen_unavailable"})
    if proposed is not None and payload.timeline_slots is None:
        raise HTTPException(422, detail={"code": "split_screen_requires_timeline"})
    if payload.timeline_slots is not None:
        try:
            layout_split_timeline(
                [row.model_dump(mode="json") for row in payload.timeline_slots],
                proposed if proposed is not None else saved,
            )
        except ValueError as exc:
            raise HTTPException(
                422, detail={"code": "invalid_split_screen", "message": str(exc)}
            ) from exc


def authored_split_speaker_background_available(job, variant):
    """Only receipt-backed speaker occurrences in a verified split-origin authored edit."""
    if (
        variant.get("render_destination") != "device"
        or variant.get("editor_timeline_mode") != "authored"
        or variant.get("editor_state") == "empty"
        or not split_screen_supported()
    ):
        return False
    timeline = variant.get("user_timeline") or {}
    rows = [row for row in timeline.get("slots") or [] if not row.get("removed")]
    if not rows or not (
        timeline.get("split_screen_sections") or variant.get("editor_split_screen_origin") is True
    ):
        return False
    from app.services.kria_editor_ops import copy_source_occurrence
    from app.services.phone_editor_sources import (
        editor_source_bindings,
        phone_editor_source_revision,
    )
    from app.services.phone_sources import PHONE_SOURCES_FIELD

    try:
        if timeline.get("split_screen_sections"):
            layout_split_timeline(rows, timeline["split_screen_sections"])
        catalog = (phone_editor_source_revision(job, variant) or {}).get("sources") or []
        originals = {
            (row["media_id"], row["proxy_path"], row["generation"])
            for row in (job.assembly_plan or {}).get(PHONE_SOURCES_FIELD) or []
        }
        originals.update(
            (binding.media_id, binding.proxy_path, binding.generation)
            for binding in editor_source_bindings(variant)
        )
        for row in rows:
            index = row.get("clip_index")
            if (
                copy_source_occurrence(row).get("speaker_subject") is not True
                or isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < len(catalog)
            ):
                continue
            source = catalog[index]
            if (
                source.get("kind") == "video"
                and (source.get("media_id"), source.get("gcs_path"), source.get("generation"))
                in originals
            ):
                return True
    except (ValueError, KeyError, TypeError, AttributeError):
        return False
    return False
