"""One caption line's size and place from chat (KRI-1394): the editor's caption style scope.

``patch_caption_line {cue_id | all, size_px?, position_x_frac?, position_y_frac?}`` writes what
the phone's ``NativeEditorSession.writeCaptionStyle`` writes on a Save (``setCaptionGestureSize``
/ ``setCaptionGesturePosition`` / ``setCaptionStyleSize`` / ``placeCaptions`` and the "apply to
all" of ``finishCaptionGesture``):

* one line: ``caption_meta.appearance.line_overrides[<cue id>]`` gains the values;
* every line: ``size_px`` goes to ``caption_meta.size_px`` (and leaves each cue's own size), a
  position to ``caption_meta.appearance``; the same key leaves every line's override.

The phone's clamps apply (size 36-160 px, x 0.05-0.95, y 0.10-0.90, the Save's
``EditorCaptionLinePlacement`` bounds). The device render reads ``line_overrides`` by cue id
(``NativeEditorRenderCompiler``), so a line is addressed by a cue id the snapshot lists;
captions kept as text rows (no cue list) are not offered.

Offered only while the snapshot carries the ``caption_lines`` family (a phone edit whose editor
offers ``caption_style_scope``), so every other snapshot and prompt is byte-identical.
"""

from __future__ import annotations

import copy
import math
from typing import Any

from app.agents.editor_ops_v2 import OpSpec
from app.kria.reply_language import say

FAMILY = "caption_lines"
CLOSED_NOTE = (
    "One caption line can't be sized or moved on its own from chat on this edit.",
    "Bu düzenlemede tek bir altyazı satırını sohbetten ayrı boyutlandıramıyorum ya da "
    "taşıyamıyorum.",
)
# Field -> (low, high), the phone's clamps (`setCaptionGestureSize` / `setCaptionGesturePosition`).
BOUNDS = {
    "size_px": (36.0, 160.0),
    "position_x_frac": (0.05, 0.95),
    "position_y_frac": (0.10, 0.90),
}


def _clamp(key: str, value: object) -> float | int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    low, high = BOUNDS[key]
    clamped = min(max(number, low), high)
    return int(round(clamped)) if key == "size_px" else round(clamped, 4)


def addressable_cue_ids(variant: dict[str, Any]) -> list[str]:
    return [
        row["id"]
        for row in variant.get("caption_cues") or []
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
    ]


def current_appearance(variant: dict[str, Any]) -> dict[str, Any]:
    stored = variant.get("caption_editor_style")
    return copy.deepcopy(stored) if isinstance(stored, dict) else {}


def placement_snapshot(variant: dict[str, Any]) -> dict[str, Any]:
    """Where every caption line sits now and the lines that sit on their own.

    The shared values are what the phone draws (`caption_look_from_variant`, a 78 px default),
    so "bigger" starts from the size on screen, never from a blank.
    """
    from app.pipeline.phone_captions import caption_look_from_variant  # noqa: PLC0415

    appearance = current_appearance(variant)
    look = caption_look_from_variant(variant)
    ids = set(addressable_cue_ids(variant))
    own = {
        cue_id: {key: value for key, value in values.items() if key in BOUNDS}
        for cue_id, values in (appearance.get("line_overrides") or {}).items()
        if cue_id in ids and isinstance(values, dict)
    }
    sizes = {
        row["id"]: row["size_px"]
        for row in variant.get("caption_cues") or []
        if isinstance(row, dict) and row.get("id") in ids and row.get("size_px") is not None
    }
    for cue_id, size in sizes.items():
        own.setdefault(cue_id, {}).setdefault("size_px", size)
    return {
        "size_px": look.text_size_px,
        "position_x_frac": appearance.get("position_x_frac", look.position_x_frac),
        "position_y_frac": appearance.get("position_y_frac", look.position_y_frac),
        "lines": {cue_id: values for cue_id, values in own.items() if values},
    }


