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
* ``set_post_aspect{aspect}`` (KRI-606): the post-wide size. Only offered while
  ``snapshot["post"]["sizes_enabled"]`` is true; must be one of
  ``snapshot["post"]["allowed_aspects"]``.
* ``set_slide_fit{slide_indexes|all, fit, background?}`` (KRI-606): show a photo whole
  with bars (``fit: "fit"``) or fill the frame (``fit: "fill"``). Same gate.
* ``set_slide_tilt{slide_indexes|all, rotation_deg?, backdrop?, color?, reset?}``
  (KRI-1138): turn a photo or video to any angle (clockwise-positive, -180..180) over a
  blank, colour or blurred backdrop. A SIBLING op of ``set_slide_framing`` rather than
  two more fields on it: framing is photo-only, gated on ``framing_enabled`` and is about
  the crop, while a tilt turns videos too, is gated on ``tilt_enabled`` (MEDIA_TILT_ENABLED
  and a protocol-28 build) and persists in its own ``SlideEdits.tilt`` key. Only offered
  while ``snapshot["post"]["tilt_enabled"]`` is true.

Do not import app.agents.edit_copilot / app.services.kria_editor_ops at module top
level (circular); import lazily inside functions.
"""

from __future__ import annotations

import math
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
    named = payload.get("slide_id")
    if named is not None:
        # KRI-818: the model named the photo by its stable id; it must be the same slide
        # as the pre-edit index, or the two disagree and nothing is guessed.
        if named != slide_id:
            state.invalid_value()
            return None
        return {"slide_index": index, "slide_id": slide_id, "slide_named": True}
    return {"slide_index": index, "slide_id": slide_id}


def resolve_text_slide_target(payload: dict, snapshot: dict, state: Any) -> str | None:
    """``add_text{slide_id|slide_index}`` -> the stable slide id (KRI-818).

    Both name a slide of the post BEFORE this turn's ops (``slide_index`` is the 0-based
    ``slots`` index shown in CURRENT DRAFT). The id is what the compiler binds the text
    to, so it lands on that slide wherever a reorder in the same bundle puts it. A
    target only exists for one-slide text: a window covering several slides is the
    whole-post copy and cannot also name one slide.
    """
    from app.agents.edit_copilot import SLIDE_POST_SURFACE  # noqa: PLC0415
    from app.agents.editor_ops_v2 import surface_of  # noqa: PLC0415

    if surface_of(snapshot) != SLIDE_POST_SURFACE:
        state.invalid_value()
        return None
    slots = _active_slots(snapshot)
    by_id = {s.get("media_id"): s for s in slots if isinstance(s.get("media_id"), str)}
    slide_id = payload.get("slide_id")
    index = payload.get("slide_index")
    if index is not None:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(slots):
            state.invalid_value()
            return None
        from_index = slots[index].get("media_id")
        if slide_id is not None and slide_id != from_index:
            state.invalid_value()
            return None
        slide_id = from_index
    if not isinstance(slide_id, str) or not slide_id:
        state.invalid_value()
        return None
    slot = by_id.get(slide_id)
    if slot is None or slot.get("removed"):
        state.reject(
            op="add_text", reason="stale_target", detail="that slide is no longer available"
        )
        return None
    start, end = payload.get("start_s"), payload.get("end_s")
    if (
        isinstance(start, (int, float))
        and isinstance(end, (int, float))
        and float(end) - float(start) > 1.0 + 1e-6
    ):
        state.invalid_value()
        return None
    return slide_id


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


def _canvas_aspect(profile: str, orientation: str, aspect: str | None = None) -> float | None:
    from app.pipeline.slide_post.profiles import PLATFORM_PROFILES, post_canvas  # noqa: PLC0415

    spec = PLATFORM_PROFILES.get(profile)  # type: ignore[call-overload]
    if spec is None:
        return None
    if orientation == "landscape":
        if not spec.allows_landscape:
            return None
        from app.kria.framing import LANDSCAPE_CANVAS  # noqa: PLC0415

        return LANDSCAPE_CANVAS[0] / LANDSCAPE_CANVAS[1]
    canvas = post_canvas(profile, aspect)  # type: ignore[arg-type]
    return canvas[0] / canvas[1]


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
    post_aspect = post.get("aspect") if isinstance(post.get("aspect"), str) else None
    changes: list[dict[str, Any]] = []
    for index in indexes:
        slot = slots[index]
        if slot.get("media_kind") == "video":
            # Crop and landscape are for photos; video slides keep the plain cover fit
            # on every renderer. "all" quietly skips them, a named video is refused.
            if payload.get("all") is True:
                continue
            state.reject(
                op=name,
                reason="capability_unavailable",
                detail="Videos in a slide post can't be cropped or turned landscape.",
            )
            return None
        slide_id = slot.get("media_id")
        if slot.get("removed") or not isinstance(slide_id, str) or not slide_id:
            state.reject(op=name, reason="stale_target", detail="that slide is no longer available")
            return None
        current = (
            slot.get("orientation") if slot.get("orientation") in _ORIENTATIONS else "portrait"
        )
        target = orientation or ("portrait" if reset else current)
        canvas_aspect = _canvas_aspect(profile, target, post_aspect)
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


_FIT_BACKGROUNDS = ("blur", "black", "white")


def _sizes_enabled(post: Any) -> bool:
    return isinstance(post, dict) and post.get("sizes_enabled") is True


def _coerce_set_post_aspect(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    post = snapshot.get("post") if isinstance(snapshot.get("post"), dict) else {}
    if not _sizes_enabled(post):
        state.reject(
            op=name, reason="capability_unavailable", detail="resizing posts isn't available yet"
        )
        return None
    aspect = payload.get("aspect")
    allowed = post.get("allowed_aspects")
    if not isinstance(aspect, str) or not isinstance(allowed, list) or not allowed:
        state.invalid_value()
        return None
    if aspect not in allowed:
        profile = str(post.get("platform_profile") or "")
        who = "Instagram carousels" if profile == "instagram_carousel" else "TikTok photo posts"
        state.reject(
            op=name,
            reason="capability_unavailable",
            detail=f"{who} can be {', '.join(str(a) for a in allowed)}.",
        )
        return None
    # `post.aspect` is the resolved current size. Asking for the size the post already has must
    # change nothing (in particular it must not drop crops): the handler reads `changed`.
    return {"aspect": aspect, "changed": aspect != post.get("aspect")}


def _coerce_set_slide_fit(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    post = snapshot.get("post") if isinstance(snapshot.get("post"), dict) else {}
    if not _sizes_enabled(post):
        state.reject(
            op=name, reason="capability_unavailable", detail="fitting photos isn't available yet"
        )
        return None
    mode = payload.get("fit")
    background = payload.get("background")
    if mode not in ("fill", "fit"):
        state.invalid_value()
        return None
    if background is not None and background not in _FIT_BACKGROUNDS:
        state.invalid_value()
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
    target = (background or "blur") if mode == "fit" else None
    changes: list[dict[str, Any]] = []
    for index in indexes:
        slot = slots[index]
        if slot.get("media_kind") == "video":
            # Fit is a photo treatment; "all" quietly skips videos, a named one is refused.
            if payload.get("all") is True:
                continue
            state.reject(
                op=name,
                reason="capability_unavailable",
                detail="Videos in a slide post always fill the frame.",
            )
            return None
        slide_id = slot.get("media_id")
        if slot.get("removed") or not isinstance(slide_id, str) or not slide_id:
            state.reject(op=name, reason="stale_target", detail="that slide is no longer available")
            return None
        changes.append({"slide_id": slide_id, "fit": target})
    if not changes:
        state.invalid_value()
        return None
    return {"changes": changes}


_TILT_BACKDROPS = ("blank", "color", "blur")
_HEX_COLOUR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _tilt_enabled(post: Any) -> bool:
    return isinstance(post, dict) and post.get("tilt_enabled") is True


def _slot_tilt(slot: dict) -> dict | None:
    """The slide's current tilt as the snapshot shows it (None = upright)."""
    tilt = slot.get("tilt")
    return tilt if isinstance(tilt, dict) else None


