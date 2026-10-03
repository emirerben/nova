"""Creator-owned clip cuts for phone Voiceover videos (KRI-290).

A phone `narrated` or montage `voiceover` edit used to lock its timeline to the
voiceover (`locked_to_voiceover` / `voiceover_bed_fit`). The creator may now
trim, extend, reorder, split or delete its clips: footage longer than the
voiceover keeps playing after the voice ends, footage shorter than it cuts the
voice at the video's end. Either state is theirs to keep or fix in a later
edit.

The pinned device recipe stays the single source of truth. A timeline Save
resolves the posted slots here, `prepare_phone_editor_commit` swaps the
recipe's video track (`app.pipeline.phone_voiceover_cut`), and the saved
`user_timeline` mirrors exactly what the phone renders. The variant keeps its
archetype, so caption and lane Saves keep working on top of the new cut.

Kill switch: `PHONE_VOICEOVER_TIMELINE_EDITS_ENABLED=false` restores the lock
(capabilities and Save flip together).
"""

from __future__ import annotations

import copy
import math
import uuid
from typing import Any

from fastapi import HTTPException

from app.config import settings
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_voiceover_cut import narrated_slot_window
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from app.services.phone_voiceover_timeline import (
    is_phone_voiceover_family_variant,
    project_phone_voiceover_timeline,
    source_pool_paths,
)

# The native editor's own floor (`NativeEditorSession.minimumClipDuration`).
MIN_SLOT_DURATION_S = 0.1
# A posted window may overshoot its footage by float noise.
_SOURCE_TOLERANCE_S = 0.001
# A changed narrated window may not slow its footage below the native preview's
# own floor (`NativeEditorRenderCompiler`: rates outside 0.25...4 don't preview).
MIN_NARRATED_RATE = 0.25


def current_voiceover_slots(job: Any, variant: dict) -> list[dict] | None:
    """The cut the editor shows: the saved creator timeline, else the one
    projected from the pinned recipe. ``None`` when there is neither."""
    timeline = variant.get("user_timeline")
    rows = timeline.get("slots") if isinstance(timeline, dict) else None
    if isinstance(rows, list) and rows:
        return [copy.deepcopy(row) for row in rows if isinstance(row, dict)]
    projected = project_phone_voiceover_timeline(
        job.assembly_plan or {}, getattr(job, "all_candidates", None) or {}, variant
    )
    if not projected:
        return None
    pool = projected["pool"]
    return [{**slot, "source_gcs_path": pool[slot["clip_index"]]} for slot in projected["slots"]]


def phone_voiceover_cut_editable(job: Any, variant: dict) -> bool:
    """True when the creator may change this phone Voiceover edit's clip cut.

    Needs a cut the editor can show (otherwise the native preview falls back
    to locked recipe clips, which only works while the timeline is closed).
    """
    return bool(
        settings.phone_voiceover_timeline_edits_enabled
        and is_phone_voiceover_family_variant(variant)
        and variant.get("editor_timeline_mode") != "authored"
        and variant.get("editor_state") != "empty"
        and current_voiceover_slots(job, variant)
    )


def _error(status_code: int, code: str, **context: object) -> HTTPException:
    detail: dict[str, object] = {"code": code}
    detail.update({key: value for key, value in context.items() if value is not None})
    return HTTPException(status_code=status_code, detail=detail)


