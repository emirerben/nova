"""The creator's own song from chat (KRI-1394): the editor Sounds tab's Save section.

``set_user_song {volume?, window_start_s?, window_end_s?, remove?}`` compiles into the editor
Save's ``user_song`` section (`EditorCommitUserSong`), the one the phone sends from
``NativeEditorSession.setUserSongVolume`` / ``setUserSongStart`` / ``setUserSongEnd`` /
``removeUserSong`` (operation keys ``user_song.volume`` / ``user_song.window`` /
``user_song.remove``). The Save folds it into the plan the device recipe pins
(`generative_jobs._merge_user_song_edit`), so this lane only keeps the draft honest: the same
locks and limits, said in plain words before the turn ends.

* A lip-sync song is the master clock: its start and end are where the takes were filmed
  (``user_song_lipsync_locked``). Only its volume or its removal are offered.
* A background song's window keeps at least ``MIN_PLAYABLE_SONG_S`` of song
  (``user_song_window_out_of_range``); an end at the song's own end means "no end set".
* A removal wins outright, like the Save.

Offered only while the snapshot carries the ``user_song`` family (a creator song on this edit and
the editor's ``user_song`` capability), so every other snapshot and prompt is byte-identical.

Do not import app.agents.edit_copilot / app.services.kria_editor_ops at module top level
(circular); import lazily inside functions.
"""

from __future__ import annotations

import math
from typing import Any

from app.agents.editor_ops_v2 import OpSpec
from app.kria.reply_language import say
from app.schemas.user_song import MIN_PLAYABLE_SONG_S

FAMILY = "user_song"
# Where a chat turn keeps its unsaved `user_song` section on the projected variant
# (`project_editor_draft`), so the next turn's snapshot and compile see it.
DRAFT_KEY = "user_song_edit"
CLOSED_NOTE = (
    "Your own song can't be changed from chat on this edit.",
    "Bu düzenlemede kendi şarkını sohbetten değiştiremiyorum.",
)
_EPSILON = 1e-3


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _editable(caps: object, key: str) -> bool:
    value = caps.get(key) if isinstance(caps, dict) else None
    return value is True or (isinstance(value, dict) and value.get("editable") is True)


def song_state(job: Any, variant: dict[str, Any]) -> dict[str, Any] | None:
    """The creator song as this turn has it: the plan's song, the saved guided edit, then the
    unsaved draft section, each laid over the last the way the Save merges them.

    ``window_end_s`` is where the song stops now; a background song's plan end follows the video
    length from its start (`guided_story`'s re-window), so only an end the creator set
    (``creator_end_s``) limits a later start.
    """
    from app.routes.generative_jobs import _guided_v2_revision  # noqa: PLC0415
    from app.services.user_song_projection import user_song_for_variant  # noqa: PLC0415

    song = user_song_for_variant(job, variant, song_filename=None)
    if song is None:
        return None
    state: dict[str, Any] = {
        "mode": song["mode"],
        "duration_s": round(float(song["duration_s"]), 3),
        "window_start_s": round(float(song["window_start_s"]), 3),
        "window_end_s": round(float(song["window_end_s"]), 3),
        "volume": round(float(song.get("volume", 1.0)), 3),
    }
    guided = _guided_v2_revision(job, variant) if job is not None else None
    for edit in (
        (guided or {}).get("user_song") if isinstance(guided, dict) else None,
        variant.get(DRAFT_KEY),
    ):
        if not isinstance(edit, dict):
            continue
        if edit.get("removed"):
            return {**state, "removed": True}
        for key in ("volume", "window_start_s", "window_end_s"):
            value = _num(edit.get(key))
            if value is not None:
                state[key] = round(min(value, state["duration_s"]), 3)
        end = _num(edit.get("window_end_s"))
        if end is not None and end < state["duration_s"] - _EPSILON:
            state["creator_end_s"] = round(end, 3)
    return state


def user_song_snapshot(job: Any, variant: dict[str, Any], caps: dict[str, Any]) -> dict | None:
    """The ``user_song`` section of the copilot snapshot: the song now and what may change."""
    state = song_state(job, variant)
    section = caps.get("user_song") if isinstance(caps, dict) else None
    if state is None or not isinstance(section, dict):
        return None
    out = dict(state)
    out["min_window_s"] = MIN_PLAYABLE_SONG_S
    out["volume_editable"] = _editable(section, "volume")
    out["window_editable"] = state["mode"] == "background" and _editable(section, "window")
    out["removable"] = _editable(section, "remove")
    return out


def _refuse(state: Any, name: str, en: str, tr: str) -> None:
    detail = say(en=en, tr=tr)
    state.reject(op=name, reason="capability_unavailable", detail=detail)
    if detail not in state.reply_notes:
        state.reply_notes.append(detail)


