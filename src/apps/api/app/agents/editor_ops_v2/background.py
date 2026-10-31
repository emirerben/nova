"""Editor ops v2, background lane (KRI-1117): the photo or video behind a Talking speaker.

A phone Talking edit can show one Visuals-pool photo or looping muted video behind the
speaker for the whole video. This lane adds two ops:

* ``set_background {asset_id, source_start_s?}``: put a pool visual behind the speaker,
  replacing the current one. ``asset_id`` must be one of ``snapshot.background.pool`` (the
  item's ready photos and videos the server listed), the same fail-closed rule as
  ``add_overlay`` and its ``asset_pool``.
* ``remove_background``: take it away (only while one is set).

Both compile into the editor Save's ``speaker_background`` section. The Save
(`generative_jobs._validate_speaker_background_section`, after
`phone_speaker_background.pin_pool_visual_for_background`) stays the authority on what the
phone can draw; this lane only keeps the draft honest so a bad ask fails as a plain sentence
in chat. The snapshot carries the section only while the editor capability is open (see
`kria_editor_ops._background_snapshot`), so a closed gate leaves every other snapshot and
prompt byte-identical.

Do not import app.agents.edit_copilot / app.services.kria_editor_ops at module top level
(circular); import lazily inside functions.
"""

from __future__ import annotations

from typing import Any

from app.agents.editor_ops_v2 import OpSpec

# A looping background never needs a start far into a video; the Save bounds it by the
# video's own duration.
MAX_SOURCE_START_S = 1800.0


def _pool_row(snapshot: dict, asset_id: object) -> dict | None:
    section = snapshot.get("background") if isinstance(snapshot, dict) else None
    pool = section.get("pool") if isinstance(section, dict) else None
    if not isinstance(asset_id, str) or not isinstance(pool, list):
        return None
    return next(
        (row for row in pool if isinstance(row, dict) and row.get("id") == asset_id),
        None,
    )


def _coerce_set_background(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    row = _pool_row(snapshot, payload.get("asset_id"))
    if row is None or row.get("kind") not in ("image", "video"):
        state.invalid_value()
        return None
    start = payload.get("source_start_s", 0.0)
    if isinstance(start, bool) or not isinstance(start, (int, float)):
        state.invalid_value()
        return None
    start = float(start)
    if not 0.0 <= start <= MAX_SOURCE_START_S or start != start:
        state.invalid_value()
        return None
    duration = row.get("duration_s")
    if row["kind"] == "image":
        start = 0.0
    elif isinstance(duration, (int, float)) and duration > 0 and start >= duration:
        state.invalid_value()
        return None
    return {
        "op": name,
        "asset_id": row["id"],
        "kind": row["kind"],
        "source_start_s": start,
    }


def _coerce_remove_background(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    section = snapshot.get("background") if isinstance(snapshot, dict) else None
    if not isinstance(section, dict) or not section.get("current"):
        state.reject(
            op=name,
            reason="invalid_value",
            detail="There is no background behind the speaker to remove",
        )
        return None
    return {"op": name}


SPECS: list[OpSpec] = [
    OpSpec(
        name="set_background",
        required=frozenset({"asset_id"}),
        fields=frozenset({"asset_id", "source_start_s"}),
        family=frozenset({"background", "speaker_background"}),
        coerce=_coerce_set_background,
    ),
    OpSpec(
        name="remove_background",
        required=frozenset(),
        fields=frozenset(),
        family=frozenset({"background", "speaker_background"}),
        coerce=_coerce_remove_background,
    ),
]


def _op_set_background(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import (  # noqa: PLC0415
        KriaEditorOpError,
        background_current,
    )

    document = {
        "media_id": str(op["asset_id"]),
        "kind": op["kind"],
        "source_start_s": float(op.get("source_start_s") or 0.0),
    }
    if document == background_current(state.job, state.variant):
        raise KriaEditorOpError("That photo or video is already behind you")
    state.speaker_background = document
    state.changed.add("speaker_background")
    state.summary = (
        "Put the video behind you" if document["kind"] == "video" else "Put the photo behind you"
    )


def _op_remove_background(state: Any, op: dict[str, Any]) -> None:  # noqa: ARG001
    from app.services.kria_editor_ops import (  # noqa: PLC0415
        KriaEditorOpError,
        background_current,
    )

    if state.speaker_background is None and background_current(state.job, state.variant) is None:
        raise KriaEditorOpError("There is no background behind you to remove")
    state.speaker_background = None
    state.changed.add("speaker_background")
    state.summary = "Removed the background behind you"


def register_handlers() -> None:
    from app.services import kria_editor_ops  # noqa: PLC0415

    kria_editor_ops.register_handler("set_background", _op_set_background)
    kria_editor_ops.register_handler("remove_background", _op_remove_background)
