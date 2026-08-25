"""Server-owned policy for the opt-in Speech cleanup control.

This module intentionally contains no database or request code.  The API, clip
writer and worker all use the same small policy surface so a UI toggle cannot
silently change the render contract.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

# Live dispatch has completed the opt-in cutover. ``legacy_auto`` remains a
# historical job contract below so already-enqueued jobs can still render.
SpeechCleanupMode = Literal["opt_in", "disabled"]
SpeechCleanupContract = Literal["legacy_auto", "required_v1", "off_v1"]

_SUPPORTED_FORMATS = {"subtitled", "talking_head", "narrated", "narrated_planned", "narrated_ready"}


class SpeechCleanupFailure(RuntimeError):
    """Typed, user-actionable failure for a required cleanup contract."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        self.reason = reason
        super().__init__(detail or f"Speech cleanup failed: {reason}")


@dataclass(frozen=True)
class SpeechCleanupCapability:
    available: bool
    reason: str | None = None


@dataclass(frozen=True)
class SpeechCleanupInputs:
    """Content inputs that can invalidate a user's cleanup consent.

    Rollout and engine state intentionally do not live here: an operational
    outage must preserve an explicit On preference so the user can retry once
    the service recovers.
    """

    footage_identity: tuple[tuple[str, str], ...]
    edit_format: str
    audio_mode: str | None
    voiceover_gcs_path: str | None


def _clip_assignments(item: Any) -> list[dict[str, Any]]:
    raw = getattr(item, "clip_assignments", None) or []
    return [value for value in raw if isinstance(value, dict) and value.get("gcs_path")]


def main_footage_identity(item: Any) -> tuple[tuple[str, str], ...]:
    """Return the ordered consent identity for the item's main footage.

    ``media_id`` is the preferred stable identity.  Legacy rows without one use
    a deterministic positional identity, so adding notes or analysis metadata
    cannot revoke consent while replacing/reordering a source can.
    """

    assignments = _clip_assignments(item)
    if assignments:
        return tuple(
            (
                str(value.get("media_id") or f"legacy-{index}"),
                str(value.get("gcs_path") or ""),
            )
            for index, value in enumerate(assignments)
        )
    return tuple(
        (f"legacy-{index}", str(path))
        for index, path in enumerate(getattr(item, "clip_gcs_paths", None) or [])
        if path
    )


