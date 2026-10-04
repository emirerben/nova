"""Editor ops v2, slides lane (KRI-301).

Two ops that exist ONLY on the slide-post surface (``snapshot["surface"] ==
"slide_post"``); ``edit_copilot._family_allowed`` refuses them on a video edit:

* ``set_slide_cover{slide_index}``: make slide N (0-based, the ``slots`` index shown
  in CURRENT DRAFT) the post's cover. Resolved to a stable ``slide_id`` here so the
  cover survives a reorder in the same bundle.
* ``set_post_caption{caption}``: replace the post caption ("" clears it).

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


def register_handlers() -> None:
    from app.services import kria_editor_ops  # noqa: PLC0415

    kria_editor_ops.register_handler("set_slide_cover", _op_set_slide_cover)
    kria_editor_ops.register_handler("set_post_caption", _op_set_post_caption)
