"""The finished video's shape from chat (KRI-1394): the editor's video shape picker.

``set_video_shape {orientation?, landscape_fit?}`` compiles into the editor Save's
``orientation`` ("portrait" | "landscape") and ``landscape_fit`` ("fit" = black bars, "fill" =
crop) sections, what ``NativeEditorSession.setVideoShape`` sends. Each axis is offered exactly
where the editor's capability map opens it on a phone edit (`render_shape.device_editor_offer`:
a Talking edit's orientation behind the landscape flag and its fit, a Voiceover montage's or a
guided edit's fit), so the Save never meets an axis it would refuse. The snapshot's
``video_shape`` section (family ``video_shape``) is present only then, so every other snapshot
and prompt is byte-identical.
"""

from __future__ import annotations

from typing import Any

from app.agents.editor_ops_v2 import OpSpec
from app.kria.reply_language import say

FAMILY = "video_shape"
ORIENTATIONS = ("portrait", "landscape")
FITS = ("fit", "fill")
CLOSED_NOTE = (
    "The video's shape can't be changed from chat on this edit.",
    "Bu düzenlemede videonun biçimini sohbetten değiştiremiyorum.",
)
_AXES = {"orientation": ORIENTATIONS, "landscape_fit": FITS}


def _axis(caps: object, key: str) -> dict[str, Any]:
    value = caps.get(key) if isinstance(caps, dict) else None
    return value if isinstance(value, dict) else {}


def video_shape_snapshot(variant: dict[str, Any], caps: dict[str, Any]) -> dict[str, Any] | None:
    """The shape now and the axes the editor opens, or None when neither is open."""
    if variant.get("render_destination") != "device":
        return None
    orientation, fit = _axis(caps, "orientation"), _axis(caps, "landscape_fit")
    section = {
        "orientation": str(variant.get("orientation") or "portrait"),
        "orientation_editable": orientation.get("editable") is True,
        "landscape_fit": fit.get("value") if fit.get("value") in FITS else "fill",
        "landscape_fit_editable": fit.get("editable") is True,
    }
    if not (section["orientation_editable"] or section["landscape_fit_editable"]):
        return None
    return section


def _refuse(state: Any, name: str) -> None:
    detail = say(en=CLOSED_NOTE[0], tr=CLOSED_NOTE[1])
    state.reject(op=name, reason="capability_unavailable", detail=detail)
    if detail not in state.reply_notes:
        state.reply_notes.append(detail)


def _coerce(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    shape = snapshot.get("video_shape") if isinstance(snapshot, dict) else None
    if not isinstance(shape, dict) or FAMILY not in (snapshot.get("allowed_op_families") or []):
        _refuse(state, name)
        return None
    out: dict[str, Any] = {}
    for key, choices in _AXES.items():
        if key not in payload:
            continue
        if payload[key] not in choices:
            state.invalid_value(f"{key} is one of {', '.join(choices)}.")
            return None
        if shape.get(f"{key}_editable") is not True:
            _refuse(state, name)
            return None
        out[key] = payload[key]
    if not out:
        state.invalid_value("Set orientation or landscape_fit.")
        return None
    return out


def current_shape(job: Any, variant: dict[str, Any]) -> dict[str, Any]:
    from app.routes.generative_jobs import _editor_capabilities  # noqa: PLC0415

    fit = _axis(_editor_capabilities(job, variant), "landscape_fit")
    return {
        "orientation": str(variant.get("orientation") or "portrait"),
        "landscape_fit": fit.get("value") if fit.get("value") in FITS else "fill",
    }


def _closed_note() -> str:
    """``CLOSED_NOTE`` in the chat's language."""
    return say(en=CLOSED_NOTE[0], tr=CLOSED_NOTE[1])


def _op_set_video_shape(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    current = {**current_shape(state.job, state.variant), **(state.video_shape or {})}
    edit = {key: op[key] for key in _AXES if key in op and op[key] != current[key]}
    if not edit:
        raise KriaEditorOpError("The video already has that shape")
    state.video_shape = {**(state.video_shape or {}), **edit}
    state.changed.add(FAMILY)
    state.summary = "Changed the video's shape"


SPECS = [
    OpSpec(
        name="set_video_shape",
        required=frozenset(),
        fields=frozenset({"orientation", "landscape_fit"}),
        family=frozenset({FAMILY}),
        coerce=_coerce,
        closed_note=_closed_note,
    )
]


def register_handlers() -> None:
    from app.services import kria_editor_ops  # noqa: PLC0415

    kria_editor_ops.register_handler("set_video_shape", _op_set_video_shape)
