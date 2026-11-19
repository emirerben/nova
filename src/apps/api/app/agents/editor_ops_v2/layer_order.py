"""Which picture is on top, from chat (KRI-1394): the editor's layer order.

``set_layer_order {layer, z | direction}`` moves one picture within its stack and writes a dense
integer ``z`` (0 = bottom) on every picture of that stack, as ``NativeEditorSession.setLayerZ``
does for ``reorderLayer`` / ``moveLayer`` / ``moveVisualLayer`` / ``setMediaOverlayZIndex``.

Renderer passes are fixed, so ``z`` only orders peers of one pass, and a stack is one of:

* the picture cards ON TOP of a Talking speaker (``media_overlays`` with display ``pip``; the
  pinned recipe draws them by ``(z, start_s, id)``, `phone_subtitled_plan._compile_overlay_track`).
  A full-screen card covers the video and has no place in it.
* the uploaded picture blocks (``visual_blocks`` of kind ``media``; drawn by ``(z, start_s, id)``,
  `phone_editor_visuals.compile_editor_media_track`). Only the blocks the snapshot lists as
  ``visual_media`` (the creator's own) may be moved; the rest keep their relative place.

Text is a pass of its own above both on the phone, so moving text changes nothing there and is not
offered. ``layer`` is a card or picture id from the snapshot's ``layer_order`` section, present
only while a stack holds two or more pictures, so every other snapshot and prompt is unchanged.
"""

from __future__ import annotations

from typing import Any

from app.agents.editor_ops_v2 import OpSpec
from app.kria.reply_language import say

DIRECTIONS = ("front", "back", "forward", "backward")
FULLSCREEN_NOTE = (
    "A full-screen picture covers the video, so it has no place in front of or behind the "
    "pictures on top.",
    "Tam ekran görsel videonun tamamını kaplar; üstteki görsellerin önünde ya da arkasında "
    "bir yeri yok.",
)


def _order_key(row: dict[str, Any]) -> tuple[float, float, str]:
    try:
        z = float(row.get("z") or 0)
    except (TypeError, ValueError):
        z = 0.0
    try:
        start = float(row.get("start_s") or 0)
    except (TypeError, ValueError):
        start = 0.0
    return z, start, str(row.get("id") or "")


def card_stack(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Picture cards on top of the video, bottom first, as the recipe draws them."""
    return sorted(
        (row for row in rows if isinstance(row, dict) and row.get("display_mode") != "fullscreen"),
        key=_order_key,
    )


def picture_stack(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Uploaded picture blocks, bottom first, as the recipe draws them."""
    return sorted(
        (row for row in rows if isinstance(row, dict) and row.get("kind") == "media"),
        key=_order_key,
    )


def layer_order_snapshot(
    cards: list[dict[str, Any]] | None, pictures: list[dict[str, Any]] | None, movable: set[str]
) -> dict[str, list[str]] | None:
    """Ids bottom to top per stack that holds two or more pictures, else None."""
    section: dict[str, list[str]] = {}
    if cards is not None:
        ids = [str(row.get("id")) for row in card_stack(cards) if row.get("id")]
        if len(ids) > 1:
            section["cards"] = ids
    if pictures is not None:
        ids = [str(row["id"]) for row in picture_stack(pictures) if row.get("id") in movable]
        if len(ids) > 1:
            section["pictures"] = ids
    return section or None


def _coerce(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    order = snapshot.get("layer_order") if isinstance(snapshot, dict) else None
    layer = payload.get("layer")
    stacks = order if isinstance(order, dict) else {}
    if not any(isinstance(layer, str) and layer in ids for ids in stacks.values()):
        overlays = snapshot.get("overlays") if isinstance(snapshot, dict) else None
        cards = overlays.get("cards") if isinstance(overlays, dict) else None
        if any(
            isinstance(card, dict)
            and card.get("id") == layer
            and card.get("display_mode") == "fullscreen"
            for card in cards or []
        ):
            detail = say(en=FULLSCREEN_NOTE[0], tr=FULLSCREEN_NOTE[1])
            state.reject(op=name, reason="capability_unavailable", detail=detail)
            if detail not in state.reply_notes:
                state.reply_notes.append(detail)
            return None
        state.reject(
            op=name,
            reason="stale_target",
            detail="layer must be an id listed in LAYER ORDER",
        )
        return None
    has_z, direction = "z" in payload, payload.get("direction")
    if has_z == (direction is not None):
        state.invalid_value(
            "Give either z (0 = bottom) or direction (front, back, forward, backward)."
        )
        return None
    if direction is not None:
        if direction not in DIRECTIONS:
            state.invalid_value("direction is one of front, back, forward, backward.")
            return None
        return {"layer": layer, "direction": direction}
    z = payload["z"]
    if isinstance(z, bool) or not isinstance(z, (int, float)) or z != z:
        state.invalid_value("z is a whole number, 0 = bottom.")
        return None
    return {"layer": layer, "z": max(0, int(round(z)))}


def _destination(index: int, count: int, op: dict[str, Any]) -> int:
    direction = op.get("direction")
    target = {
        "front": count - 1,
        "back": 0,
        "forward": index + 1,
        "backward": index - 1,
    }.get(direction, op.get("z", index))
    return min(max(int(target), 0), count - 1)


def _op_set_layer_order(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import (  # noqa: PLC0415
        KriaEditorOpError,
        _removable_visual_media,
        _visual_media_rows,
    )

    layer = str(op.get("layer") or "")
    cards = card_stack(state.media_overlays)
    pictures_rows = (
        state.visual_blocks
        if state.visual_blocks is not None
        else _visual_media_rows(state.job, state.variant)
    )
    movable = {row.get("id") for row in _removable_visual_media(state.job, state.variant)}
    if any(row.get("id") == layer for row in cards):
        stack, lane = cards, "media_overlays"
    elif layer in movable and any(row.get("id") == layer for row in pictures_rows):
        stack, lane = picture_stack(pictures_rows), "visual_media"
    else:
        raise KriaEditorOpError("That picture is not in a stack of pictures on this edit")
    if len(stack) < 2:
        raise KriaEditorOpError("There is no other picture to put it in front of or behind")
    index = next(i for i, row in enumerate(stack) if row.get("id") == layer)
    destination = _destination(index, len(stack), op)
    if destination == index:
        raise KriaEditorOpError("That picture is already there")
    reordered = list(stack)
    reordered.insert(destination, reordered.pop(index))
    for z, row in enumerate(reordered):
        row["z"] = z
    if lane == "visual_media":
        state.visual_blocks = pictures_rows
    else:
        state.changed.add("media_overlays")
    state.changed.add("layer_order")
    state.summary = "Changed which picture is on top"


SPECS = [
    OpSpec(
        name="set_layer_order",
        required=frozenset({"layer"}),
        fields=frozenset({"layer", "z", "direction"}),
        family=frozenset({"overlay", "visual_media"}),
        coerce=_coerce,
    )
]


def register_handlers() -> None:
    from app.services import kria_editor_ops  # noqa: PLC0415

    kria_editor_ops.register_handler("set_layer_order", _op_set_layer_order)
