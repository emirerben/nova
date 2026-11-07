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

KRI-1173 (a plain colour behind the speaker), same family as ``set_background``:

* ``set_background_color {color}``: a flat colour instead of a photo or video. ``color`` is a
  hex in any case or one of the named presets (EN and TR, `BACKGROUND_COLOR_PRESETS`), normalised to
  ``#RRGGBB`` uppercase here; anything else is refused in words. It keeps the timed
  backgrounds and the corner the speaker sits in (windows play over the colour). While the
  snapshot says ``color_available: false`` it answers as an unmet request with the section's
  ``color_reason`` (S59), never the generic "can't change that" sentence.

Qendresa item 18 (family ``background_focus``, open only while the snapshot says
``focus_available``):

* ``set_background_focus {position?, zoom?, zoom_delta?, asset_id?}``: aim the photo / video
  fill and the timed backgrounds (or only those showing ``asset_id``) at the left, centre or
  right of the picture and/or zoom them. It is about the RECORDING; the corner the speaker sits
  in is ``set_speaker_frame``'s.

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

from app.agents._schemas.reaction_beats import normalise_background_color
from app.agents.editor_ops_v2 import OpSpec
from app.kria.reply_language import say

# A looping background never needs a start far into a video; the Save bounds it by the
# video's own duration.
MAX_SOURCE_START_S = 1800.0
# Mirrors `ProjectedBackground` cadence bounds of the creator strategy (`BackgroundMedia`).
DEFAULT_CADENCE_S = 4.0
MIN_CADENCE_S = 2.0
MAX_CADENCE_S = 10.0
# Qendresa item 18: what chat may set (the wire allows 0.2..4; the editor slider and chat 0.3..3).
MIN_CHAT_FOCUS_ZOOM = 0.3
MAX_CHAT_FOCUS_ZOOM = 3.0
_FOCUS_POSITIONS = {
    "left": 0.0,
    "center": 0.5,
    "centre": 0.5,
    "middle": 0.5,
    "right": 1.0,
}
DEFAULT_FOCUS = {"x": 0.5, "y": 0.5, "zoom": 1.0}


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


def _coerce_set_background_color(
    name: str, payload: dict, snapshot: dict, state: Any
) -> dict | None:
    section = snapshot.get("background") if isinstance(snapshot, dict) else None
    if isinstance(section, dict) and section.get("color_available") is not True:
        # S59: the lane is open but the colour gate is not -- say the snapshot's reason as an
        # unmet request, never the generic "can't change that on this edit".
        reason = str(section.get("color_reason") or "").strip() or (
            "a plain colour behind you isn't available right now"
        )
        detail = say(
            en=f"I can't put a plain colour behind you: {reason}.",
            tr="Arkana düz bir renk koyamıyorum: bu uygulama sürümünde henüz açık değil.",
        )
        state.reject(op=name, reason="capability_unavailable", detail=detail)
        state.reply_notes.append(detail)
        return None
    color = normalise_background_color(payload.get("color"))
    if color is None:
        detail = say(
            en=(
                "That isn't a colour I can use. Say white, black, green, blue, red, yellow "
                "or grey, or give a hex like #FFFFFF."
            ),
            tr=(
                "Bu, kullanabileceğim bir renk değil. Beyaz, siyah, yeşil, mavi, kırmızı, sarı "
                "ya da gri de, ya da #FFFFFF gibi bir kod ver."
            ),
        )
        state.reject(op=name, reason="invalid_value", detail=detail)
        state.invalid_value(detail)
        return None
    return {"op": name, "color": color}


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _coerce_set_background_focus(
    name: str, payload: dict, snapshot: dict, state: Any
) -> dict | None:
    section = snapshot.get("background") if isinstance(snapshot, dict) else None
    if isinstance(section, dict) and section.get("focus_available") is not True:
        detail = say(
            en="I can't aim or zoom the recordings behind you on this edit.",
            tr="Arkandaki kayıtları bu düzenlemede hizalayamıyor ya da yakınlaştıramıyorum.",
        )
        state.reject(op=name, reason="capability_unavailable", detail=detail)
        state.reply_notes.append(detail)
        return None
    cleaned: dict[str, Any] = {"op": name}
    position = payload.get("position")
    if position is not None:
        key = position.strip().lower() if isinstance(position, str) else None
        if key not in _FOCUS_POSITIONS:
            state.invalid_value()
            return None
        cleaned["position"] = "center" if key in ("centre", "middle") else key
    zoom = payload.get("zoom")
    if zoom is not None:
        number = _finite_number(zoom)
        if number is None:
            state.invalid_value()
            return None
        clamped = min(max(number, MIN_CHAT_FOCUS_ZOOM), MAX_CHAT_FOCUS_ZOOM)
        if clamped != number:
            state.reply_notes.append(
                say(
                    en=f"I kept the zoom between {MIN_CHAT_FOCUS_ZOOM:g}x and "
                    f"{MAX_CHAT_FOCUS_ZOOM:g}x.",
                    tr=f"Yakınlaştırmayı {MIN_CHAT_FOCUS_ZOOM:g}x ile "
                    f"{MAX_CHAT_FOCUS_ZOOM:g}x arasında tuttum.",
                )
            )
        cleaned["zoom"] = clamped
    delta = payload.get("zoom_delta")
    if delta is not None:
        number = _finite_number(delta)
        if number is None or not -2.0 <= number <= 2.0:
            state.invalid_value()
            return None
        cleaned["zoom_delta"] = number
    if not ({"position", "zoom", "zoom_delta"} & cleaned.keys()):
        state.invalid_value()
        return None
    asset_id = payload.get("asset_id")
    if asset_id is not None:
        if _pool_row(snapshot, asset_id) is None:
            state.invalid_value()
            return None
        cleaned["asset_id"] = asset_id
    return cleaned


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


