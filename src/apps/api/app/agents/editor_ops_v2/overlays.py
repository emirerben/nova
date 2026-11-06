"""Editor ops v2, overlay lane (KRI-661): picture cards on a phone Talking edit.

A Talking edit shows the creator's photos and videos either ON TOP of the speaker
(a small card) or FULL SCREEN (a cutaway). Chat could not change either: the
family was never offered for a phone edit and no handler compiled the legacy
``patch_overlay`` / ``remove_overlay`` ops. This lane adds:

* ``set_overlay_display``: one op that flips every card (or every photo, every
  video, or one card) between ``pip`` and ``fullscreen`` -- "put all my photos on
  top" must not cost thirty ``patch_overlay`` ops.
* compile handlers for ``patch_overlay`` and ``remove_overlay``.

Cards are addressed by ``overlay_index`` into ``snapshot.overlays.cards``. The Save
(`phone_subtitled_editor._overlays_from_sections`) is the authority on what a card
may become; the handlers here only keep the draft honest (valid ranges, no two
full-screen cards on screen at once) so a bad ask fails as a plain sentence in chat
instead of a 422 on Save.

Do not import app.agents.edit_copilot / app.services.kria_editor_ops at module top
level (circular); import lazily inside functions.
"""

from __future__ import annotations

import math
from typing import Any

from app.agents._schemas.visual_editor import VisualEditorStyle
from app.agents.editor_ops_v2 import OpSpec
from app.kria.clip_tilt import wrap_degrees
from app.kria.reply_language import say

DISPLAY_MODES = ("pip", "fullscreen")
KINDS = ("image", "video", "all")
# Where a card lands when it leaves full screen: the upper-right corner, clear of the
# caption band and of a centred face (the same default the card matcher starts from).
PIP_DEFAULT_GEOMETRY = {"x_frac": 0.74, "y_frac": 0.22, "scale": 0.36}
PATCHABLE = ("start_s", "end_s", "x_frac", "y_frac", "scale", "display_mode")
# KRI-1141: a card's tilt, clockwise on screen like `patch_slots.tilt`. `rotation_deg` replaces the
# card's angle, `rotation_delta_deg` adds to it. Both are patch_overlay keys only while the snapshot
# opens `overlays.tilt`; they are stored as the card's `editor_style.rotation_deg`.
TILT_KEYS = ("rotation_deg", "rotation_delta_deg")
TILT_MAX_TURN_DEG = 360.0


def tilt_open(snapshot: object) -> bool:
    """Does this snapshot offer to tilt a picture card (`overlays.tilt`)?"""
    overlays = snapshot.get("overlays") if isinstance(snapshot, dict) else None
    return isinstance(overlays, dict) and overlays.get("tilt") is True


def coerce_tilt_keys(patch: dict, state: Any) -> dict | None:
    """The cleaned tilt keys of a `patch_overlay` patch, ``{}`` when it has none, None if invalid.

    An absolute angle is wrapped into -180..180 here; a relative one is added to the card's own
    angle when the op is applied. Either may be up to a full turn either way.
    """
    if "rotation_deg" in patch and "rotation_delta_deg" in patch:
        state.invalid_value(_tilt_shape_note())
        return None
    clean: dict[str, float] = {}
    for key in TILT_KEYS:
        if key not in patch:
            continue
        value = patch[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or abs(value) > TILT_MAX_TURN_DEG
        ):
            state.invalid_value(
                say(
                    en="A tilt needs an angle in degrees, up to a full turn to the left or right.",
                    tr="Eğim için en fazla tam bir tur sola veya sağa olacak şekilde derece "
                    "cinsinden bir açı gerekiyor.",
                )
            )
            return None
        clean[key] = wrap_degrees(float(value)) if key == "rotation_deg" else float(value)
    return clean


def _tilt_shape_note() -> str:
    return say(
        en="A tilt is one angle: either the angle to set or the degrees to add, not both.",
        tr="Eğim tek bir açıdır: ya ayarlanacak açı ya da eklenecek derece, ikisi birden değil.",
    )


