"""Server-authoritative Smart Captions availability.

The browser persists per-video intent, but it cannot select a creator preset or
bypass rollout gates.  Keep this resolver small so every future generation and
correction entry point can reuse the same decision.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.config import settings
from app.models import CreatorStyleAssignment

SMART_CAPTIONS_EDIT_FORMAT = "subtitled"


@dataclass(frozen=True, slots=True)
class SmartCaptionsCapability:
    available: bool
    reason: str | None
    preset_id: str | None = None
    preset_version: str | None = None
    shadow_preset_id: str | None = None
    shadow_preset_version: str | None = None


def _resolved_shadow(assignment: Any) -> tuple[str, str] | None:
    shadow_id = str(getattr(assignment, "shadow_preset_id", None) or "").strip()
    shadow_version = str(getattr(assignment, "shadow_preset_version", None) or "").strip()
    if not shadow_id and not shadow_version:
        return None
    if not shadow_id or not shadow_version:
        return None
    try:
        from app.smart_edit.presets import load_preset  # noqa: PLC0415

        load_preset(shadow_id, shadow_version)
    except Exception:
        return None
    return shadow_id, shadow_version


def _default_capability() -> SmartCaptionsCapability | None:
    """Fleet-wide fallback for users WITHOUT an assignment row.

    Configured via SMART_CAPTIONS_DEFAULT_PRESET_ID/_VERSION (both required;
    empty = no default, per-assignment canary behavior unchanged). An existing
    row always wins — including ``enabled=false``, which stays a per-creator
    opt-out the default must NOT override. No shadow preset on the default
    path: shadow comparisons remain an explicitly-assigned canary tool.
    """

    preset_id = str(settings.smart_captions_default_preset_id or "").strip()
    preset_version = str(settings.smart_captions_default_preset_version or "").strip()
    if not preset_id or not preset_version:
        return None
    try:
        from app.smart_edit.presets import load_preset  # noqa: PLC0415

        load_preset(preset_id, preset_version)
    except Exception:  # noqa: BLE001 — misconfigured default fails closed
        return None
    return SmartCaptionsCapability(
        True,
        None,
        preset_id=preset_id,
        preset_version=preset_version,
    )


def _resolve_from_assignment(
    *, edit_format: str | None, assignment: Any | None
) -> SmartCaptionsCapability:
    """Apply the same server-owned gate ladder for async routes and sync tasks."""

    if not settings.smart_captions_enabled:
        return SmartCaptionsCapability(False, "feature_disabled")
    if not settings.subtitled_archetype_enabled:
        return SmartCaptionsCapability(False, "base_renderer_disabled")
    if edit_format != SMART_CAPTIONS_EDIT_FORMAT:
        return SmartCaptionsCapability(False, "unsupported_edit_format")
    if assignment is None:
        default = _default_capability()
        if default is not None:
            return default
        return SmartCaptionsCapability(False, "not_assigned")
    if assignment.enabled is not True:
        # An existing disabled row is an explicit opt-out — never fall back
        # to the fleet default for this creator.
        return SmartCaptionsCapability(False, "not_assigned")

    preset_id = str(assignment.preset_id or "").strip()
    preset_version = str(assignment.preset_version or "").strip()
    if not preset_id or not preset_version:
        return SmartCaptionsCapability(False, "invalid_assignment")
    try:
        from app.smart_edit.presets import load_preset  # noqa: PLC0415

        load_preset(preset_id, preset_version)
    except Exception:
        return SmartCaptionsCapability(False, "invalid_assignment")
    shadow = _resolved_shadow(assignment)
    return SmartCaptionsCapability(
        True,
        None,
        preset_id=preset_id,
        preset_version=preset_version,
        shadow_preset_id=shadow[0] if shadow else None,
        shadow_preset_version=shadow[1] if shadow else None,
    )


async def resolve_smart_captions_capability(
    *,
    user_id: uuid.UUID,
    edit_format: str | None,
    db: AsyncSession,
) -> SmartCaptionsCapability:
    # Avoid a DB read when a HARD gate (kill switch / format) already makes the
    # capability unavailable. This is material on GET /plan-items/{id}, which
    # is polled. NOTE: with a default preset configured the assignment=None
    # probe can come back AVAILABLE — the row must still be read so a
    # per-creator override or opt-out wins over the fleet default.
    early = _resolve_from_assignment(edit_format=edit_format, assignment=None)
    if not early.available and early.reason != "not_assigned":
        return early
    assignment = await db.get(CreatorStyleAssignment, user_id)
    return _resolve_from_assignment(
        edit_format=edit_format,
        assignment=assignment,
    )


def resolve_smart_captions_context_sync(
    *,
    user_id: uuid.UUID,
    edit_format: str | None,
    requested: bool,
    sound_design_enabled: bool = True,
    db: Session,
) -> dict[str, str] | None:
    """Pin a reviewed creator preset into a render job at dispatch time.

    A stored ``smart_captions_enabled=true`` never bypasses a later kill switch
    or revoked creator assignment. Returning ``None`` preserves the legacy
    ``all_candidates`` shape and therefore the byte-identity contract.
    """

    if not requested:
        return None
    early = _resolve_from_assignment(edit_format=edit_format, assignment=None)
    if not early.available and early.reason != "not_assigned":
        return None
    assignment = db.get(CreatorStyleAssignment, user_id)
    capability = _resolve_from_assignment(edit_format=edit_format, assignment=assignment)
    if not capability.available or not capability.preset_id or not capability.preset_version:
        return None
    context = {
        "preset_id": capability.preset_id,
        "preset_version": capability.preset_version,
        "sound_design": "auto" if sound_design_enabled else "off",
    }
    if capability.shadow_preset_id and capability.shadow_preset_version:
        context["shadow_preset_id"] = capability.shadow_preset_id
        context["shadow_preset_version"] = capability.shadow_preset_version
    return context