def _coerce(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    song = snapshot.get("user_song") if isinstance(snapshot, dict) else None
    if not isinstance(song, dict) or FAMILY not in (snapshot.get("allowed_op_families") or []):
        _refuse(state, name, CLOSED_NOTE[0], CLOSED_NOTE[1])
        return None
    if song.get("removed"):
        _refuse(
            state,
            name,
            "Your song is already taken off this edit.",
            "Şarkın bu düzenlemeden zaten kaldırıldı.",
        )
        return None
    if payload.get("remove") is True:
        if not song.get("removable"):
            _refuse(state, name, CLOSED_NOTE[0], CLOSED_NOTE[1])
            return None
        return {"remove": True}
    out: dict[str, Any] = {}
    if "volume" in payload:
        volume = _num(payload["volume"])
        if volume is None:
            state.invalid_value("Song volume is a number from 0 (silent) to 1 (full).")
            return None
        if not song.get("volume_editable"):
            _refuse(state, name, CLOSED_NOTE[0], CLOSED_NOTE[1])
            return None
        out["volume"] = round(min(max(volume, 0.0), 1.0), 2)
    window = {key: payload[key] for key in ("window_start_s", "window_end_s") if key in payload}
    if window:
        if song.get("mode") == "lipsync":
            # The Save's own refusal (`user_song_lipsync_locked`), in the editor's words.
            _refuse(
                state,
                name,
                "Lip-sync keeps the song where you filmed it.",
                "Dudak senkronu şarkıyı çektiğin yerde tutar.",
            )
            return None
        if not song.get("window_editable"):
            _refuse(state, name, CLOSED_NOTE[0], CLOSED_NOTE[1])
            return None
        values = {key: _num(value) for key, value in window.items()}
        if any(value is None or value < 0 for value in values.values()):
            state.invalid_value("Song start and end are seconds into the song file.")
            return None
        error = window_error(song, values.get("window_start_s"), values.get("window_end_s"))
        if error:
            state.invalid_value(error)
            return None
        out.update({key: round(value, 3) for key, value in values.items() if value is not None})
    if not out:
        state.invalid_value("Say what to change: volume, window_start_s, window_end_s or remove.")
        return None
    return out


def window_error(song: dict[str, Any], start: float | None, end: float | None) -> str | None:
    """Why a background song window cannot be saved, else None (the Save's
    ``user_song_window_out_of_range``): at least ``MIN_PLAYABLE_SONG_S`` of song must play
    after the start, and before an end the creator set."""
    duration = float(song["duration_s"])
    new_start = float(song["window_start_s"]) if start is None else start
    if new_start > duration - MIN_PLAYABLE_SONG_S + _EPSILON:
        return (
            f"The song is {duration:g}s long; it must start at least "
            f"{MIN_PLAYABLE_SONG_S:g}s before its end."
        )
    new_end = end if end is not None else song.get("creator_end_s")
    if new_end is None:
        return None
    new_end = min(float(new_end), duration)
    if new_end < new_start + MIN_PLAYABLE_SONG_S - _EPSILON:
        return (
            f"That leaves less than {MIN_PLAYABLE_SONG_S:g}s of the song between its start "
            f"({new_start:g}s) and its end ({new_end:g}s)."
        )
    return None


def _op_set_user_song(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    current = song_state(state.job, state.variant)
    if current is None or current.get("removed"):
        raise KriaEditorOpError("This edit has no song of your own to change")
    pending = dict(state.user_song or {})
    if pending.get("removed"):
        raise KriaEditorOpError("Your song is already taken off this edit")
    if op.get("remove"):
        state.user_song = {"removed": True}
        state.changed.add(FAMILY)
        state.summary = "Removed your song"
        return
    effective = {**current, **pending}
    if "window_end_s" in pending:
        effective.pop("creator_end_s", None)
        if pending["window_end_s"] < float(current["duration_s"]) - _EPSILON:
            effective["creator_end_s"] = pending["window_end_s"]
    edit: dict[str, Any] = {}
    if op.get("volume") is not None:
        volume = round(min(max(float(op["volume"]), 0.0), 1.0), 2)
        if abs(volume - float(effective["volume"])) > 0.0005:
            edit["volume"] = volume
    start, end = op.get("window_start_s"), op.get("window_end_s")
    if start is not None or end is not None:
        if current["mode"] == "lipsync":
            raise KriaEditorOpError("Lip-sync keeps the song where you filmed it")
        error = window_error(
            effective,
            None if start is None else float(start),
            None if end is None else float(end),
        )
        if error:
            raise KriaEditorOpError(error)
        if start is not None and abs(float(start) - float(effective["window_start_s"])) > _EPSILON:
            edit["window_start_s"] = round(float(start), 3)
        if end is not None:
            # The song's own end is "no end set" on the wire (the Save pops it).
            value = min(float(end), float(current["duration_s"]))
            if abs(value - float(effective["window_end_s"])) > _EPSILON:
                edit["window_end_s"] = round(value, 3)
    if not edit:
        raise KriaEditorOpError("Your song is already set that way")
    state.user_song = {**pending, **edit}
    state.changed.add(FAMILY)
    state.summary = (
        "Changed the volume of your song"
        if set(edit) == {"volume"}
        else "Changed where your song plays"
    )


SPECS = [
    OpSpec(
        name="set_user_song",
        required=frozenset(),
        fields=frozenset({"volume", "window_start_s", "window_end_s", "remove"}),
        family=frozenset({FAMILY}),
        coerce=_coerce,
        closed_note=CLOSED_NOTE,
    )
]


def register_handlers() -> None:
    from app.services import kria_editor_ops  # noqa: PLC0415

    kria_editor_ops.register_handler("set_user_song", _op_set_user_song)