def card_rotation(card: dict[str, Any]) -> float:
    style = card.get("editor_style")
    value = style.get("rotation_deg") if isinstance(style, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _set_card_rotation(card: dict[str, Any], degrees: float) -> None:
    """Store ``degrees`` as the card's angle; upright removes the style, like an untouched card."""
    if degrees == 0:
        card.pop("editor_style", None)
        return
    card["editor_style"] = VisualEditorStyle(rotation_deg=degrees).model_dump()


def _coerce_set_overlay_display(
    name: str, payload: dict, snapshot: dict, state: Any
) -> dict | None:
    out = {"op": name}
    if payload.get("display_mode") not in DISPLAY_MODES:
        state.invalid_value()
        return None
    out["display_mode"] = payload["display_mode"]
    if "overlay_index" in payload:
        out["overlay_index"] = payload["overlay_index"]
    kind = payload.get("kind", "all")
    if kind not in KINDS:
        state.invalid_value()
        return None
    out["kind"] = kind
    return out


SPECS: list[OpSpec] = [
    OpSpec(
        name="set_overlay_display",
        required=frozenset({"display_mode"}),
        fields=frozenset({"display_mode", "kind", "overlay_index"}),
        family=frozenset({"overlay", "overlays", "media"}),
        coerce=_coerce_set_overlay_display,
    ),
]


def _placeholder_geometry(card: dict[str, Any]) -> bool:
    """A full-screen card carries stand-in geometry (centred over the speaker)."""
    return all(
        abs(float(card.get(key, default)) - default) < 1e-6
        for key, default in (("x_frac", 0.5), ("y_frac", 0.4), ("scale", 0.35))
    )


def _fullscreen_allowed() -> bool:
    from app.config import settings  # noqa: PLC0415

    return bool(getattr(settings, "phone_subtitled_fullscreen_overlays_enabled", True))


def _check_fullscreen(state: Any, touched: list[dict[str, Any]]) -> None:
    """Refuse, in plain words, a full-screen card the render would drop or stack.

    Mirrors `_compile_fullscreen_track` (phone_subtitled_plan): windows are clamped to the
    speaker's length, a window under the minimum is silently skipped, and two cutaways may
    not overlap. Only the cards this op touched can raise, so an edit that already carried
    an odd (but compilable) pair is never refused for something the creator did not change.
    """
    from app.pipeline.phone_subtitled_plan import _MIN_FULLSCREEN_S  # noqa: PLC0415
    from app.routes.generative_jobs import visual_block_variant_duration  # noqa: PLC0415
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    total = visual_block_variant_duration(state.variant) or 0.0

    def window(card: dict[str, Any]) -> tuple[float, float]:
        start = max(float(card.get("start_s") or 0.0), 0.0)
        end = float(card.get("end_s") or 0.0)
        return start, min(end, total) if total > 0 else end

    touched_ids = {id(c) for c in touched if c.get("display_mode") == "fullscreen"}
    for card in state.media_overlays:
        if id(card) in touched_ids:
            start, end = window(card)
            if end - start < _MIN_FULLSCREEN_S:
                raise KriaEditorOpError(
                    "A full-screen picture needs at least a third of a second on screen, "
                    "inside the video -- lengthen it or keep it on top of the video"
                )
    spans = [
        (start, end, card)
        for card in state.media_overlays
        if card.get("display_mode") == "fullscreen"
        for start, end in [window(card)]
        if end - start >= _MIN_FULLSCREEN_S
    ]
    holder: dict[str, Any] | None = None  # the earlier card whose window reaches furthest
    reach = 0.0
    for start, end, card in sorted(spans, key=lambda row: (row[0], row[1])):
        if (
            holder is not None
            and start < reach - 1e-6
            and (id(card) in touched_ids or id(holder) in touched_ids)
        ):
            raise KriaEditorOpError(
                "Two full-screen pictures would be on screen at the same time -- keep one "
                "of them on top of the video or move one"
            )
        if end > reach:
            holder, reach = card, end


def _to_pip(card: dict[str, Any]) -> None:
    card["display_mode"] = "pip"
    # Only a stand-in position is replaced: a card the creator placed keeps its place and size.
    if _placeholder_geometry(card):
        card.update(PIP_DEFAULT_GEOMETRY)


def _to_fullscreen(card: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    if not _fullscreen_allowed():
        raise KriaEditorOpError("Full-screen pictures are turned off right now")
    card["display_mode"] = "fullscreen"
    # A full-screen cutaway has nothing to turn about, and Save refuses its angle.
    card.pop("editor_style", None)


def _op_set_overlay_display(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError, _require_index  # noqa: PLC0415

    mode = op.get("display_mode")
    if mode not in DISPLAY_MODES:
        raise KriaEditorOpError("Overlay display must be on top or full screen")
    if op.get("overlay_index") is not None:
        index = _require_index(state.media_overlays, op.get("overlay_index"), "Overlay")
        targets = [state.media_overlays[index]]
    else:
        kind = op.get("kind", "all")
        targets = [
            card
            for card in state.media_overlays
            if kind == "all" or (card.get("kind") == "video") == (kind == "video")
        ]
    if not targets:
        raise KriaEditorOpError("There are no pictures to change")
    touched: list[dict[str, Any]] = []
    for card in targets:
        if (card.get("display_mode") or "pip") == mode:
            continue
        _to_pip(card) if mode == "pip" else _to_fullscreen(card)
        touched.append(card)
    if not touched:
        raise KriaEditorOpError("Those pictures already show that way")
    _check_fullscreen(state, touched)
    state.changed.add("media_overlays")


def _op_patch_overlay(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError, _require_index  # noqa: PLC0415

    index = _require_index(state.media_overlays, op.get("overlay_index"), "Overlay")
    card = state.media_overlays[index]
    raw = op.get("patch") if isinstance(op.get("patch"), dict) else {}
    patch = {k: v for k, v in raw.items() if k in PATCHABLE}
    tilt = {k: raw[k] for k in TILT_KEYS if k in raw}
    if not patch and not tilt:
        raise KriaEditorOpError("No portable picture field was supplied")
    start = float(patch.get("start_s", card.get("start_s") or 0.0))
    end = float(patch.get("end_s", card.get("end_s") or 0.0))
    if end <= start:
        raise KriaEditorOpError("A picture needs to stay on screen for some time")
    target = patch.pop("display_mode", None) or (card.get("display_mode") or "pip")
    geometry = {k: patch[k] for k in ("x_frac", "y_frac", "scale") if k in patch}
    if target == "fullscreen" and geometry and set(patch) <= set(geometry):
        raise KriaEditorOpError(
            "A full-screen picture has no position -- put it on top of the video first"
        )
    was_fullscreen = (card.get("display_mode") or "pip") == "fullscreen"
    if tilt and target == "fullscreen":
        raise KriaEditorOpError(
            "A full-screen picture can't be tilted -- put it on top of the video first"
        )
    if target == "pip" and was_fullscreen:
        card.update(geometry)
        _to_pip(card)
    elif target == "fullscreen" and not was_fullscreen:
        _to_fullscreen(card)
    card.update({k: v for k, v in patch.items() if target == "pip" or k in ("start_s", "end_s")})
    if tilt:
        current = card_rotation(card)
        if "rotation_delta_deg" in tilt:
            turned = wrap_degrees(current + tilt["rotation_delta_deg"])
        else:
            turned = wrap_degrees(tilt["rotation_deg"])
        if turned == current:
            raise KriaEditorOpError("That picture is already tilted that way")
        _set_card_rotation(card, turned)
    _check_fullscreen(state, [card])
    state.changed.add("media_overlays")


def _op_remove_overlay(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import _require_index  # noqa: PLC0415

    state.media_overlays.pop(
        _require_index(state.media_overlays, op.get("overlay_index"), "Overlay")
    )
    state.changed.add("media_overlays")


def register_handlers() -> None:
    from app.services import kria_editor_ops  # noqa: PLC0415

    kria_editor_ops.register_handler("set_overlay_display", _op_set_overlay_display)
    kria_editor_ops.register_handler("patch_overlay", _op_patch_overlay)
    kria_editor_ops.register_handler("remove_overlay", _op_remove_overlay)
