"""Editor ops v2, background lane (KRI-1117): the photo or video behind a Talking speaker.

A phone Talking edit can show one Visuals-pool photo or looping muted video behind the
speaker for the whole video. This lane adds two ops:

* ``set_background {asset_id, source_start_s?}``: put a pool visual behind the speaker,
  replacing the current one. ``asset_id`` must be one of ``snapshot.background.pool`` (the
  item's ready photos and videos the server listed), the same fail-closed rule as
  ``add_overlay`` and its ``asset_pool``.
* ``remove_background``: take it away (only while one is set).

KRI-1162 (commentary-style green screen), offered only while the snapshot's ``background``
section says ``windows_available`` (family ``background_windows``, so a closed windows gate
leaves every snapshot and prompt byte-identical):

* ``set_background_sequence {asset_ids, cadence_s?}``: several pool visuals that take turns
  behind the speaker, laid out from 0 with the cadence until the edit ends (the section's
  ``duration_s``); replaces the windows, keeps the fill (the first id when none is set).
* ``set_speaker_frame {preset}`` / ``clear_speaker_frame``: the matted speaker small in a
  named corner / full-screen again. Presets only, never numbers (S37).

``set_background`` still means one fill and now clears the windows; ``remove_background``
clears everything.

All of them compile into the editor Save's ``speaker_background`` section. The Save
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
# Mirrors `ProjectedBackground` cadence bounds of the creator strategy (`BackgroundMedia`).
DEFAULT_CADENCE_S = 4.0
MIN_CADENCE_S = 2.0
MAX_CADENCE_S = 10.0


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


def _coerce_set_background_sequence(
    name: str, payload: dict, snapshot: dict, state: Any
) -> dict | None:
    from app.services.phone_background_sequence import MAX_WINDOWS  # noqa: PLC0415

    ids = payload.get("asset_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_WINDOWS:
        state.invalid_value()
        return None
    rows = [_pool_row(snapshot, asset_id) for asset_id in ids]
    if any(row is None or row.get("kind") not in ("image", "video") for row in rows):
        state.invalid_value()
        return None
    cadence = payload.get("cadence_s", DEFAULT_CADENCE_S)
    if (
        isinstance(cadence, bool)
        or not isinstance(cadence, (int, float))
        or not MIN_CADENCE_S <= float(cadence) <= MAX_CADENCE_S
    ):
        state.invalid_value()
        return None
    return {
        "op": name,
        "asset_ids": [row["id"] for row in rows if row is not None],
        "kinds": [row["kind"] for row in rows if row is not None],
        "cadence_s": float(cadence),
    }


def _coerce_set_speaker_frame(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS  # noqa: PLC0415

    preset = payload.get("preset")
    if not isinstance(preset, str) or preset not in SPEAKER_FRAME_PRESETS:
        state.invalid_value()
        return None
    return {"op": name, "preset": preset}


def _coerce_clear_speaker_frame(
    name: str,
    payload: dict,
    snapshot: dict,
    state: Any,  # noqa: ARG001
) -> dict | None:
    return {"op": name}


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
    # KRI-1162: their own family, only in `allowed_op_families` while the windows gate is open.
    OpSpec(
        name="set_background_sequence",
        required=frozenset({"asset_ids"}),
        fields=frozenset({"asset_ids", "cadence_s"}),
        family=frozenset({"background_windows"}),
        coerce=_coerce_set_background_sequence,
    ),
    OpSpec(
        name="set_speaker_frame",
        required=frozenset({"preset"}),
        fields=frozenset({"preset"}),
        family=frozenset({"background_windows"}),
        coerce=_coerce_set_speaker_frame,
    ),
    OpSpec(
        name="clear_speaker_frame",
        required=frozenset(),
        fields=frozenset(),
        family=frozenset({"background_windows"}),
        coerce=_coerce_clear_speaker_frame,
    ),
]


def _working_document(state: Any) -> dict | None:
    """The background document as this turn has it so far: an earlier op's, else the saved one."""
    from app.services.kria_editor_ops import background_current  # noqa: PLC0415

    if "speaker_background" in state.changed:
        return state.speaker_background
    return background_current(state.job, state.variant)


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
    working = _working_document(state)
    # KRI-1162: one fill replaces the timed backgrounds; the corner the speaker sits in stays.
    if isinstance(working, dict) and working.get("speaker_frame"):
        document["speaker_frame"] = dict(working["speaker_frame"])
    if document == background_current(state.job, state.variant):
        raise KriaEditorOpError("That photo or video is already behind you")
    state.speaker_background = document
    state.changed.add("speaker_background")
    state.summary = (
        "Put the video behind you" if document["kind"] == "video" else "Put the photo behind you"
    )