def footage_fingerprint(item: Any) -> str:
    payload = json.dumps(main_footage_identity(item), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cleanup_inputs(item: Any) -> SpeechCleanupInputs:
    """Capture the policy-relevant content state before a writer mutates it."""

    return SpeechCleanupInputs(
        footage_identity=main_footage_identity(item),
        edit_format=str(getattr(item, "edit_format", "") or "").strip().lower(),
        audio_mode=(str(getattr(item, "audio_mode", "") or "").strip().lower() or None),
        voiceover_gcs_path=(str(getattr(item, "voiceover_gcs_path", "") or "") or None),
    )


def capability_for_item(
    item: Any,
    *,
    mode: SpeechCleanupMode,
    engine_enabled: bool,
    renderer_enabled: bool = True,
) -> SpeechCleanupCapability:
    """Resolve capability without querying the database or rendering anything."""

    if mode not in {"opt_in", "disabled"}:
        raise ValueError(f"unsupported_speech_cleanup_mode:{mode}")
    if mode == "disabled":
        return SpeechCleanupCapability(False, "rollout_disabled")
    if not main_footage_identity(item):
        return SpeechCleanupCapability(False, "no_committed_clip")
    edit_format = str(getattr(item, "edit_format", "") or "").strip().lower()
    if edit_format not in _SUPPORTED_FORMATS:
        return SpeechCleanupCapability(False, "unsupported_format")
    if getattr(item, "voiceover_gcs_path", None) or getattr(item, "audio_mode", None) == (
        "voiceover"
    ):
        return SpeechCleanupCapability(False, "replacement_voiceover")
    if not renderer_enabled:
        return SpeechCleanupCapability(False, "renderer_disabled")
    if not engine_enabled:
        return SpeechCleanupCapability(False, "engine_disabled")
    return SpeechCleanupCapability(True)


def renderer_enabled_for_item(
    item: Any,
    *,
    subtitled_enabled: bool,
    talking_head_enabled: bool,
    narrated_self_narration_enabled: bool,
) -> bool:
    """Return whether the declared format has a cleanup-capable renderer."""

    edit_format = str(getattr(item, "edit_format", "") or "").strip().lower()
    if edit_format == "subtitled":
        return subtitled_enabled
    if edit_format == "talking_head":
        return talking_head_enabled
    if edit_format in {"narrated", "narrated_planned", "narrated_ready"}:
        # Voiceover edits are intentionally ineligible for this feature; the
        # only supported narrated path is self-narration from source footage.
        return narrated_self_narration_enabled
    return False


def contract_for_item(
    item: Any,
    *,
    mode: SpeechCleanupMode,
    engine_enabled: bool,
    renderer_enabled: bool = True,
) -> SpeechCleanupContract | None:
    """Map a PlanItem preference to an immutable job contract.

    New dispatches can only stamp explicit ``required_v1`` or ``off_v1``
    contracts. Historical ``legacy_auto`` jobs remain accepted by renderers.
    """

    if mode not in {"opt_in", "disabled"}:
        raise ValueError(f"unsupported_speech_cleanup_mode:{mode}")
    if mode == "disabled":
        # A stored On preference is durable user intent.  Do not silently turn
        # it off when the rollout is paused; dispatch must reject it so the UI
        # can offer an explicit Turn off action and preserve consent for retry.
        if bool(getattr(item, "speech_cleanup_enabled", False)):
            capability = capability_for_item(
                item,
                mode=mode,
                engine_enabled=engine_enabled,
                renderer_enabled=renderer_enabled,
            )
            raise ValueError(f"speech_cleanup_unavailable:{capability.reason}")
        return "off_v1"
    if bool(getattr(item, "speech_cleanup_enabled", False)):
        capability = capability_for_item(
            item,
            mode=mode,
            engine_enabled=engine_enabled,
            renderer_enabled=renderer_enabled,
        )
        if not capability.available:
            raise ValueError(f"speech_cleanup_unavailable:{capability.reason}")
        return "required_v1"
    return "off_v1"


def new_notice(reason: str) -> dict[str, str]:
    return {"id": uuid.uuid4().hex, "reason": reason}


def reconcile_consent(item: Any, previous_identity: tuple[tuple[str, str], ...]) -> bool:
    """Clear opt-in consent when the ordered main-footage identity changes."""

    current = main_footage_identity(item)
    if current == previous_identity or not bool(getattr(item, "speech_cleanup_enabled", False)):
        return False
    item.speech_cleanup_enabled = False
    item.speech_cleanup_notice = new_notice("main_footage_changed")
    return True


def reconcile_item_policy_change(item: Any, previous: SpeechCleanupInputs) -> bool:
    """Reconcile opt-in after any PlanItem content writer.

    Main-footage replacement, an unsupported format, and replacement voiceover
    are user-content changes, so they clear consent and create a notice. Engine
    or rollout outages are deliberately excluded and preserve On.
    """

    if not bool(getattr(item, "speech_cleanup_enabled", False)):
        return False
    current = cleanup_inputs(item)
    reason: str | None = None
    if current.footage_identity != previous.footage_identity:
        reason = "main_footage_changed"
    elif current.edit_format not in _SUPPORTED_FORMATS:
        reason = "unsupported_format"
    elif current.voiceover_gcs_path or current.audio_mode == "voiceover":
        reason = "replacement_voiceover"
    if reason is None:
        return False
    item.speech_cleanup_enabled = False
    item.speech_cleanup_notice = new_notice(reason)
    return True


def acknowledge_notice(item: Any, notice_id: str | None) -> bool:
    notice = getattr(item, "speech_cleanup_notice", None)
    if not notice_id or not isinstance(notice, dict) or notice.get("id") != notice_id:
        return False
    item.speech_cleanup_notice = None
    return True
