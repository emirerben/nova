"""Atomic editor admission for timeline-owned split sections."""

from fastapi import HTTPException

from app.kria.split_screen import layout_split_timeline
from app.services.phone_rollout import split_screen_client_supported, split_screen_supported


def require_split_screen_commit(variant, payload):
    saved = (variant.get("user_timeline") or {}).get("split_screen_sections") or []
    proposed = payload.split_screen_sections
    members = any(row.split_section_id is not None for row in payload.timeline_slots or [])
    if not saved and not proposed and not members:
        return
    if not split_screen_client_supported():
        raise HTTPException(
            426,
            detail={
                "code": "client_update_required",
                "feature": "split_screen",
                "minimum_client_protocol": 42,
            },
        )
    if variant.get("render_destination") != "device":
        raise HTTPException(422, detail={"code": "split_screen_requires_device"})
    if proposed and not split_screen_supported(require_client=False):
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