def _close(a: object, b: object) -> bool:
    try:
        return abs(float(a) - float(b)) <= 1e-6  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def resolve_phone_voiceover_slots(job: Any, variant: dict, slots: list[Any]) -> list[dict]:
    """Validate posted `TimelineSlotEdit`s for a phone Voiceover edit.

    Returns the active slots in timeline order, exactly as the recipe will
    render them (seconds, no beat grid, no snapping). Raises HTTPException:
    409 ``TIMELINE_STALE`` for an unknown slot id; 422 ``TIMELINE_EMPTY``,
    ``TIMELINE_UNKNOWN_CLIP``, ``TIMELINE_INVALID_DURATION``,
    ``TIMELINE_INVALID_WINDOW``, ``TIMELINE_OUT_OF_BOUNDS``,
    ``TIMELINE_TOO_LONG`` or ``phone_edit_unsupported`` (a look, crop, layout
    or speed change the phone editor doesn't offer).
    """
    from app.routes.generative_jobs import _TIMELINE_MAX_SLOTS, TIMELINE_MAX_TOTAL_S

    assembly = job.assembly_plan or {}
    pool = source_pool_paths(assembly, getattr(job, "all_candidates", None) or {})
    bindings: dict[str, PhoneSourceBinding] = {}
    for row in assembly.get(PHONE_SOURCES_FIELD) or []:
        try:
            binding = PhoneSourceBinding.model_validate(row)
        except ValueError:
            continue
        bindings[binding.proxy_path] = binding
    current = current_voiceover_slots(job, variant) or []
    baseline = {row.get("slot_id"): row for row in current if row.get("slot_id")}
    narrated = variant.get("resolved_archetype") == "narrated"
    clip_rates = {
        (row.get("clip_index"), float(row.get("playback_rate") or 1.0)) for row in current
    }

    resolved: list[dict] = []
    seen: set[str] = set()
    total = 0.0
    for slot in slots:
        if slot.removed:
            continue
        if slot.slot_id is not None and slot.slot_id not in baseline:
            raise _error(409, "TIMELINE_STALE")
        slot_id = slot.slot_id or str(uuid.uuid4())
        if slot_id in seen:
            raise _error(422, "TIMELINE_INVALID_WINDOW", slot_id=slot_id)
        seen.add(slot_id)
        if not 0 <= slot.clip_index < len(pool) or pool[slot.clip_index] not in bindings:
            raise _error(422, "TIMELINE_UNKNOWN_CLIP")
        if slot.duration_beats is not None or slot.duration_s is None:
            raise _error(422, "TIMELINE_INVALID_DURATION")
        in_s, duration = float(slot.in_s), float(slot.duration_s)
        if not (math.isfinite(in_s) and math.isfinite(duration)) or in_s < 0:
            raise _error(422, "TIMELINE_INVALID_WINDOW", slot_id=slot.slot_id)
        if duration < MIN_SLOT_DURATION_S:
            raise _error(422, "TIMELINE_INVALID_WINDOW", slot_id=slot.slot_id)
        if (
            slot.look_preset not in (None, "none")
            or slot.look_adjustments is not None
            or slot.source_crop is not None
            or slot.layout == "supporting_card"
        ):
            raise _error(422, "phone_edit_unsupported")
        binding = bindings[pool[slot.clip_index]]
        source_duration = float(binding.original.duration_s)
        base = baseline.get(slot.slot_id) if slot.slot_id else None
        unchanged = (
            base is not None
            and base.get("clip_index") == slot.clip_index
            and _close(base.get("in_s"), in_s)
            and _close(base.get("duration_s"), duration)
        )
        rate = 1.0
        if narrated:
            # The narrated compiler slows short footage to fill its window;
            # the server owns that speed, so a posted one is ignored.
            try:
                in_s, _, fitted_rate = narrated_slot_window(binding, in_s, duration)
            except UnsupportedPhonePlan:
                fitted_rate = 0.0
            if fitted_rate <= 0 or (not unchanged and fitted_rate < MIN_NARRATED_RATE):
                raise _error(
                    422,
                    "TIMELINE_OUT_OF_BOUNDS",
                    reason="source_window_too_short",
                    slot_id=slot.slot_id,
                    clip_index=slot.clip_index,
                    source_duration_s=round(source_duration, 3),
                )
        else:
            # Speed isn't editable on the phone: a clip keeps the rate it was
            # pinned with (a split piece inherits it from its source clip).
            base_rate = float(base.get("playback_rate") or 1.0) if base is not None else None
            posted = slot.playback_rate
            if base_rate is not None:
                if posted is not None and not _close(posted, base_rate):
                    raise _error(422, "phone_edit_unsupported")
                rate = base_rate
            elif posted is not None and not _close(posted, 1.0):
                if (slot.clip_index, float(posted)) not in clip_rates:
                    raise _error(422, "phone_edit_unsupported")
                rate = float(posted)
            if not unchanged and (
                in_s >= source_duration
                or in_s + duration * rate > source_duration + _SOURCE_TOLERANCE_S
            ):
                raise _error(
                    422,
                    "TIMELINE_OUT_OF_BOUNDS",
                    reason="source_window_too_short",
                    slot_id=slot.slot_id,
                    clip_index=slot.clip_index,
                    in_s=round(in_s, 3),
                    source_duration_s=round(source_duration, 3),
                    available_duration_s=round(max(0.0, source_duration - in_s), 3),
                    required_duration_s=round(duration * rate, 3),
                )
        total += duration
        row = {
            "slot_id": slot_id,
            "clip_index": slot.clip_index,
            "in_s": in_s,
            "duration_s": duration,
            "duration_beats": None,
            "order": len(resolved),
            "removed": False,
            "source_gcs_path": pool[slot.clip_index],
            "source_duration_s": round(source_duration, 3),
            "transition_after": slot.transition_after,
            "transition_duration_s": slot.transition_duration_s,
        }
        if rate != 1.0:
            row["playback_rate"] = rate
        if slot.parent_segment_id:
            row["parent_segment_id"] = slot.parent_segment_id
        resolved.append(row)
    if not resolved:
        raise _error(422, "TIMELINE_EMPTY")
    # Never block trimming an edit that was already longer than the editor cap.
    current_total = sum(float(row.get("duration_s") or 0.0) for row in current)
    if len(resolved) > _TIMELINE_MAX_SLOTS or total > max(TIMELINE_MAX_TOTAL_S, current_total):
        raise _error(422, "TIMELINE_TOO_LONG")
    return resolved