def _coerce_set_slide_tilt(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    post = snapshot.get("post") if isinstance(snapshot.get("post"), dict) else {}
    if not _tilt_enabled(post):
        state.reject(
            op=name, reason="capability_unavailable", detail="tilting slides isn't available yet"
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
    rotation = payload.get("rotation_deg")
    backdrop = payload.get("backdrop")
    colour = payload.get("color")
    reset = payload.get("reset") is True
    if rotation is not None and (
        isinstance(rotation, bool)
        or not isinstance(rotation, (int, float))
        or not math.isfinite(rotation)
    ):
        state.invalid_value()
        return None
    if backdrop is not None and backdrop not in _TILT_BACKDROPS:
        state.invalid_value()
        return None
    if (backdrop == "color") != (colour is not None) or (
        colour is not None and not (isinstance(colour, str) and _HEX_COLOUR.match(colour))
    ):
        # A colour goes with backdrop "color" and only with it, as a #RRGGBB.
        state.invalid_value()
        return None
    if reset and (rotation is not None or backdrop is not None):
        state.invalid_value()
        return None
    if rotation is None and backdrop is None and not reset:
        state.invalid_value()
        return None
    target_rotation: float | None = None
    if rotation is not None:
        # "turn it 200 degrees right" is 160 degrees left: fold into -180..180.
        folded = math.fmod(float(rotation), 360.0)
        folded = folded - 360.0 if folded > 180.0 else folded + 360.0 if folded < -180.0 else folded
        target_rotation = round(folded, 3)
    changes: list[dict[str, Any]] = []
    for index in indexes:
        slot = slots[index]
        slide_id = slot.get("media_id")
        if slot.get("removed") or not isinstance(slide_id, str) or not slide_id:
            state.reject(op=name, reason="stale_target", detail="that slide is no longer available")
            return None
        current = _slot_tilt(slot)
        if reset:
            changes.append({"slide_id": slide_id, "tilt": None})
            continue
        current_backdrop = (
            dict(current["backdrop"])
            if current is not None and isinstance(current.get("backdrop"), dict)
            else {"kind": "blank"}
        )
        if backdrop is not None:
            current_backdrop = {"kind": backdrop, **({"color": colour} if colour else {})}
        degrees = target_rotation
        if degrees is None:
            if current is None:
                # A background with no turn draws nothing; say so instead of storing it.
                state.reject(
                    op=name,
                    reason="capability_unavailable",
                    detail="Turn the slide first, then pick what shows behind it.",
                )
                return None
            degrees = float(current.get("rotation_deg") or 0.0)
        changes.append(
            {
                "slide_id": slide_id,
                "tilt": None
                if degrees == 0
                else {"rotation_deg": degrees, "backdrop": current_backdrop},
            }
        )
    if not changes:
        state.invalid_value()
        return None
    return {"changes": changes}


SPECS: list[OpSpec] = [
    OpSpec(
        name="set_slide_cover",
        required=frozenset({"slide_index"}),
        fields=frozenset({"slide_index", "slide_id"}),
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
    OpSpec(
        name="set_post_aspect",
        required=frozenset({"aspect"}),
        fields=frozenset({"aspect"}),
        family=_FAMILY,
        coerce=_coerce_set_post_aspect,
    ),
    OpSpec(
        name="set_slide_fit",
        required=frozenset({"fit"}),
        fields=frozenset({"slide_indexes", "all", "fit", "background"}),
        family=_FAMILY,
        coerce=_coerce_set_slide_fit,
    ),
    OpSpec(
        name="set_slide_tilt",
        required=frozenset(),
        fields=frozenset({"slide_indexes", "all", "rotation_deg", "backdrop", "color", "reset"}),
        family=_FAMILY,
        coerce=_coerce_set_slide_tilt,
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
    if state.post.get("aspect_changed") and any(c.get("source_crop") for c in changes):
        # The crop was resolved against the OLD size; applying it to the new one would
        # cut the wrong region. Failing honestly beats a silently mis-shaped crop.
        raise KriaEditorOpError("Change the size first, then crop")
    for change in changes:
        row = rows.get(str(change.get("slide_id")))
        if row is None:
            raise KriaEditorOpError("That slide is no longer in the post")
        row["orientation"] = change["orientation"]
        row["source_crop"] = change.get("source_crop")
    state.summary = f"Reframe {len(changes)} slide{'s' if len(changes) != 1 else ''}"


def _op_set_post_aspect(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    aspect = op.get("aspect")
    if not isinstance(aspect, str) or not aspect:
        raise KriaEditorOpError("That size is not valid")
    if op.get("changed") is False:
        state.summary = f"The post is already {aspect}"
        return
    state.post["aspect"] = aspect
    state.post["aspect_changed"] = True
    # A rectangle chosen for the old shape would be mis-shaped for the new canvas,
    # so portrait slides drop their crop (landscape slides keep theirs).
    reset = 0
    for row in state.slots:
        if row.get("removed") or row.get("orientation") == "landscape":
            continue
        if row.get("source_crop") is not None:
            row["source_crop"] = None
            reset += 1
    state.post["aspect_crops_reset"] = int(state.post.get("aspect_crops_reset", 0)) + reset
    state.summary = f"Set the post size to {aspect}"


def _op_set_slide_fit(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    changes = op.get("changes")
    if not isinstance(changes, list) or not changes:
        raise KriaEditorOpError("That fit change is not valid")
    rows = {str(row.get("media_id")): row for row in state.slots if not row.get("removed")}
    for change in changes:
        row = rows.get(str(change.get("slide_id")))
        if row is None:
            raise KriaEditorOpError("That slide is no longer in the post")
        row["fit"] = change.get("fit")
    state.summary = f"Change the fit on {len(changes)} slide{'s' if len(changes) != 1 else ''}"


def _op_set_slide_tilt(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    changes = op.get("changes")
    if not isinstance(changes, list) or not changes:
        raise KriaEditorOpError("That tilt change is not valid")
    rows = {str(row.get("media_id")): row for row in state.slots if not row.get("removed")}
    for change in changes:
        row = rows.get(str(change.get("slide_id")))
        if row is None:
            raise KriaEditorOpError("That slide is no longer in the post")
        row["tilt"] = change.get("tilt")
    state.summary = f"Change the tilt on {len(changes)} slide{'s' if len(changes) != 1 else ''}"


def register_handlers() -> None:
    from app.services import kria_editor_ops  # noqa: PLC0415

    kria_editor_ops.register_handler("set_slide_cover", _op_set_slide_cover)
    kria_editor_ops.register_handler("set_post_caption", _op_set_post_caption)
    kria_editor_ops.register_handler("set_slide_framing", _op_set_slide_framing)
    kria_editor_ops.register_handler("set_post_aspect", _op_set_post_aspect)
    kria_editor_ops.register_handler("set_slide_fit", _op_set_slide_fit)
    kria_editor_ops.register_handler("set_slide_tilt", _op_set_slide_tilt)
