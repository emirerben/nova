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

from typing import Any

from app.agents.editor_ops_v2 import OpSpec

DISPLAY_MODES = ("pip", "fullscreen")
KINDS = ("image", "video", "all")
# Where a card lands when it leaves full screen: the upper-right corner, clear of the
# caption band and of a centred face (the same default the card matcher starts from).
PIP_DEFAULT_GEOMETRY = {"x_frac": 0.74, "y_frac": 0.22, "scale": 0.36}
PATCHABLE = ("start_s", "end_s", "x_frac", "y_frac", "scale", "display_mode")


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


def _require_fullscreen_sequence(cards: list[dict[str, Any]]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    previous_end = 0.0
    for card in sorted(
        (c for c in cards if c.get("display_mode") == "fullscreen"),
        key=lambda c: (float(c.get("start_s") or 0.0), float(c.get("end_s") or 0.0)),
    ):
        start, end = float(card.get("start_s") or 0.0), float(card.get("end_s") or 0.0)
        if start < previous_end - 1e-6:
            raise KriaEditorOpError(
                "Two full-screen pictures would be on screen at the same time -- keep one "
                "of them on top of the video or move one"
            )
        previous_end = max(previous_end, end)


def _to_pip(card: dict[str, Any], *, keep_geometry: bool) -> None:
    card["display_mode"] = "pip"
    if keep_geometry:
        return
    # A full-screen card carries placeholder geometry (centred, over the face): give it
    # the corner a card normally gets.
    card.update(PIP_DEFAULT_GEOMETRY)


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
    changed = False
    for card in targets:
        before = card.get("display_mode") or "pip"
        if before == mode:
            continue
        if mode == "pip":
            _to_pip(card, keep_geometry=False)
        else:
            card["display_mode"] = "fullscreen"
        changed = True
    if not changed:
        raise KriaEditorOpError("Those pictures already show that way")
    _require_fullscreen_sequence(state.media_overlays)
    state.changed.add("media_overlays")


def _op_patch_overlay(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError, _require_index  # noqa: PLC0415

    index = _require_index(state.media_overlays, op.get("overlay_index"), "Overlay")
    card = state.media_overlays[index]
    raw = op.get("patch")
    patch = {k: v for k, v in (raw if isinstance(raw, dict) else {}).items() if k in PATCHABLE}
    if not patch:
        raise KriaEditorOpError("No portable picture field was supplied")
    start = float(patch.get("start_s", card.get("start_s") or 0.0))
    end = float(patch.get("end_s", card.get("end_s") or 0.0))
    if end <= start:
        raise KriaEditorOpError("A picture needs to stay on screen for some time")
    if patch.get("display_mode") == "pip" and (card.get("display_mode") or "pip") != "pip":
        geometry = {k for k in ("x_frac", "y_frac", "scale") if k in patch}
        _to_pip(card, keep_geometry=len(geometry) == 3)
        patch.pop("display_mode")
    card.update(patch)
    _require_fullscreen_sequence(state.media_overlays)
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
