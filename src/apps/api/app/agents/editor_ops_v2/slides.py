"""Editor ops v2, slides lane (KRI-301).

Three ops that exist ONLY on the slide-post surface (``snapshot["surface"] ==
"slide_post"``); ``edit_copilot._family_allowed`` refuses them on a video edit:

* ``set_slide_cover{slide_index}``: make slide N (0-based, the ``slots`` index shown
  in CURRENT DRAFT) the post's cover. Resolved to a stable ``slide_id`` here so the
  cover survives a reorder in the same bundle.
* ``set_post_caption{caption}``: replace the post caption ("" clears it).
* ``set_slide_framing{slide_indexes|all, orientation?, focus?, zoom?, reset?}``
  (KRI-44): turn slides vertical/landscape and crop them. The model never writes
  rectangles; ``focus``/``zoom`` are resolved here against the slide's upright
  source shape (``app.kria.framing.crop_for_focus``). Only offered while
  ``snapshot["post"]["framing_enabled"]`` is true.

Do not import app.agents.edit_copilot / app.services.kria_editor_ops at module top
level (circular); import lazily inside functions.
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.editor_ops_v2 import OpSpec

_FAMILY = frozenset({"slides"})
MAX_CAPTION_CHARS = 2200  # mirrors SlidePostDraft.caption
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")


def _active_slots(snapshot: dict) -> list[dict]:
    from app.agents import edit_copilot as ec  # noqa: PLC0415

    return [s for s in ec._snapshot_list(snapshot, ec._SLOT_INDEX_KEYS) if isinstance(s, dict)]


def _coerce_set_slide_cover(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    index = payload.get("slide_index")
    slots = _active_slots(snapshot)
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(slots):
        state.invalid_value()
        return None
    slot = slots[index]
    slide_id = slot.get("media_id")
    if slot.get("removed") or not isinstance(slide_id, str) or not slide_id:
        state.reject(op=name, reason="stale_target", detail="that slide is no longer available")
        return None
    return {"slide_index": index, "slide_id": slide_id}


def _coerce_set_post_caption(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    raw = payload.get("caption")
    if not isinstance(raw, str):
        state.invalid_value()
        return None
    caption = _CONTROL.sub(" ", raw).strip()
    if len(caption) > MAX_CAPTION_CHARS:
        state.invalid_value("the caption is too long")
        return None
    return {"caption": caption}


_ORIENTATIONS = ("portrait", "landscape")


def _canvas_aspect(profile: str, orientation: str) -> float | None:
    from app.pipeline.slide_post.profiles import PLATFORM_PROFILES  # noqa: PLC0415

    spec = PLATFORM_PROFILES.get(profile)  # type: ignore[call-overload]
    if spec is None:
        return None
    if orientation == "landscape":
        if not spec.allows_landscape:
            return None
        from app.kria.framing import LANDSCAPE_CANVAS  # noqa: PLC0415

        return LANDSCAPE_CANVAS[0] / LANDSCAPE_CANVAS[1]
    return spec.canvas[0] / spec.canvas[1]


def _coerce_set_slide_framing(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    from app.kria.framing import FOCI, crop_for_focus  # noqa: PLC0415

    post = snapshot.get("post") if isinstance(snapshot.get("post"), dict) else {}
    if not post.get("framing_enabled"):
        state.reject(
            op=name, reason="capability_unavailable", detail="cropping slides isn't available yet"
        )
        return None
    slots = _active_slots(snapshot)
    if payload.get("all") is True:
        indexes = list(range(len(slots)))
    else:
        raw = payload.get("slide_indexes")
        if (
            not isinstance(raw, list)
            or not raw
            or any(
                isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(slots)
                for i in raw
            )
        ):
            state.invalid_value()
            return None
        indexes = list(dict.fromkeys(raw))
    orientation = payload.get("orientation")
    focus = payload.get("focus")
    zoom = payload.get("zoom")
    reset = payload.get("reset") is True
    if orientation is not None and orientation not in _ORIENTATIONS:
        state.invalid_value()
        return None
    if focus is not None and focus not in FOCI:
        state.invalid_value()
        return None
    if zoom is not None and (isinstance(zoom, bool) or not isinstance(zoom, (int, float))):
        state.invalid_value()
        return None
    if orientation is None and focus is None and zoom is None and not reset:
        state.invalid_value()
        return None
    profile = str(post.get("platform_profile") or "")
    changes: list[dict[str, Any]] = []
    for index in indexes:
        slot = slots[index]
        slide_id = slot.get("media_id")
        if slot.get("removed") or not isinstance(slide_id, str) or not slide_id:
            state.reject(op=name, reason="stale_target", detail="that slide is no longer available")
            return None
        current = (
            slot.get("orientation") if slot.get("orientation") in _ORIENTATIONS else "portrait"
        )
        target = orientation or ("portrait" if reset else current)
        canvas_aspect = _canvas_aspect(profile, target)
        if canvas_aspect is None:
            state.reject(
                op=name,
                reason="capability_unavailable",
                detail="Instagram carousels keep one shape for every slide.",
            )
            return None
        crop: dict | None = None
        if focus is not None or zoom is not None:
            source_aspect = slot.get("source_aspect")
            if isinstance(source_aspect, bool) or not isinstance(source_aspect, (int, float)):
                state.reject(
                    op=name,
                    reason="capability_unavailable",
                    detail="I can't see this photo's shape yet, so crop it by hand.",
                )
                return None
            try:
                crop = crop_for_focus(
                    float(source_aspect),
                    canvas_aspect,
                    focus or "center",
                    float(zoom) if zoom is not None else 1.0,
                )
            except ValueError:
                state.invalid_value()
                return None
        elif not reset and target == current:
            # Nothing to change on this slide (same shape, no new crop).
            continue
        # Changing the shape without a new crop drops the old one: a rectangle
        # chosen for the previous shape would be mis-shaped for the new canvas.
        changes.append({"slide_id": slide_id, "orientation": target, "source_crop": crop})
    if not changes:
        state.invalid_value()
        return None
    return {"changes": changes}


SPECS: list[OpSpec] = [
    OpSpec(
        name="set_slide_cover",
        required=frozenset({"slide_index"}),
        fields=frozenset({"slide_index"}),
        family=_FAMILY,
        coerce=_coerce_set_slide_cover,
    ),
    OpSpec(
        name="set_post_caption",
        required=frozenset({"caption"}),
        fields=frozenset({"caption"}),
        family=_FAMILY,
        coerce=_coerce_set_post_caption,
    ),
    OpSpec(
        name="set_slide_framing",
        required=frozenset(),
        fields=frozenset({"slide_indexes", "all", "orientation", "focus", "zoom", "reset"}),
        family=_FAMILY,
        coerce=_coerce_set_slide_framing,
    ),
]


# ── Compile handlers ──────────────────────────────────────────────────────────


def _op_set_slide_cover(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    slide_id = op.get("slide_id")
    live = {str(row.get("media_id")) for row in state.slots if not row.get("removed")}
    if not isinstance(slide_id, str) or slide_id not in live:
        raise KriaEditorOpError("That slide is no longer in the post")
    state.post["cover_slide_id"] = slide_id
    state.summary = f"Set slide {int(op['slide_index']) + 1} as the cover"


def _op_set_post_caption(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    caption = op.get("caption")
    if not isinstance(caption, str) or len(caption) > MAX_CAPTION_CHARS:
        raise KriaEditorOpError("That caption is not valid")
    state.post["caption"] = caption
    state.summary = "Update the post caption"


def _op_set_slide_framing(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    changes = op.get("changes")
    if not isinstance(changes, list) or not changes:
        raise KriaEditorOpError("That framing change is not valid")
    rows = {str(row.get("media_id")): row for row in state.slots if not row.get("removed")}
    for change in changes:
        row = rows.get(str(change.get("slide_id")))
        if row is None:
            raise KriaEditorOpError("That slide is no longer in the post")
        row["orientation"] = change["orientation"]
        row["source_crop"] = change.get("source_crop")
    state.summary = f"Reframe {len(changes)} slide{'s' if len(changes) != 1 else ''}"


def register_handlers() -> None:
    from app.services import kria_editor_ops  # noqa: PLC0415

    kria_editor_ops.register_handler("set_slide_cover", _op_set_slide_cover)
    kria_editor_ops.register_handler("set_post_caption", _op_set_post_caption)
    kria_editor_ops.register_handler("set_slide_framing", _op_set_slide_framing)
