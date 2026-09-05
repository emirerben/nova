"""Pure helpers for the Creator render read model."""

from __future__ import annotations

from typing import Any

from app.services.job_status import PLAN_ITEM_JOB_TERMINAL

CREATOR_RENDER_READY_STATUSES = frozenset({"done", "variants_ready", "variants_ready_partial"})


def creator_render_projection_status(job_status: str | None) -> str:
    """Map an authoritative Job status to the public Creator render state."""

    if job_status in CREATOR_RENDER_READY_STATUSES:
        return "ready"
    if job_status in PLAN_ITEM_JOB_TERMINAL:
        return "failed"
    if job_status == "queued":
        return "queued"
    return "rendering"


def build_creator_render_projection(
    *,
    job_status: str,
    job_id: object,
    current_job_id: object | None,
    owner_id: object,
    plan_item_id: object,
    ownership_epoch: int,
    session_id: object | None,
    session_revision: int = 0,
    attempt: int = 0,
    generation_attempt_id: str | None = None,
    variant_id: object | None = None,
    render_generation_id: object | None = None,
) -> dict[str, Any]:
    """Build the complete projection used by both worker and HTTP paths."""

    projection: dict[str, Any] = {
        "status": creator_render_projection_status(job_status),
        "job_id": str(job_id),
        "current_job_id": str(current_job_id) if current_job_id is not None else None,
        "user_id": str(owner_id),
        "plan_item_id": str(plan_item_id),
        "ownership_epoch": ownership_epoch,
        "session_id": str(session_id) if session_id is not None else None,
        "session_revision": session_revision,
        "attempt": attempt,
    }
    if generation_attempt_id:
        projection["generation_attempt_id"] = generation_attempt_id
    if variant_id:
        projection["variant_id"] = str(variant_id)
    if render_generation_id:
        projection["render_generation_id"] = str(render_generation_id)
    return projection


__all__ = ["build_creator_render_projection", "creator_render_projection_status"]