def _coerce(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    captions = snapshot.get("captions") if isinstance(snapshot, dict) else None
    placement = captions.get("line_placement") if isinstance(captions, dict) else None
    if not isinstance(placement, dict) or FAMILY not in (snapshot.get("allowed_op_families") or []):
        detail = say(en=CLOSED_NOTE[0], tr=CLOSED_NOTE[1])
        state.reject(op=name, reason="capability_unavailable", detail=detail)
        if detail not in state.reply_notes:
            state.reply_notes.append(detail)
        return None
    every = payload.get("all") is True
    cue_id = payload.get("cue_id")
    if every == (cue_id is not None):
        state.invalid_value("Name one line with cue_id, or set all: true for every line.")
        return None
    out: dict[str, Any] = {"all": True} if every else {}
    if not every:
        ids = {row.get("id") for row in captions.get("cues") or [] if isinstance(row, dict)}
        if not isinstance(cue_id, str) or cue_id not in ids:
            state.reject(
                op=name,
                reason="stale_target",
                detail="cue_id must be one of the CAPTIONS ids",
            )
            return None
        out["cue_id"] = cue_id
    for key in BOUNDS:
        if key in payload:
            value = _clamp(key, payload[key])
            if value is None:
                state.invalid_value(f"{key} must be a number.")
                return None
            out[key] = value
    if not set(BOUNDS) & out.keys():
        state.invalid_value("Set size_px, position_x_frac or position_y_frac.")
        return None
    return out


def _closed_note() -> str:
    """``CLOSED_NOTE`` in the chat's language."""
    return say(en=CLOSED_NOTE[0], tr=CLOSED_NOTE[1])


def _op_patch_caption_line(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    values = {key: op[key] for key in BOUNDS if key in op}
    if not values:
        raise KriaEditorOpError("Set a caption size or place")
    # The Save merges `appearance` key by key over the stored style, so only touched keys go.
    working = state.caption_patch.get("appearance")
    appearance = copy.deepcopy(working) if isinstance(working, dict) else {}
    effective = {**current_appearance(state.variant), **appearance}
    overrides = {
        cue_id: dict(own)
        for cue_id, own in (effective.get("line_overrides") or {}).items()
        if isinstance(own, dict)
    }
    changed = False
    if op.get("all"):
        for key, value in values.items():
            if key == "size_px":
                current = state.caption_patch.get("size_px", state.variant.get("caption_size_px"))
                state.caption_patch["size_px"] = value
                for row in state.captions:
                    if row.pop("size_px", None) is not None:
                        changed = True
                        state.changed.add("captions")
            else:
                current = effective.get(key)
                appearance[key] = value
            changed |= current != value
            for own in overrides.values():
                changed |= own.pop(key, None) is not None
        overrides = {cue_id: own for cue_id, own in overrides.items() if own}
    else:
        cue_id = str(op["cue_id"])
        if cue_id not in {row.get("id") for row in state.captions}:
            raise KriaEditorOpError("That caption line is no longer in this edit")
        own = overrides.get(cue_id, {})
        changed = {**own, **values} != own
        overrides[cue_id] = {**own, **values}
    if not changed:
        raise KriaEditorOpError("The captions already sit that way")
    if "line_overrides" in effective or overrides:
        appearance["line_overrides"] = overrides
    state.caption_patch["appearance"] = appearance
    state.changed.add("caption_meta")
    state.summary = "Changed every caption line" if op.get("all") else "Changed one caption line"


SPECS = [
    OpSpec(
        name="patch_caption_line",
        required=frozenset(),
        fields=frozenset({"cue_id", "all", "size_px", "position_x_frac", "position_y_frac"}),
        family=frozenset({FAMILY}),
        coerce=_coerce,
        closed_note=_closed_note,
    )
]


def register_handlers() -> None:
    from app.services import kria_editor_ops  # noqa: PLC0415

    kria_editor_ops.register_handler("patch_caption_line", _op_patch_caption_line)