def _op_set_background_sequence(state: Any, op: dict[str, Any]) -> None:
    from app.services import phone_background_sequence as sequence  # noqa: PLC0415
    from app.services.kria_editor_ops import (  # noqa: PLC0415
        KriaEditorOpError,
        background_current,
        background_duration_s,
    )

    ids: list[str] = list(op["asset_ids"])
    kinds: list[str] = list(op["kinds"])
    duration = background_duration_s(state.variant)
    if duration < sequence.MIN_WINDOW_S:
        raise KriaEditorOpError("I can't tell how long this video is, so I can't time backgrounds")
    layout = sequence.lay_out(
        [sequence.SequenceEntry(index=i) for i in range(len(ids))],
        cadence_s=float(op.get("cadence_s") or DEFAULT_CADENCE_S),
        take_s=duration,
        words=None,
        keep_segments=None,
    )
    if not layout.windows:
        raise KriaEditorOpError("This video is too short to change backgrounds")
    working = _working_document(state)
    fill = (
        {
            "media_id": working["media_id"],
            "kind": working["kind"],
            "source_start_s": working.get("source_start_s", 0.0),
        }
        if isinstance(working, dict) and working.get("media_id")
        else {"media_id": ids[0], "kind": kinds[0], "source_start_s": 0.0}
    )
    document: dict[str, Any] = {
        **fill,
        "windows": [
            {
                "media_id": ids[window.index],
                "kind": kinds[window.index],
                "source_start_s": 0.0,
                "start_s": round(window.start_s, 3),
                "end_s": round(window.end_s, 3),
            }
            for window in layout.windows
        ],
    }
    if isinstance(working, dict) and working.get("speaker_frame"):
        document["speaker_frame"] = dict(working["speaker_frame"])
    if document == background_current(state.job, state.variant):
        raise KriaEditorOpError("Those backgrounds are already behind you")
    state.speaker_background = document
    state.changed.add("speaker_background")
    state.summary = (
        f"Put {len(set(ids))} backgrounds behind you, changing about every "
        f"{layout.cadence_s:g} seconds"
        if len(set(ids)) > 1
        else "Put the photo behind you"
    )


def _op_set_speaker_frame(state: Any, op: dict[str, Any]) -> None:
    from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS  # noqa: PLC0415
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    working = _working_document(state)
    if not isinstance(working, dict) or not working.get("media_id"):
        raise KriaEditorOpError(
            "Put a photo or video behind you first, then I can place you small in a corner"
        )
    frame = dict(SPEAKER_FRAME_PRESETS[op["preset"]])
    if working.get("speaker_frame") == frame:
        raise KriaEditorOpError("You're already there")
    state.speaker_background = {**working, "speaker_frame": frame}
    state.changed.add("speaker_background")
    state.summary = "Made you small in the corner"


def _op_clear_speaker_frame(state: Any, op: dict[str, Any]) -> None:  # noqa: ARG001
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    working = _working_document(state)
    if not isinstance(working, dict) or not working.get("speaker_frame"):
        raise KriaEditorOpError("You're already full screen")
    document = {key: value for key, value in working.items() if key != "speaker_frame"}
    state.speaker_background = document
    state.changed.add("speaker_background")
    state.summary = "Put you back full screen"


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
    kria_editor_ops.register_handler("set_background_sequence", _op_set_background_sequence)
    kria_editor_ops.register_handler("set_speaker_frame", _op_set_speaker_frame)
    kria_editor_ops.register_handler("clear_speaker_frame", _op_clear_speaker_frame)
