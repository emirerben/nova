"""Editor ops v2, timeline lane (KRI-219 Lane B).

Two bulk ops so a whole-edit ask ("make it 20 seconds", "all clips 2 seconds",
"crossfade between every clip") is one or two operations instead of one per clip:

* ``patch_slots``: selector + patch over many clips (duration, source start,
  speed, look, transition, crop, remove).
* ``set_total_duration``: retarget the whole edit's length.

Both compile into ``timeline_slots``. On guided-native variants the compiler
then rebases the per-clip label bars onto their segments
(``services/kria_editor_timeline.py``).

Circular-import rule: no top-level import of app.agents.edit_copilot or
app.services.kria_editor_ops (both import this package).
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import ValidationError

from app.agents.editor_ops_v2 import OpSpec
from app.kria.clip_tilt import ClipTilt, wrap_degrees
from app.kria.recipes import ClipBackdrop
from app.kria.reply_language import say

# Mirrors src/apps/ios/Kria/Core/NativeEditorDocument.swift NativeEditorWireContract.
LOOK_PRESETS = (
    "none",
    "stadium_diffusion",
    "olive_film",
    "smoky_split_tone",
    "golden_hour",
    "faded_analog",
)
TRANSITIONS = ("cut", "crossfade", "dip_to_black", "flash")
TRANSITION_MIN_S = 0.1
TRANSITION_MAX_S = 0.3
RATE_MIN, RATE_MAX = 0.25, 4.0
MIN_CLIP_S = 0.1
MIN_RETARGET_CLIP_S = 0.3
_FPS = 30.0
_TOL_S = 0.05
_PATCH_KEYS = frozenset(
    {
        "duration_s",
        "in_s",
        "playback_rate",
        "look_preset",
        "transition_after",
        "transition_duration_s",
        "source_crop",
        "tilt",
        "removed",
    }
)
_TILT_KEYS = frozenset({"rotation_deg", "rotation_delta_deg", "backdrop"})
_TILT_MAX_TURN_DEG = 360.0
_STRATEGIES = ("proportional", "uniform", "trim_tail")
_FAMILY = frozenset({"clip", "clips", "timeline"})


# ── Parser coercion ───────────────────────────────────────────────────────────


def _num(value: object, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < low or number > high:
        return None
    return number


def _max_total_s() -> float:
    from app.schemas.edit_proposal import MAX_PROPOSAL_DURATION_S  # noqa: PLC0415

    return float(MAX_PROPOSAL_DURATION_S)


def _clean_selector(name: str, selector: object, slots: list[dict], state: Any) -> dict | None:
    if not isinstance(selector, dict):
        state.invalid_value()
        return None
    chosen = [k for k in ("slot_indexes", "clip_ids", "media_kind", "all") if selector.get(k)]
    if len(chosen) != 1:
        state.invalid_value()
        return None
    kind = chosen[0]
    raw = selector[kind]
    if kind == "slot_indexes":
        if (
            not isinstance(raw, list)
            or not 1 <= len(raw) <= len(slots)
            or any(
                isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(slots)
                for i in raw
            )
            or len(set(raw)) != len(raw)
        ):
            state.invalid_value()
            return None
        return {"slot_indexes": sorted(raw)}
    if kind == "clip_ids":
        known = {str(s.get("media_id")) for s in slots if s.get("media_id")}
        if not isinstance(raw, list) or any(not isinstance(v, str) for v in raw):
            state.invalid_value()
            return None
        if any(v not in known for v in raw):
            state.reject(
                op=name, reason="stale_target", detail="A selected clip is no longer available"
            )
            return None
        return {"clip_ids": list(dict.fromkeys(raw))}
    if kind == "media_kind":
        if raw not in {"video", "image"}:
            state.invalid_value()
            return None
        return {"media_kind": raw}
    if raw is not True:
        state.invalid_value()
        return None
    return {"all": True}


def _clean_crop(value: object) -> dict[str, float] | None:
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        return None
    try:
        x, y, w, h = (float(value[k]) for k in ("x", "y", "width", "height"))
    except (TypeError, ValueError):
        return None
    if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1):
        return None
    if x + w > 1 + 1e-6 or y + h > 1 + 1e-6:
        return None
    return {"x": x, "y": y, "width": w, "height": h}


def _clean_tilt(value: object, state: Any) -> tuple[bool, dict[str, Any] | None]:
    """(valid, tilt). ``None`` is "straighten"; otherwise an angle and/or a backdrop.

    ``rotation_deg`` replaces the clip's angle and ``rotation_delta_deg`` adds to it. Either
    may be up to a full turn either way: an absolute angle is wrapped here, a relative one is
    wrapped against each clip's own angle when it is applied.
    """
    if value is None:
        return True, None
    if not isinstance(value, dict) or not value or not set(value) <= _TILT_KEYS:
        state.invalid_value(_tilt_shape_note())
        return False, None
    if "rotation_deg" in value and "rotation_delta_deg" in value:
        state.invalid_value(_tilt_shape_note())
        return False, None
    clean: dict[str, Any] = {}
    for key in ("rotation_deg", "rotation_delta_deg"):
        if key not in value:
            continue
        angle = _num(value[key], -_TILT_MAX_TURN_DEG, _TILT_MAX_TURN_DEG)
        if angle is None:
            state.invalid_value(
                say(
                    en="A tilt needs an angle in degrees, up to a full turn to the left or right.",
                    tr="Eğim için en fazla tam bir tur sola veya sağa olacak şekilde derece "
                    "cinsinden bir açı gerekiyor.",
                )
            )
            return False, None
        clean[key] = wrap_degrees(angle) if key == "rotation_deg" else angle
    if "backdrop" in value:
        try:
            backdrop = ClipBackdrop.model_validate(value["backdrop"])
        except ValidationError:
            state.invalid_value(
                say(
                    en="Behind a tilted clip I can show a plain background, a blurred copy of the "
                    "clip, or one colour (like #112233).",
                    tr="Eğik klibin arkasında düz bir arka plan, klibin bulanık bir kopyası veya "
                    "tek bir renk (örneğin #112233) gösterebilirim.",
                )
            )
            return False, None
        clean["backdrop"] = backdrop.model_dump(mode="json")
    return True, clean


def _tilt_shape_note() -> str:
    return say(
        en="A tilt is an angle, a background, or both. I can't read that one.",
        tr="Eğim bir açı, bir arka plan ya da ikisi birden olabilir. Bunu anlayamadım.",
    )


def _coerce_patch_slots(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    from app.agents import edit_copilot as ec  # noqa: PLC0415

    slots = [s for s in ec._snapshot_list(snapshot, ec._SLOT_INDEX_KEYS) if isinstance(s, dict)]
    selector = _clean_selector(name, payload.get("selector"), slots, state)
    if selector is None:
        return None
    patch = payload.get("patch")
    if not isinstance(patch, dict) or not patch or not set(patch) <= _PATCH_KEYS:
        state.invalid_value()
        return None
    clean: dict[str, Any] = {}
    for key, value in patch.items():
        if key == "duration_s":
            cleaned: Any = _num(value, MIN_CLIP_S, _max_total_s())
        elif key == "in_s":
            cleaned = _num(value, 0.0, 36000.0)
        elif key == "playback_rate":
            cleaned = _num(value, RATE_MIN, RATE_MAX)
        elif key == "transition_duration_s":
            cleaned = _num(value, TRANSITION_MIN_S, TRANSITION_MAX_S)
        elif key == "look_preset":
            cleaned = value if value in LOOK_PRESETS else None
        elif key == "transition_after":
            cleaned = value if value in TRANSITIONS else None
        elif key == "removed":
            cleaned = value if isinstance(value, bool) else None
        elif key == "tilt":
            # None is a valid value here ("straighten"), so it cannot ride the check below.
            valid, tilt = _clean_tilt(value, state)
            if not valid:
                return None
            clean[key] = tilt
            continue
        else:  # source_crop
            cleaned = _clean_crop(value)
        if cleaned is None:
            state.invalid_value()
            return None
        clean[key] = cleaned
    capabilities = snapshot.get("timeline_patch_capabilities")
    if "tilt" in clean and not (
        isinstance(capabilities, dict) and capabilities.get("tilt") is True
    ):
        unavailable = say(
            en="I can't tilt clips on this edit.",
            tr="Bu videoda klipleri eğemiyorum.",
        )
        state.reject(op=name, reason="capability_unavailable", detail=unavailable)
        state.reply_notes.append(unavailable)
        return None
    if isinstance(capabilities, dict):
        unavailable = [key for key in clean if capabilities.get(key) is False]
        if unavailable:
            state.invalid_value()
            state.reject(
                op=name,
                reason="capability_unavailable",
                detail="unsupported timeline field: " + ", ".join(unavailable),
            )
            return None
    preservation = _preservation_fields(payload, state)
    if preservation is None:
        return None
    return {"selector": selector, "patch": clean, **preservation}


def _preservation_fields(payload: dict, state: Any) -> dict | None:
    if "preserve_text" not in payload:
        return {}
    if not isinstance(payload["preserve_text"], bool):
        state.invalid_value()
        return None
    return {"preserve_text": payload["preserve_text"]}


def _coerce_set_total_duration(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    target = _num(payload.get("target_s"), 1.0, _max_total_s())
    if target is None:
        state.invalid_value()
        return None
    strategy = payload.get("strategy", "proportional")
    if strategy not in _STRATEGIES:
        state.invalid_value()
        return None
    preservation = _preservation_fields(payload, state)
    if preservation is None:
        return None
    return {"target_s": target, "strategy": strategy, **preservation}


_ORDER_CRITERIA = ("capture_time", "custom", "random")
_ORDER_DIRECTIONS = ("asc", "desc")


def _coerce_reorder_clips_by(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    """Resolve ``reorder_clips_by`` into a concrete slot permutation.

    For ``capture_time`` the order comes from the capture-time facts the SERVER
    put on each slot, never from anything the model wrote. Clips with no capture time keep their
    slot (``order_by_capture_time`` semantics) and are reported in the reply;
    ties keep their current relative order. ``custom`` takes the model's
    ``order`` (current slot indexes in their new sequence) for asks no server
    fact grounds ("reverse it", "swap 2 and 5"); it must name every clip still
    in the edit exactly once (listing removed slots too is accepted), and
    removed slots keep their place. ``random`` shuffles the clips still in the edit with a
    seed: the requirement's stored one when the planner passes it, else one derived from the
    clips' identities, so the same timeline always shuffles the same way (a retry, a replay
    and a re-render agree) and the model never authors an order.
    """
    from app.agents import edit_copilot as ec  # noqa: PLC0415
    from app.services.clip_facts import (  # noqa: PLC0415
        capture_time_from_facts,
        ordered_capture_media,
    )

    criterion = payload.get("criterion")
    direction = payload.get("direction", "asc")
    if criterion not in _ORDER_CRITERIA or (
        criterion == "capture_time" and direction not in _ORDER_DIRECTIONS
    ):
        state.invalid_value()
        return None
    slots = [s for s in ec._snapshot_list(snapshot, ec._SLOT_INDEX_KEYS) if isinstance(s, dict)]
    if len(slots) < 2:
        state.reject(
            op=name, reason="capability_unavailable", detail="there is only one clip to order"
        )
        return None
    if criterion == "random":
        from app.services.order_mode import derived_seed, seeded_permutation  # noqa: PLC0415

        live = [index for index, slot in enumerate(slots) if not slot.get("removed")]
        media_ids = [str(s.get("media_id") or "") for s in slots]
        raw_seed = payload.get("seed")
        seed = (
            raw_seed
            if isinstance(raw_seed, int) and not isinstance(raw_seed, bool) and raw_seed >= 0
            else derived_seed("|".join(media_ids[i] for i in live), None)
        )
        permutation = list(range(len(slots)))
        for position, index in zip(live, seeded_permutation(live, seed), strict=True):
            permutation[position] = index
        if permutation == list(range(len(slots))):
            state.reorder_noop = True
            state.reject(
                op=name, reason="capability_unavailable", detail="there is only one clip to shuffle"
            )
            return None
        return {
            "criterion": criterion,
            "seed": seed,
            "permutation": permutation,
            "media_ids": media_ids,
        }
    if criterion == "custom":
        live = [index for index, slot in enumerate(slots) if not slot.get("removed")]
        order = payload.get("order")
        if (
            not isinstance(order, list)
            or any(isinstance(i, bool) or not isinstance(i, int) for i in order)
            or sorted(order) not in (live, list(range(len(slots))))
        ):
            state.invalid_value(
                say(
                    en="The new order has to list every clip in the edit exactly once.",
                    tr="Yeni sıra, videodaki her klibi tam bir kez içermeli.",
                )
            )
            return None
        # The model may list removed slots too (it reads every slot); they keep their place.
        order = [index for index in order if index in live]
        permutation = list(range(len(slots)))
        for position, index in zip(live, order, strict=True):
            permutation[position] = index
        if permutation == list(range(len(slots))):
            state.reorder_noop = True
            state.reply_notes.append(
                say(en="The clips are already in that order.", tr="Klipler zaten bu sırada.")
            )
            state.reject(
                op=name,
                reason="capability_unavailable",
                detail="the clips are already in that order",
            )
            return None
        return {
            "criterion": criterion,
            "permutation": permutation,
            "media_ids": [str(s.get("media_id") or "") for s in slots],
        }
    times: dict[str, Any] = {}
    active: list[int] = []
    for index, slot in enumerate(slots):
        if slot.get("removed"):
            continue
        active.append(index)
        facts = slot.get("facts")
        moment = (
            capture_time_from_facts([f for f in facts if isinstance(f, dict)])
            if isinstance(facts, list)
            else None
        )
        if moment is not None:
            times[str(index)] = moment
    untimed = [str(n + 1) for n, index in enumerate(active) if str(index) not in times]
    if len(times) < 2:
        state.reply_notes.append(
            say(
                en="I couldn't order them by filming time: "
                + (
                    "none of your clips carries a filming time."
                    if not times
                    else "only one of your clips carries a filming time."
                ),
                tr="Klipleri çekim saatine göre sıralayamadım: "
                + (
                    "hiçbir klibinde çekim saati yok."
                    if not times
                    else "yalnızca bir klibinde çekim saati var."
                ),
            )
        )
        state.reject(
            op=name,
            reason="capability_unavailable",
            detail="fewer than two clips carry a capture time",
        )
        return None
    result = ordered_capture_media(
        [str(i) for i in range(len(slots))], times, descending=direction == "desc"
    )
    permutation = [int(i) for i in result.ordered_ids]
    if permutation == list(range(len(slots))):
        state.reorder_noop = True
        state.reply_notes.append(
            say(
                en="The clips are already in "
                + ("newest-first" if direction == "desc" else "chronological")
                + " order.",
                tr="Klipler zaten "
                + ("en yeniden eskiye" if direction == "desc" else "çekim sırasına göre")
                + " sıralı.",
            )
        )
        state.reject(
            op=name, reason="capability_unavailable", detail="the clips are already in that order"
        )
        return None
    if untimed:
        state.reply_notes.append(
            say(
                en=f"No filming time for clip{'s' if len(untimed) != 1 else ''} "
                f"{', '.join(untimed)}.",
                tr=f"Şu kliplerde çekim saati yok: {', '.join(untimed)}."
                if len(untimed) != 1
                else f"{untimed[0]}. klipte çekim saati yok.",
            )
        )
        state.reply_notes.append(
            say(
                en="Clips without one stay where they are.",
                tr="Çekim saati olmayan klipler yerinde kalıyor.",
            )
        )
    return {
        "criterion": criterion,
        "direction": direction,
        "permutation": permutation,
        "media_ids": [str(s.get("media_id") or "") for s in slots],
    }


SPECS: list[OpSpec] = [
    OpSpec(
        name="reorder_clips_by",
        required=frozenset({"criterion"}),
        fields=frozenset({"criterion", "direction", "order"}),
        family=_FAMILY,
        coerce=_coerce_reorder_clips_by,
    ),
    OpSpec(
        name="patch_slots",
        required=frozenset({"selector", "patch"}),
        fields=frozenset({"selector", "patch", "preserve_text"}),
        family=_FAMILY,
        coerce=_coerce_patch_slots,
    ),
    OpSpec(
        name="set_total_duration",
        required=frozenset({"target_s"}),
        fields=frozenset({"target_s", "strategy", "preserve_text"}),
        family=_FAMILY,
        coerce=_coerce_set_total_duration,
    ),
]


# ── Compile handlers ──────────────────────────────────────────────────────────


def _ops():
    from app.services import kria_editor_ops  # noqa: PLC0415

    return kria_editor_ops


def _err(message: str):
    return _ops().KriaEditorOpError(message)


def _frame(value: float) -> float:
    return round(round(value * _FPS) / _FPS, 6)


def _is_image(row: dict[str, Any]) -> bool:
    return row.get("media_kind") == "image"


def _clip_capability(state: Any, key: str, *, default: bool = True) -> bool:
    caps = _ops()._editor_capabilities(state.job, state.variant)
    clips = caps.get("clips") if isinstance(caps, dict) else None
    entry = clips.get(key) if isinstance(clips, dict) else None
    if entry is None:
        return default
    return entry is True or (isinstance(entry, dict) and entry.get("editable") is True)


def _select(state: Any, selector: dict[str, Any]) -> list[int]:
    rows = state.slots
    active = [i for i, row in enumerate(rows) if not row.get("removed")]
    if selector.get("all"):
        chosen = active
    elif "slot_indexes" in selector:
        indexes = [_ops()._snapshot_slot_index(state, i) for i in selector["slot_indexes"]]
        chosen = [i for i in indexes if i in active]
    elif "clip_ids" in selector:
        wanted = set(selector["clip_ids"])
        chosen = [i for i in active if str(rows[i].get("media_id")) in wanted]
    else:
        wanted_kind = selector.get("media_kind")
        chosen = [i for i in active if (rows[i].get("media_kind") or "video") == wanted_kind]
    if not chosen:
        raise _err("No matching clips on the timeline")
    return chosen


def _slide_floor(rows: list[dict[str, Any]], row: dict[str, Any], ops: Any) -> float:
    """The earliest source second ``row`` may slide back to: the end of the latest window of
    another clip cut from the same file that finishes before ``row`` starts, so lengthening one
    moment never replays the footage of its neighbour."""
    key = row.get("clip_index")
    start = float(row.get("in_s") or 0.0)
    if key is None:
        return 0.0
    ends = [
        float(other.get("in_s") or 0.0) + ops._slot_duration(other) * ops._slot_rate(other)
        for other in rows
        if other is not row and not other.get("removed") and other.get("clip_index") == key
    ]
    return max((end for end in ends if end <= start + 1e-6), default=0.0)


def _fit_window(
    source: float,
    in_s: float,
    duration: float,
    rate: float,
    *,
    slide: bool,
    floor: float = 0.0,
) -> tuple[float, float]:
    """Fit a clip of ``duration`` output seconds into a ``source``-second file.

    The clip plays ``duration * rate`` source seconds starting at ``in_s``. When that runs
    past the end and ``slide`` is set, the start moves earlier so the clip ends at the end
    of the source (everything it already showed stays in), but never before ``floor``; the
    duration is capped at the footage between ``floor`` and the end. Without ``slide`` the
    duration is clamped to the footage after ``in_s``.
    Invariant: ``floor <= in_s`` and ``in_s + duration * rate <= source``.
    """
    room = (source - in_s) / rate
    if duration <= room + 1e-9:
        return in_s, duration
    if not slide:
        return in_s, math.floor(room * _FPS) / _FPS
    duration = min(duration, math.floor((source - floor) / rate * _FPS) / _FPS)
    return max(floor, source - duration * rate), duration


def _tilted(row: dict[str, Any], patch: dict[str, Any], *, clip_number: int) -> dict | None:
    """The ``tilt`` row value ``row`` ends with: its current tilt, changed by ``patch["tilt"]``.

    A turn and a backdrop are independent: an angle keeps the backdrop, a backdrop keeps the
    angle, and a relative angle adds to this clip's own. ``None`` means upright on a plain
    background. A clip with a look cannot be tilted (the engine refuses a look on a
    non-identity transform), so a look is refused here instead of vanishing at render time.
    """
    change = patch["tilt"]
    if change is None:
        return None
    look = patch["look_preset"] if "look_preset" in patch else row.get("look_preset")
    if look not in (None, "none"):
        raise _err(f"Clip {clip_number} has a look, so it can't be tilted. Remove the look first")
    if "backdrop" in change and row.get("layout") == "supporting_card":
        raise _err("A photo shown as a card can turn, but its background can't be changed")
    current = ClipTilt.from_row(row.get("tilt"))
    rotation = current.rotation_deg
    if "rotation_deg" in change:
        rotation = change["rotation_deg"]
    elif "rotation_delta_deg" in change:
        rotation = wrap_degrees(rotation + change["rotation_delta_deg"])
    backdrop = change["backdrop"] if "backdrop" in change else current.backdrop
    return ClipTilt.model_validate({"rotation_deg": rotation, "backdrop": backdrop}).row()


def _op_patch_slots(state: Any, op: dict[str, Any]) -> None:
    ops = _ops()
    patch = dict(op.get("patch") or {})
    on_device = state.variant.get("render_destination") == "device"
    source_window_timeline = ops._is_guided_native(
        state.job, state.variant
    ) or ops._is_authored_phone_native(state.job, state.variant)
    if "look_preset" in patch and (on_device or not _clip_capability(state, "looks")):
        raise _err("Looks are not available for this edit")
    if "playback_rate" in patch and (
        not source_window_timeline
        or not _clip_capability(state, "playback_rate", default=not on_device)
    ):
        raise _err("Speed changes are not available for this edit")
    if "source_crop" in patch and not _clip_capability(state, "source_crop", default=False):
        raise _err("Cropping is not available for this edit")
    if "tilt" in patch and not _clip_capability(state, "tilt", default=False):
        raise _err("Tilting clips isn't available for this edit")
    if ("transition_after" in patch or "transition_duration_s" in patch) and (
        not _clip_capability(state, "transitions")
    ):
        raise _err("Transitions are not available for this edit")

    indexes = _select(state, dict(op.get("selector") or {}))
    rows = state.slots
    active = [i for i, row in enumerate(rows) if not row.get("removed")]
    last_active = active[-1] if active else -1
    applied = 0
    tilt_unchanged = False
    duration_only_on_cut = False
    for index in indexes:
        row = rows[index]
        if patch.get("removed") is True:
            if sum(not r.get("removed") for r in rows) <= 1:
                raise _err("The final clip cannot be removed")
            row["removed"] = True
            applied += 1
            continue
        image = _is_image(row)
        touched = False
        rate_old = ops._slot_rate(row)
        rate = float(patch["playback_rate"]) if "playback_rate" in patch and not image else rate_old
        duration = ops._slot_duration(row)
        in_s = float(row.get("in_s") or 0.0)
        source_span = (
            float(row["source_end_s"]) - in_s
            if row.get("source_end_s") is not None
            else duration * rate_old
        )
        if "in_s" in patch and not image:
            in_s = float(patch["in_s"])
            row["in_s"] = in_s
            touched = True
        if "playback_rate" in patch and not image:
            row["playback_rate"] = rate
            if "duration_s" not in patch:
                # "speed up clip 2" keeps the same footage, so the clip gets shorter.
                duration = source_span / rate
            row["source_end_s"] = in_s + source_span
            touched = True
        if "duration_s" in patch:
            duration = float(patch["duration_s"])
            touched = True
        if touched and "source_end_s" in row:
            row["source_end_s"] = in_s + (duration * rate if "duration_s" in patch else source_span)
        if touched:
            requested_duration = duration
            source = row.get("source_duration_s")
            if not image and isinstance(source, (int, float)) and source > 0:
                # A start point the creator (or model) chose is kept; otherwise a clip
                # that outgrows the footage after its start reaches back into the footage
                # before it.
                slide = "in_s" not in patch and "playback_rate" not in patch
                if not slide and (float(source) - in_s) / rate < MIN_CLIP_S:
                    raise _err("That start point leaves too little footage in the clip")
                new_in, duration = _fit_window(
                    float(source),
                    in_s,
                    duration,
                    rate,
                    slide=slide,
                    floor=_slide_floor(rows, row, ops) if slide else 0.0,
                )
                if new_in != in_s:
                    row["in_s"] = round(new_in, 3)
            if duration < MIN_CLIP_S:
                raise _err("A clip cannot be shorter than 0.1 seconds")
            row["duration_beats"] = None
            row["duration_s"] = round(duration, 6 if "source_end_s" in row else 3)
            if "source_end_s" in row and (
                duration != requested_duration or float(row.get("in_s") or 0) != in_s
            ):
                row["source_end_s"] = float(row.get("in_s") or 0) + duration * rate
            applied += 1
        if "look_preset" in patch:
            row["look_preset"] = patch["look_preset"]
            row["look_adjustments"] = None
            applied += 1
        if "source_crop" in patch:
            if patch["source_crop"] is not None and row.get("layout") == "supporting_card":
                # A card photo is drawn whole on the phone; there is nothing to crop.
                raise _err("A photo shown as a card can't be cropped")
            row["source_crop"] = patch["source_crop"]
            applied += 1
        if "tilt" in patch:
            tilted = _tilted(row, patch, clip_number=active.index(index) + 1)
            if tilted == ClipTilt.from_row(row.get("tilt")).row():
                tilt_unchanged = True
            else:
                row["tilt"] = tilted
                applied += 1
        if ("transition_after" in patch or "transition_duration_s" in patch) and (
            index != last_active
        ):
            current = row.get("transition_after") or "cut"
            transition = patch.get("transition_after", current)
            if transition == "cut":
                if "transition_after" not in patch:
                    # Duration-only patch on a hard cut has nothing to lengthen:
                    # not applied (reported honestly below), row untouched.
                    duration_only_on_cut = True
                    continue
                row["transition_after"] = "cut"
                row["transition_duration_s"] = None
            else:
                seconds = patch.get("transition_duration_s") or row.get("transition_duration_s")
                row["transition_after"] = transition
                row["transition_duration_s"] = float(seconds or 0.3)
            applied += 1
    if not applied:
        if tilt_unchanged:
            raise _err(
                "Those clips are already upright"
                if patch["tilt"] is None
                else "Those clips are already tilted that way"
            )
        if last_active in indexes and (
            "transition_after" in patch or "transition_duration_s" in patch
        ):
            raise _err("The last clip has no transition after it")
        if duration_only_on_cut:
            raise _err("Those clips have a hard cut, so there is no transition to lengthen")
        raise _err("That change does not apply to the selected clips")
    state.changed.add("timeline")


def _plan_total(durations: list[float], rows: list[dict[str, Any]]) -> float:
    from app.config import settings  # noqa: PLC0415
    from app.services.guided_timeline import effective_transitions  # noqa: PLC0415

    if not durations:
        return 0.0
    if settings.edit_transitions_enabled:
        eff = effective_transitions(
            durations,
            [
                (
                    str(row.get("transition_after") or "cut"),
                    float(row.get("transition_duration_s") or 0.0),
                )
                for row in rows
            ],
        )
    else:
        eff = [("cut", 0.0)] * len(durations)
    return sum(durations) - sum(overlap for _t, overlap in eff[:-1])


def _bounds(
    row: dict[str, Any], ops: Any, rows: list[dict[str, Any]] | None = None
) -> tuple[float, float]:
    current = ops._slot_duration(row)
    low = min(MIN_RETARGET_CLIP_S, current)
    high = _max_total_s()
    source = row.get("source_duration_s")
    if not _is_image(row) and isinstance(source, (int, float)) and source > 0:
        # The whole source after any neighbouring window cut from the same file, not just the
        # footage after the start: a longer clip slides its start earlier (``_fit_window``).
        rate = ops._slot_rate(row)
        floor = _slide_floor(rows, row, ops) if rows else 0.0
        high = min(high, math.floor((float(source) - floor) / rate * _FPS) / _FPS)
    return low, max(low, high)


def _write_durations(state: Any, idx: list[int], durations: list[float]) -> None:
    ops = _ops()
    source_clock = any(state.slots[i].get("source_end_s") is not None for i in idx)
    cumulative = 0.0
    written = 0.0
    for pos, i in enumerate(idx):
        row = state.slots[i]
        source = row.get("source_duration_s")
        if not _is_image(row) and isinstance(source, (int, float)) and source > 0:
            in_s = float(row.get("in_s") or 0.0)
            new_in, durations[pos] = _fit_window(
                float(source),
                in_s,
                durations[pos],
                ops._slot_rate(row),
                slide=True,
                floor=_slide_floor(state.slots, row, ops),
            )
            if new_in != in_s:
                row["in_s"] = round(new_in, 3)
        cumulative += durations[pos]
        edge = round(cumulative, 3)
        persisted_duration = round(durations[pos], 6) if source_clock else round(edge - written, 3)
        written = edge
        row["duration_beats"] = None
        row["duration_s"] = persisted_duration
        if row.get("source_end_s") is not None:
            row["source_end_s"] = round(
                float(row.get("in_s") or 0.0) + persisted_duration * ops._slot_rate(row), 6
            )
    state.changed.add("timeline")


def _trim_tail(state: Any, ops: Any, target: float, active_idx: list[int]) -> None:
    idx = list(active_idx)
    d = [ops._slot_duration(state.slots[i]) for i in idx]

    def rows() -> list[dict[str, Any]]:
        return [state.slots[i] for i in idx]

    for _ in range(4 + len(idx)):
        gap = target - _plan_total(d, rows())
        if abs(gap) <= _TOL_S:
            break
        tail = len(idx) - 1
        low, _high = _bounds(state.slots[idx[tail]], ops, state.slots)
        if gap < 0:
            wanted = d[tail] + gap
            if wanted >= low - 1e-9:
                d[tail] = _frame(max(low, wanted))
            elif len(idx) > 1:
                state.slots[idx[tail]]["removed"] = True
                idx.pop()
                d.pop()
            else:
                d[tail] = _frame(low)
                break
        else:
            grown = False
            for pos in range(len(idx) - 1, -1, -1):
                _low, high = _bounds(state.slots[idx[pos]], ops, state.slots)
                if high - d[pos] > 1e-6:
                    d[pos] = _frame(min(high, d[pos] + gap))
                    grown = True
                    break
            if not grown:
                break
    reached = _plan_total(d, rows())
    if abs(target - reached) > 0.15:
        raise _err(f"Cannot reach that length by trimming the end; closest is {reached:.1f} s")
    _write_durations(state, idx, d)


def _op_set_total_duration(state: Any, op: dict[str, Any]) -> None:
    ops = _ops()
    target = float(op["target_s"])
    strategy = str(op.get("strategy") or "proportional")
    idx = [i for i, row in enumerate(state.slots) if not row.get("removed")]
    if not idx:
        raise _err("The timeline has no clips")
    rows = [state.slots[i] for i in idx]
    bounds = [_bounds(row, ops, state.slots) for row in rows]
    lows = [b[0] for b in bounds]
    highs = [b[1] for b in bounds]
    longest = _plan_total(highs, rows)
    if target > longest + _TOL_S:
        raise _err(
            f"Longest possible is {math.floor(longest * 10) / 10:.1f} s with the footage available"
        )
    if strategy == "trim_tail":
        _trim_tail(state, ops, target, idx)
        return
    shortest = _plan_total(lows, rows)
    if target < shortest - _TOL_S:
        raise _err(
            f"Shortest possible is {math.ceil(shortest * 10) / 10:.1f} s with every clip at "
            f"{MIN_RETARGET_CLIP_S:g} s (use trim_tail to drop clips instead)"
        )
    d = [ops._slot_duration(row) for row in rows]
    if strategy == "uniform":
        overlaps = sum(d) - _plan_total(d, rows)
        share = (target + overlaps) / len(d)
        d = [min(max(share, lo), hi) for lo, hi in bounds]
    for _ in range(4):
        gap = target - _plan_total(d, rows)
        if abs(gap) <= _TOL_S / 5:
            break
        free = [
            k
            for k in range(len(d))
            if (gap > 0 and d[k] < highs[k] - 1e-9) or (gap < 0 and d[k] > lows[k] + 1e-9)
        ]
        if not free:
            break
        weights = [1.0 if strategy == "uniform" else max(d[k], 1e-6) for k in free]
        scale = sum(weights)
        for k, weight in zip(free, weights, strict=True):
            d[k] = min(highs[k], max(lows[k], d[k] + gap * weight / scale))
    d = [_frame(min(highs[k], max(lows[k], d[k]))) for k in range(len(d))]
    # Quantizing to frames can leave a sliver; push it onto the clip with most room.
    gap = target - _plan_total(d, rows)
    if abs(gap) > 1e-6:
        room = [(highs[k] - d[k]) if gap > 0 else (d[k] - lows[k]) for k in range(len(d))]
        k = max(range(len(d)), key=lambda i: room[i])
        d[k] = _frame(min(highs[k], max(lows[k], d[k] + gap)))
    residual = target - _plan_total(d, rows)
    if 0 < abs(residual) <= len(d) * 0.000001:
        for k in range(len(d) - 1, -1, -1):
            corrected = round(d[k] + residual, 6)
            if lows[k] <= corrected <= highs[k]:
                d[k] = corrected
                break
    if abs(target - _plan_total(d, rows)) > 0.15:
        raise _err("Cannot reach that length with the footage available")
    _write_durations(state, idx, d)


def _op_reorder_clips_by(state: Any, op: dict[str, Any]) -> None:
    permutation = op.get("permutation")
    media_ids = op.get("media_ids")
    rows = state.slots
    if (
        not isinstance(permutation, list)
        or not isinstance(media_ids, list)
        or len(permutation) != len(rows)
        or len(media_ids) != len(rows)
        or sorted(permutation) != list(range(len(rows)))
        or any(isinstance(i, bool) or not isinstance(i, int) for i in permutation)
    ):
        raise _err("The timeline changed before this reorder could be drafted")
    if any(str(row.get("media_id") or "") != media_ids[i] for i, row in enumerate(rows)):
        raise _err("The timeline changed before this reorder could be drafted")
    criterion = op.get("criterion")
    if criterion not in _ORDER_CRITERIA:
        raise _err("That ordering is not supported")
    state.slots[:] = [rows[i] for i in permutation]
    state.changed.add("timeline")
    if criterion == "custom":
        state.summary = "Reorder clips"
        return
    if criterion == "random":
        state.summary = "Shuffle clips"
        return
    state.summary = "Order clips by filming time" + (
        ", newest first" if op.get("direction") == "desc" else ""
    )


def register_handlers() -> None:
    """Register compile handlers with kria_editor_ops (lazy import)."""
    ops = _ops()
    ops.register_handler("patch_slots", _op_patch_slots)
    ops.register_handler("set_total_duration", _op_set_total_duration)
    ops.register_handler("reorder_clips_by", _op_reorder_clips_by)
