"""Bindings for creator-authored, audio-independent visual timelines.

Old narrated jobs never acquire this contract merely because a flag changes.
The confirmed strategy and the immutable proposal must both opt in.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.agents._schemas.creator_agent import canonical_context_hash

GUIDED_VOICEOVER_CONTRACT = "guided_voiceover_v1"
CREATOR_FIDELITY_QUEUE = "creator-fidelity-v1"


def requests_guided_voiceover(strategy: object) -> bool:
    return (
        isinstance(strategy, Mapping)
        and strategy.get("execution_contract") == GUIDED_VOICEOVER_CONTRACT
        and strategy.get("render_program") == "guided"
        and strategy.get("audio_strategy") == "voiceover"
    )


def narration_matches_item(narration: object, item: Any) -> bool:
    """Compare the pinned audio identity to the owner-checked current item."""
    if not isinstance(narration, Mapping):
        return False
    try:
        duration = float(narration.get("duration_s", 0))
        current_duration = float(getattr(item, "voiceover_duration_s", 0) or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        narration.get("gcs_path")
        and narration.get("generation")
        and narration["gcs_path"] == getattr(item, "voiceover_gcs_path", None)
        and str(narration["generation"]) == str(getattr(item, "voiceover_generation", ""))
        and duration > 0
        and abs(duration - current_duration) < 0.001
        and getattr(item, "audio_mode", None) == "voiceover"
    )


def execution_identity(active_plan: Mapping[str, Any], strategy: Mapping[str, Any]) -> dict:
    """Retain the exact confirmed plan and its hashes across the queue boundary."""
    edit_plan = active_plan.get("edit_plan")
    if not isinstance(edit_plan, Mapping) or edit_plan.get("strategy") != dict(strategy):
        raise ValueError("Creator execution strategy differs from its confirmed plan")
    if not active_plan.get("plan_hash"):
        raise ValueError("Creator execution has no confirmed plan identity")
    for key in ("manifest_hash", "context_hash"):
        if not edit_plan.get(key):
            raise ValueError(f"Creator execution has no {key}")
    return {
        "version": 1,
        "plan_hash": active_plan["plan_hash"],
        "manifest_hash": edit_plan["manifest_hash"],
        "context_hash": edit_plan["context_hash"],
        "edit_plan": dict(edit_plan),
        "edit_plan_hash": canonical_context_hash(edit_plan),
    }


def validate_execution_binding(
    guided_snapshot: object, strategy: object, voiceover_path: str | None
) -> bool:
    """Return legacy false or validate the entire opt-in binding, never downgrade."""
    if not requests_guided_voiceover(strategy):
        if (
            isinstance(guided_snapshot, Mapping)
            and guided_snapshot.get("execution_contract") == GUIDED_VOICEOVER_CONTRACT
        ):
            raise ValueError("The confirmed voiceover execution strategy is missing")
        return False
    if not isinstance(guided_snapshot, Mapping):
        raise ValueError("The confirmed voiceover visual plan is missing")
    approved = guided_snapshot.get("approved_proposal")
    narration = approved.get("narration") if isinstance(approved, Mapping) else None
    if (
        guided_snapshot.get("execution_contract") != GUIDED_VOICEOVER_CONTRACT
        or not isinstance(narration, Mapping)
        or not narration.get("generation")
        or narration.get("gcs_path") != voiceover_path
    ):
        raise ValueError("The confirmed voiceover identity changed")
    identity = guided_snapshot.get("creator_execution_identity")
    if not isinstance(identity, Mapping):
        raise ValueError("The confirmed creator execution identity is missing")
    edit_plan = identity.get("edit_plan")
    if (
        not isinstance(edit_plan, Mapping)
        or not identity.get("plan_hash")
        or canonical_context_hash(edit_plan) != identity.get("edit_plan_hash")
        or edit_plan.get("strategy") != strategy
        or any(identity.get(key) != edit_plan.get(key) for key in ("manifest_hash", "context_hash"))
    ):
        raise ValueError("The confirmed creator execution plan changed")
    return True
