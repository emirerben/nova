"""Shared runtime budgets for Creator-guided proposal planning."""

from typing import Any

from app.schemas.edit_proposal import (
    MixedMediaTimingProfile,
    uses_quick_photo_long_video_timing,
)

EDIT_PROPOSAL_TASK_SOFT_TIME_LIMIT_S = 1440
EDIT_PROPOSAL_TASK_HARD_TIME_LIMIT_S = 1500
CREATOR_EXECUTION_RECEIPT_LEASE_S = 1650
# Rolling-deploy fence for proposals whose persisted cut schema permits video
# holds above the legacy 1.2s ceiling. Old workers do not consume this queue;
# once the new worker is live it owns both planning and the resulting render.
MIXED_MEDIA_CREATOR_QUEUE = "creator-guided-jobs"


def edit_proposal_task_id(generation_attempt_id: str) -> str:
    """Stable broker identity for queue-aware Creator reconciliation."""

    return f"edit-proposal-{generation_attempt_id}"


def queue_for_mixed_media_timing(value: Any, *, default_queue: str) -> str:
    """Fence expanded timing plans onto workers that understand their schema."""

    try:
        profile = (
            value
            if isinstance(value, MixedMediaTimingProfile)
            else MixedMediaTimingProfile.model_validate(value)
        )
    except Exception:  # noqa: BLE001 - normal proposal validation owns malformed payloads
        return default_queue
    if uses_quick_photo_long_video_timing(profile):
        return MIXED_MEDIA_CREATOR_QUEUE
    return default_queue


def queue_for_guided_contract(
    mixed_media_timing: Any,
    montage_cadence: Any,
    *,
    default_queue: str,
) -> str:
    """Fence every new guided timing contract onto current-version workers."""

    if montage_cadence is not None:
        return MIXED_MEDIA_CREATOR_QUEUE
    return queue_for_mixed_media_timing(mixed_media_timing, default_queue=default_queue)


if EDIT_PROPOSAL_TASK_SOFT_TIME_LIMIT_S >= EDIT_PROPOSAL_TASK_HARD_TIME_LIMIT_S:
    raise RuntimeError("edit proposal soft limit must stay below its hard limit")
if CREATOR_EXECUTION_RECEIPT_LEASE_S <= EDIT_PROPOSAL_TASK_HARD_TIME_LIMIT_S:
    raise RuntimeError("creator execution lease must exceed the proposal task hard limit")
