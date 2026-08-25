"""Core Creator craft compilation.

This module is deliberately renderer-free.  It translates the three Stage 3
commands into the existing transactional editor-commit request.  Validation of
the resulting timeline/caption sections remains owned by
``app.routes.generative_jobs.prepare_editor_commit``.
"""

from __future__ import annotations

from typing import Any

from app.agents._schemas.creator_agent import (
    CreatorCraftBundle,
    SetCaptionStyleCommand,
    SetLookPresetCommand,
    SetTransitionCommand,
)
from app.pipeline.look_presets import normalize_look_adjustments, normalize_look_preset
from app.routes.generative_jobs import (
    EditorCommitCaptionMeta,
    EditorCommitRequest,
    TimelineSlotEdit,
)

_TRANSITION_TO_TIMELINE = {
    "none": "cut",
    "crossfade": "crossfade",
    "fade_black": "dip_to_black",
    "fade_white": "flash",
}


class CreatorCraftValidationError(ValueError):
    """A deterministic command/bundle validation failure before any write."""


def _effective_slots(variant: dict[str, Any]) -> list[dict[str, Any]]:
    user_timeline = variant.get("user_timeline") or {}
    if user_timeline.get("slots"):
        return [dict(slot) for slot in user_timeline["slots"] if isinstance(slot, dict)]
    ai_timeline = variant.get("ai_timeline") or {}
    return [dict(slot) for slot in ai_timeline.get("slots") or [] if isinstance(slot, dict)]


def _slot_edit(slot: dict[str, Any]) -> TimelineSlotEdit:
    """Project one server-owned slot into the public editor input contract."""

    clip_index = slot.get("clip_index")
    duration_s = slot.get("duration_s")
    if not isinstance(clip_index, int) or isinstance(clip_index, bool) or duration_s is None:
        raise CreatorCraftValidationError("The current timeline is not editable")
    preset = normalize_look_preset(slot.get("look_preset"))
    adjustments = normalize_look_adjustments(preset, slot.get("look_adjustments"))
    transition = str(slot.get("transition_after") or "cut")
    if transition not in {"cut", "crossfade", "dip_to_black", "flash"}:
        transition = "cut"
    return TimelineSlotEdit(
        slot_id=str(slot.get("slot_id")) if slot.get("slot_id") else None,
        parent_segment_id=(
            str(slot.get("parent_segment_id")) if slot.get("parent_segment_id") else None
        ),
        clip_index=clip_index,
        in_s=float(slot.get("in_s") or 0.0),
        duration_beats=(
            int(slot["duration_beats"]) if slot.get("duration_beats") is not None else None
        ),
        duration_s=float(duration_s),
        removed=bool(slot.get("removed")),
        transition_after=transition,
        transition_duration_s=(
            float(slot["transition_duration_s"])
            if slot.get("transition_duration_s") is not None and transition != "cut"
            else None
        ),
        look_preset=preset,
        look_adjustments=adjustments,
    )


def build_core_craft_editor_commit(
    bundle: CreatorCraftBundle,
    *,
    variant: dict[str, Any],
) -> EditorCommitRequest:
    """Compile a bundle into one existing atomic editor commit request.

    No persistence or queue operation occurs here.  Unsupported wipe
    transitions are rejected explicitly because the current timeline editor's
    validated vocabulary does not own those effects.
    """

    slots = _effective_slots(variant)
    slot_edits: list[TimelineSlotEdit] | None = None
    caption_meta: EditorCommitCaptionMeta | None = None

    for command in bundle.commands:
        if isinstance(command, SetCaptionStyleCommand):
            if caption_meta is not None:
                raise CreatorCraftValidationError("Only one caption-style command is allowed")
            caption_meta = EditorCommitCaptionMeta(style=command.caption_style)
            continue

        if isinstance(command, (SetTransitionCommand, SetLookPresetCommand)):
            if not slots:
                raise CreatorCraftValidationError("This variant has no editable timeline")
            if slot_edits is None:
                slot_edits = [_slot_edit(slot) for slot in slots]

        if isinstance(command, SetTransitionCommand):
            if command.transition in {"wipe_left", "wipe_right"}:
                raise CreatorCraftValidationError("This timeline does not support wipe transitions")
            if command.boundary_index >= len(slot_edits) - 1:
                raise CreatorCraftValidationError("Transition boundary is outside the timeline")
            target = slot_edits[command.boundary_index]
            target.transition_after = _TRANSITION_TO_TIMELINE[command.transition]
            target.transition_duration_s = (
                None
                if target.transition_after == "cut"
                else max(0.1, float(command.duration_s or 0.3))
            )
        elif isinstance(command, SetLookPresetCommand):
            if command.slot_index >= len(slot_edits):
                raise CreatorCraftValidationError("Look slot is outside the timeline")
            target = slot_edits[command.slot_index]
            target.look_preset = normalize_look_preset(command.look_preset)
            target.look_adjustments = normalize_look_adjustments(target.look_preset, None)

    return EditorCommitRequest(
        caption_meta=caption_meta,
        timeline_slots=slot_edits,
        base_generation=bundle.expected_generation_id,
    )


def craft_preview(bundle: CreatorCraftBundle, *, generation: str, sections: dict) -> dict[str, Any]:
    """Return bounded, non-capability preview data for the API response."""

    return {
        "generation": generation,
        "commands": [command.command for command in bundle.commands],
        "sections": dict(sections),
        "caption_style": next(
            (
                command.caption_style
                for command in bundle.commands
                if isinstance(command, SetCaptionStyleCommand)
            ),
            None,
        ),
        "transitions": [
            {
                "boundary_index": command.boundary_index,
                "transition": command.transition,
                "duration_s": command.duration_s,
            }
            for command in bundle.commands
            if isinstance(command, SetTransitionCommand)
        ],
        "looks": [
            {"slot_index": command.slot_index, "look_preset": command.look_preset}
            for command in bundle.commands
            if isinstance(command, SetLookPresetCommand)
        ],
    }


__all__ = [
    "CreatorCraftValidationError",
    "build_core_craft_editor_commit",
    "craft_preview",
]