def _frame_selector(name, payload, snapshot, state):
    from app.agents import edit_copilot as ec
    from app.agents.editor_ops_v2.timeline import _clean_selector

    if not snapshot.get("split_screen_available"):
        state.invalid_value()
        return None
    slots = [s for s in ec._snapshot_list(snapshot, ec._SLOT_INDEX_KEYS) if isinstance(s, dict)]
    return _clean_selector(name, payload.get("selector"), slots, state)


def _coerce_set_speaker_frame(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS, SpeakerFrame

    targeted = "selector" in payload
    preset = payload.get("preset")
    frame = payload.get("frame")
    if (preset is None) == (frame is None):
        state.invalid_value()
        return None
    out = {"op": name}
    if targeted:
        selector = _frame_selector(name, payload, snapshot, state)
        if selector is None:
            return None
        out["selector"] = selector
    if frame is not None:
        if not targeted:
            state.invalid_value()
            return None
        try:
            out["frame"] = SpeakerFrame.model_validate(frame).model_dump()
        except ValueError:
            state.invalid_value()
            return None
    elif not isinstance(preset, str) or (
        preset not in SPEAKER_FRAME_PRESETS and not (targeted and preset == "inherit")
    ):
        state.invalid_value()
        return None
    else:
        out["preset"] = preset
    return out


def _coerce_clear_speaker_frame(
    name: str, payload: dict, snapshot: dict, state: Any
) -> dict | None:
    out = {"op": name}
    if "selector" in payload:
        selector = _frame_selector(name, payload, snapshot, state)
        if selector is None:
            return None
        out["selector"] = selector
    return out


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
    # KRI-1173: the same family as set_background; availability is the snapshot's
    # `color_available`, answered in the coerce step so a closed gate is an unmet request.
    OpSpec(
        name="set_background_color",
        required=frozenset({"color"}),
        fields=frozenset({"color"}),
        family=frozenset({"background", "speaker_background"}),
        coerce=_coerce_set_background_color,
    ),
    OpSpec(
        name="remove_background",
        required=frozenset(),
        fields=frozenset(),
        family=frozenset({"background", "speaker_background"}),
        coerce=_coerce_remove_background,
    ),
    # Qendresa item 18: own family, in `allowed_op_families` only while the focus gate is open.
    OpSpec(
        name="set_background_focus",
        required=frozenset(),
        fields=frozenset({"position", "zoom", "zoom_delta", "asset_id"}),
        family=frozenset({"background_focus"}),
        coerce=_coerce_set_background_focus,
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
        required=frozenset(),
        fields=frozenset({"preset", "frame", "selector"}),
        family=frozenset({"background_windows"}),
        coerce=_coerce_set_speaker_frame,
    ),
    OpSpec(
        name="clear_speaker_frame",
        required=frozenset(),
        fields=frozenset({"selector"}),
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


def _keep_full_screen(document: dict, working: object) -> None:
    """Carry the creator's full-screen opening (Qendresa item 16) onto a rebuilt document: every
    background op changes the fill, the timed backgrounds or the corner, never the opening."""
    if isinstance(working, dict) and working.get("full_screen_windows"):
        document["full_screen_windows"] = [dict(span) for span in working["full_screen_windows"]]


def _focus_of(entry: object) -> dict[str, float]:
    focus = entry.get("focus") if isinstance(entry, dict) else None
    return {**DEFAULT_FOCUS, **focus} if isinstance(focus, dict) else dict(DEFAULT_FOCUS)


def _focus_by_media(working: object) -> dict[str, dict]:
    """The focus each picture has in ``working`` (the first window or the fill showing it)."""
    found: dict[str, dict] = {}
    if not isinstance(working, dict):
        return found
    for window in working.get("windows") or ():
        if isinstance(window, dict) and window.get("focus") and window.get("media_id"):
            found.setdefault(window["media_id"], dict(window["focus"]))
    if working.get("focus") and working.get("media_id"):
        found.setdefault(working["media_id"], dict(working["focus"]))
    return found


def _op_set_background_focus(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    working = _working_document(state)
    if not isinstance(working, dict):
        raise KriaEditorOpError("Put a photo or video behind you first")
    asset_id = op.get("asset_id")
    document: dict[str, Any] = dict(working)
    if working.get("windows"):
        document["windows"] = [dict(window) for window in working["windows"]]
    targets: list[dict[str, Any]] = []
    if document.get("media_id") and asset_id in (None, document["media_id"]):
        targets.append(document)
    targets.extend(
        window
        for window in document.get("windows") or ()
        if isinstance(window, dict) and asset_id in (None, window.get("media_id"))
    )
    if not targets:
        raise KriaEditorOpError("Put a photo or video behind you first")
    changed = False
    for entry in targets:
        current = _focus_of(entry)
        new = dict(current)
        if "position" in op:
            new["x"] = _FOCUS_POSITIONS[op["position"]]
        if "zoom" in op:
            new["zoom"] = float(op["zoom"])
        if "zoom_delta" in op:
            new["zoom"] = min(
                max(current["zoom"] + float(op["zoom_delta"]), MIN_CHAT_FOCUS_ZOOM),
                MAX_CHAT_FOCUS_ZOOM,
            )
        new["zoom"] = round(new["zoom"], 4)
        if new == current:
            continue
        if new == DEFAULT_FOCUS:
            entry.pop("focus", None)  # today's drawing is never written
        else:
            entry["focus"] = new
        changed = True
    if not changed:
        raise KriaEditorOpError("That's already how they're aimed")
    state.speaker_background = document
    state.changed.add("speaker_background")
    delta = op.get("zoom_delta")
    if "position" in op:
        state.summary = "Aimed the recordings at their " + (
            "centre" if op["position"] == "center" else f"{op['position']} side"
        )
    elif isinstance(delta, (int, float)):
        state.summary = "Zoomed the recordings " + ("out" if delta < 0 else "in")
    else:
        state.summary = "Changed how far the recordings are zoomed"


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
    # Qendresa item 18: the same picture stays aimed; another one starts from its own default.
    if (
        isinstance(working, dict)
        and working.get("focus")
        and working.get("media_id") == document["media_id"]
    ):
        document["focus"] = dict(working["focus"])
    # KRI-1162: one fill replaces the timed backgrounds; the corner the speaker sits in stays.
    if isinstance(working, dict) and working.get("speaker_frame"):
        document["speaker_frame"] = dict(working["speaker_frame"])
    _keep_full_screen(document, working)
    if document == background_current(state.job, state.variant):
        raise KriaEditorOpError("That photo or video is already behind you")
    state.speaker_background = document
    state.changed.add("speaker_background")
    state.summary = (
        "Put the video behind you" if document["kind"] == "video" else "Put the photo behind you"
    )


def _op_set_background_color(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import (  # noqa: PLC0415
        KriaEditorOpError,
        background_current,
    )

    document: dict[str, Any] = {"kind": "color", "color": str(op["color"])}
    working = _working_document(state)
    # A colour only changes the fill: the timed backgrounds play over it and the corner the
    # speaker sits in stays (KRI-1173).
    if isinstance(working, dict):
        if working.get("windows"):
            # Windows are copied whole, so each keeps its focus (Qendresa item 18).
            document["windows"] = [dict(window) for window in working["windows"]]
        if working.get("speaker_frame"):
            document["speaker_frame"] = dict(working["speaker_frame"])
    _keep_full_screen(document, working)
    if document == background_current(state.job, state.variant):
        raise KriaEditorOpError("That colour is already behind you")
    state.speaker_background = document
    state.changed.add("speaker_background")
    state.summary = f"Put a plain {op['color']} background behind you"


def _photo_key(media_id: object) -> str:
    """A pool photo's identity whether it is written ``asset-<id>`` or bare (S59)."""
    return str(media_id).removeprefix("asset-")


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
    working = _working_document(state)
    if isinstance(working, dict) and working.get("color"):
        fill: dict[str, Any] = {"kind": "color", "color": working["color"]}  # KRI-1173
    elif isinstance(working, dict) and working.get("media_id"):
        fill = {
            "media_id": working["media_id"],
            "kind": working["kind"],
            "source_start_s": working.get("source_start_s", 0.0),
        }
        if working.get("focus"):
            fill["focus"] = dict(working["focus"])
    else:
        fill = {"media_id": ids[0], "kind": kinds[0], "source_start_s": 0.0}
    # S57: the same rule as the render worker -- every photo once, the fill never repeating one.
    layout = sequence.lay_out(
        [sequence.SequenceEntry(index=i, media_key=_photo_key(ids[i])) for i in range(len(ids))],
        cadence_s=float(op.get("cadence_s") or DEFAULT_CADENCE_S),
        take_s=duration,
        words=None,
        keep_segments=None,
        fill_key=_photo_key(fill["media_id"]) if fill.get("media_id") else None,
    )
    if not layout.windows:
        raise KriaEditorOpError("This video is too short to change backgrounds")
    known_focus = _focus_by_media(working)
    document: dict[str, Any] = {
        **fill,
        "windows": [
            {
                "media_id": ids[window.index],
                "kind": kinds[window.index],
                "source_start_s": 0.0,
                "start_s": round(window.start_s, 3),
                "end_s": round(window.end_s, 3),
                # Qendresa item 18: a picture the creator already aimed stays aimed.
                **(
                    {"focus": dict(known_focus[ids[window.index]])}
                    if ids[window.index] in known_focus
                    else {}
                ),
            }
            for window in layout.windows
        ],
    }
    if isinstance(working, dict) and working.get("speaker_frame"):
        document["speaker_frame"] = dict(working["speaker_frame"])
    _keep_full_screen(document, working)
    if document == background_current(state.job, state.variant):
        raise KriaEditorOpError("Those backgrounds are already behind you")
    state.speaker_background = document
    state.changed.add("speaker_background")
    state.summary = (
        f"Put {len(set(ids))} backgrounds behind you, changing about every "
        f"{round(layout.cadence_s, 1):g} seconds"
        if len(set(ids)) > 1
        else "Put the photo behind you"
    )


def _set_occurrence_frame(state, op, override):
    from copy import deepcopy

    from app.agents.editor_ops_v2.timeline import _select
    from app.kria.portable_speaker_background import SpeakerFrameOverride
    from app.services.kria_editor_ops import (
        KriaEditorOpError,
        copy_source_occurrence,
        split_screen_available,
    )

    if not split_screen_available(state.job, state.variant):
        raise KriaEditorOpError("Independent speaker frames are unavailable on this client")
    value = SpeakerFrameOverride.model_validate(override).model_dump(exclude_none=True)
    rows = [state.slots[index] for index in _select(state, op["selector"])]
    if value["mode"] != "inherit":
        working = _working_document(state)
        if not isinstance(working, dict) or not (working.get("media_id") or working.get("color")):
            raise KriaEditorOpError("Set a speaker background before framing an occurrence")
        if any(
            copy_source_occurrence(row).get("speaker_subject") is not True
            or row.get("media_kind", "video") != "video"
            for row in rows
        ):
            raise KriaEditorOpError(
                "Select video speaker occurrences, or reset their frame to inherit first"
            )
    for row in rows:
        if value["mode"] != "inherit":
            row["speaker_subject"] = True
        row["speaker_frame_override"] = deepcopy(value)
    state.changed.add("timeline")
    state.summary = "Updated the selected speaker frames"


def _op_set_speaker_frame(state: Any, op: dict[str, Any]) -> None:
    from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS  # noqa: PLC0415
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    if "selector" in op:
        if op.get("preset") == "inherit":
            override = {"mode": "inherit"}
        else:
            override = {
                "mode": "custom",
                "frame": op.get("frame") or SPEAKER_FRAME_PRESETS[op["preset"]],
            }
        _set_occurrence_frame(state, op, override)
        return
    working = _working_document(state)
    if not isinstance(working, dict) or not (working.get("media_id") or working.get("color")):
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

    if "selector" in op:
        _set_occurrence_frame(state, op, {"mode": "full"})
        return
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
    for row in state.slots:
        if row.get("speaker_frame_override") is not None:
            row["speaker_frame_override"] = {"mode": "inherit"}
            state.changed.add("timeline")
    state.speaker_background = None
    state.changed.add("speaker_background")
    state.summary = "Removed the background behind you"


def register_handlers() -> None:
    from app.services import kria_editor_ops  # noqa: PLC0415

    kria_editor_ops.register_handler("set_background", _op_set_background)
    kria_editor_ops.register_handler("set_background_color", _op_set_background_color)
    kria_editor_ops.register_handler("remove_background", _op_remove_background)
    kria_editor_ops.register_handler("set_background_sequence", _op_set_background_sequence)
    kria_editor_ops.register_handler("set_background_focus", _op_set_background_focus)
    kria_editor_ops.register_handler("set_speaker_frame", _op_set_speaker_frame)
    kria_editor_ops.register_handler("clear_speaker_frame", _op_clear_speaker_frame)
