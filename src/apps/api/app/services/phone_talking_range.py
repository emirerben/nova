"""Range (source window) on a phone Talking (`subtitled`) edit.

The Range sheet edits the hydrated talking-source slots (`native-phone-talking-source[-n]`,
one per kept segment of the take). A Save carries those rows as ``timeline_slots``: the SAME
slot ids, order and count the server derived, with new ``in_s`` / ``duration_s``. The rows
become the speaker's ``keep_segments`` (`phone_editor._compile_subtitled_editor_commit`) and
every timed lane follows through the existing speech-cut projection
(`speech_cut_state.reproject_authored_speech_records`). Anything that is not a pure window
change (add, delete, split, reorder, speed, crop, look, transition) is refused by name: the
Talking recipe has no lane for it and the capability (`clips.source_window`) never offers it.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import HTTPException

from app.pipeline.speech_cut_state import reproject_authored_speech_records
from app.schemas.editor_window import below_editor_minimum, snap_to_editor_minimum

# The server and the app share these codes; the app owns the plain-words copy.
REASON_MULTI_CLIP = "talking_range_multi_clip"
REASON_WORD_CUTS = "talking_range_word_cuts"
REASON_PROCESSED_AUDIO = "talking_range_processed_audio"
REASON_CUT_MISMATCH = "talking_range_cut_mismatch"
CODE_STRUCTURE_CHANGED = "talking_range_structure_changed"
CODE_OVERLAP = "talking_range_overlap"
CODE_CAPTIONS_MISSING = "talking_range_captions_missing"

TALKING_SLOT_PREFIX = "native-phone-talking-source"
# Segments that were cut apart on a phone keep their own gap; a window may reach the
# neighbour's edge within a millisecond (the persisted summaries round to 3 decimals).
_EDGE_TOLERANCE_S = 0.001
# The phone clamps its own windows with this slack (KriaSourceWindowGeometry.boundsTolerance).
_SOURCE_BOUNDS_TOLERANCE_S = 0.05
_MIN_SPEECH_SEGMENT_S = 0.001


def is_phone_talking_range_variant(variant: dict) -> bool:
    """A device-rendered `subtitled` (Talking) variant, whatever the rollout says."""
    return (
        variant.get("resolved_archetype") == "subtitled"
        and variant.get("render_destination") == "device"
    )


def talking_range_reason(job: Any, variant: dict) -> str | None:
    """Why Range cannot be saved on this variant right now, or ``None`` when it can.

    The capability map and the Save use this one predicate, so the app never offers a Range
    the server would refuse. Cheap: no storage reads.
    """
    from app.routes.generative_jobs import editor_deletion_timeline  # noqa: PLC0415

    if variant.get("phone_talking_multi_clip"):
        return REASON_MULTI_CLIP
    rows = editor_deletion_timeline(job, variant)
    if not rows or not all(str(r.get("slot_id", "")).startswith(TALKING_SLOT_PREFIX) for r in rows):
        return REASON_MULTI_CLIP
    cuts = variant.get("explicit_speech_cuts")
    if isinstance(cuts, list) and any(
        isinstance(c, dict) and c.get("status") == "applied" for c in cuts
    ):
        return REASON_WORD_CUTS
    noise = variant.get("engine_noise_reduction")
    if isinstance(noise, dict) and noise.get("status") == "applied":
        return REASON_PROCESSED_AUDIO
    recipe = _pinned_recipe(job, variant)
    if recipe is None:
        return REASON_CUT_MISMATCH
    if any(track.id == "processed-source-audio" for track in recipe.tracks):
        return REASON_PROCESSED_AUDIO
    pinned = _pinned_windows(recipe)
    if pinned is None or len(pinned) != len(rows):
        return REASON_CUT_MISMATCH
    for (start, end), row in zip(pinned, rows, strict=True):
        if (
            abs(start - float(row["in_s"])) > _EDGE_TOLERANCE_S
            or abs(end - float(row["in_s"]) - float(row["duration_s"])) > _EDGE_TOLERANCE_S
        ):
            return REASON_CUT_MISMATCH
    return None


def _pinned_recipe(job: Any, variant: dict) -> Any | None:
    from app.kria.recipes_v2 import EditRecipeV2  # noqa: PLC0415
    from app.services.device_render import device_status  # noqa: PLC0415

    try:
        recipe = device_status(job, str(variant.get("variant_id"))).request.recipe
    except Exception:  # noqa: BLE001 - no pinned recipe -> nothing to compile from
        return None
    return recipe if isinstance(recipe, EditRecipeV2) else None


def _pinned_windows(recipe: Any) -> list[tuple[float, float]] | None:
    track = next((t for t in recipe.tracks if t.id == "subtitled"), None)
    if track is None:
        return None
    clips = sorted(track.clips, key=lambda c: c.timeline_start)
    return [(c.source_start, c.source_start + c.source_duration) for c in clips] or None


def _refuse(code: str, status: int = 422, **context: Any) -> HTTPException:
    return HTTPException(status, detail={"code": code, **context})


def resolve_phone_talking_range_slots(job: Any, variant: dict, slots: list[Any]) -> list[dict]:
    """The posted rows as resolved slot dicts, or an HTTPException naming the refusal.

    Only ``in_s`` / ``duration_s`` may differ from the rows the server derived. Rows keep
    their ids and order; a window may grow into the pause next to it but never into its
    neighbour's source, and never past the source (50 ms slack, like the phone).
    """
    from app.routes.generative_jobs import (  # noqa: PLC0415
        editor_deletion_timeline,
    )
    from app.services.authored_editor import (  # noqa: PLC0415
        _fit_window_to_source,
    )

    reason = talking_range_reason(job, variant)
    if reason is not None:
        raise _refuse(reason)
    canonical = editor_deletion_timeline(job, variant)
    if len(slots) != len(canonical) or any(
        slot.removed or slot.slot_id != row["slot_id"] for slot, row in zip(slots, canonical)
    ):
        raise _refuse(CODE_STRUCTURE_CHANGED)
    resolved: list[dict] = []
    for order, (slot, row) in enumerate(zip(slots, canonical, strict=True)):
        if (
            slot.clip_index != 0
            or slot.transition_after != "cut"
            or slot.duration_s is None
            or slot.duration_beats is not None
            or slot.parent_segment_id is not None
            or slot.playback_rate not in (None, 1.0)
            or slot.source_end_s is not None
            or slot.source_crop is not None
            or slot.look_preset not in (None, "none")
            or slot.look_adjustments is not None
            or slot.layout is not None
            or slot.tilt
        ):
            raise _refuse(CODE_STRUCTURE_CHANGED)
        duration = float(slot.duration_s)
        start = float(slot.in_s)
        if not all(math.isfinite(v) for v in (start, duration)) or start < 0:
            raise _refuse("TIMELINE_INVALID_WINDOW")
        if below_editor_minimum(duration, _MIN_SPEECH_SEGMENT_S):
            raise _refuse("TIMELINE_INVALID_WINDOW")
        duration = snap_to_editor_minimum(duration, _MIN_SPEECH_SEGMENT_S)
        duration = _fit_window_to_source(
            slot,
            order=order,
            duration=duration,
            rate=1.0,
            bound=row.get("source_duration_s"),
            minimum_duration=_MIN_SPEECH_SEGMENT_S,
        )
        resolved.append(
            {
                **row,
                "in_s": start,
                "duration_s": duration,
                "removed": False,
                "order": order,
                "source_start_locked": True,
            }
        )
    for left, right in zip(resolved, resolved[1:], strict=False):
        if left["in_s"] + left["duration_s"] > right["in_s"] + _EDGE_TOLERANCE_S:
            raise _refuse(CODE_OVERLAP, slot_id=left["slot_id"])
    return resolved


def keep_segments_from_rows(rows: list[dict]) -> list[tuple[float, float]]:
    return [(float(r["in_s"]), float(r["in_s"]) + float(r["duration_s"])) for r in rows]


def layout_slots(
    segments: list[tuple[float, float]], ids: list[str], *, tail_s: float = 0.0
) -> list[dict]:
    """Slot dicts `speech_cut_state` projects through: one per kept segment, in play order,
    plus (when the recipe plays something after the speaker, e.g. an ending clip) a
    pseudo-slot of that length so a lane under it shifts with the cut instead of dropping."""
    slots = [
        {
            "slot_id": slot_id,
            "clip_index": 0,
            "in_s": start,
            "duration_s": end - start,
            "removed": False,
        }
        for slot_id, (start, end) in zip(ids, segments, strict=True)
    ]
    if tail_s > 1e-6:
        slots.append(
            {
                "slot_id": "_tail",
                "clip_index": -1,
                "in_s": 0.0,
                "duration_s": tail_s,
                "removed": False,
            }
        )
    return slots


def reproject_between_layouts(
    records: list[dict], old_slots: list[dict], new_slots: list[dict]
) -> list[dict]:
    """Carry timed records (``start_s``/``end_s`` windows or ``at_s`` points) from the old
    output clock onto the new one through their source occurrence."""
    return reproject_authored_speech_records(records, old_slots=old_slots, new_slots=new_slots)


def prepare_phone_talking_range_commit(
    job: Any,
    variant_id: str,
    payload: Any,
    *,
    validation_arguments: dict[str, Any],
    phone_sfx_catalog_paths: dict[str, str] | None = None,
    creator_brief_binding: dict | None = None,
) -> dict:
    """Stage a Talking Save that carries Range rows: resolve them, re-anchor the speech lanes
    to the new layout, then run the ordinary phone commit (which recompiles the pinned
    recipe from ``prep["timeline_override"]``). The variant never enters authored mode."""
    from app.routes.generative_jobs import (  # noqa: PLC0415
        _find_variant,
        _prepare_editor_commit,
        editor_deletion_timeline,
    )
    from app.services.authored_editor import sync_talking_source_lanes  # noqa: PLC0415
    from app.services.phone_editor import (  # noqa: PLC0415
        _StagedJob,
        prepare_phone_editor_commit,
    )

    original_job = job
    job = _StagedJob(job)
    variant = _find_variant(job, variant_id)
    if variant is None:
        raise HTTPException(404, detail="Variant not found")
    resolved = resolve_phone_talking_range_slots(job, variant, payload.timeline_slots)
    old_slots = editor_deletion_timeline(job, variant)
    payload, unresolved = sync_talking_source_lanes(job, variant, payload, resolved, old_slots)
    if unresolved:
        raise _refuse(CODE_CAPTIONS_MISSING)
    prep = prepare_phone_editor_commit(
        job,
        variant_id,
        prepare=lambda staged: _prepare_editor_commit(
            staged, variant_id, payload, **validation_arguments
        ),
        learning_sections=payload.model_fields_set,
        sfx_catalog_paths=phone_sfx_catalog_paths,
        creator_brief_binding=creator_brief_binding,
    )
    for name, value in vars(job).items():
        if name != "_job":
            setattr(original_job, name, value)
    return prep


def reproject_lanes(lanes: Any, old_slots: list[dict], new_slots: list[dict]) -> Any:
    """The pinned card and sound-effect lanes on the new output clock.

    A card keeps the span its source occurrence still plays (dropped when none does); an
    effect keeps its trigger point or is dropped when that footage no longer plays. The ending
    clip and background have no window on the speaker's clock and stay as they are."""
    if lanes is None:
        return None
    cards = []
    for card in lanes.overlays:
        parts = reproject_between_layouts(
            [{"id": card.id, "start_s": card.start_s, "end_s": card.end_s}], old_slots, new_slots
        )
        if parts:
            cards.append(
                card.model_copy(
                    update={
                        "start_s": min(p["start_s"] for p in parts),
                        "end_s": max(p["end_s"] for p in parts),
                    }
                )
            )
    effects = []
    for effect in lanes.sound_effects:
        parts = reproject_between_layouts(
            [{"id": effect.request.id, "at_s": effect.request.at_s}], old_slots, new_slots
        )
        if parts:
            request = effect.request.model_copy(update={"at_s": parts[0]["at_s"]})
            effects.append(effect.model_copy(update={"request": request}))
    return lanes.model_copy(update={"overlays": cards, "sound_effects": effects})
