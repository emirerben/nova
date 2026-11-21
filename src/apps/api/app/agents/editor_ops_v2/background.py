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

KRI-1558 (family ``speaker_sections``, open only while the snapshot says ``sections_available``:
the windows gate AND the build that draws the speaker's layout per section, protocol 54). The
snapshot lists the take's SECTIONS (`talking_sections` over the caption words, output seconds) and
the ``layout_windows`` in force. A layout is ``own`` (her own take, full screen), ``full`` (her
cut-out full size over the picture), ``small`` (her cut-out small in a corner over the picture) or
``picture`` (the picture alone, her voice on):

* ``arrange_sections {asset_ids?}``: the planner's layout per section. The pictures (the named
  ones, else the ones already behind her, in order) go on the middle sections, which alternate
  ``small`` and ``picture``; the first and last sections are ``own``. Replaces the timed
  backgrounds and the one corner; "add pictures" no longer means one corner for the whole take.
* ``set_section_layout {speaker_layout, section_numbers? | start_s?/end_s?, preset?}``: one
  layout for some sections, for a stretch "from A to B", or (neither) the whole video; the rest
  keep theirs. No new picture is needed.
* ``remove_background_window {window_index}`` / ``retime_background_window {window_index,
  start_s, end_s}``: one timed background (1-based, as listed) taken away or moved.

``clear_speaker_frame`` ("put me back full screen") clears every place that makes her small: the
one corner, the ``small``/``picture`` sections and any clip's own corner, so it always applies.
``set_speaker_frame`` without a selector ("keep me in the corner the whole time") is one corner
for the whole video again: it clears the layouts per section.

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
            en="I can't put a plain colour behind you: {reason}.",
            tr="Arkana düz bir renk koyamıyorum: bu uygulama sürümünde henüz açık değil.",
            reason=reason,
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
                    en="I kept the zoom between {min_chat_focus_zoom:g}x and "
                    "{max_chat_focus_zoom:g}x.",
                    tr="Yakınlaştırmayı {min_chat_focus_zoom:g}x ile "
                    "{max_chat_focus_zoom:g}x arasında tuttum.",
                    min_chat_focus_zoom=MIN_CHAT_FOCUS_ZOOM,
                    max_chat_focus_zoom=MAX_CHAT_FOCUS_ZOOM,
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


# Qendresa item 16 / KRI-1394: the speaker's full-screen opening is the full-screen window that
# starts at the top of the edit (`NativeEditorSession.speakerFullScreenOpening`, within 0.05 s).
OPENING_START_S = 0.05
NO_OPENING_NOTE = (
    "There is no full-screen opening of you to take away.",
    "Kaldırılacak tam ekran bir açılışın yok.",
)
OPENING_LOCKED_NOTE = (
    "Taking away the full-screen opening isn't available on this edit yet.",
    "Tam ekran açılışı kaldırmak bu düzenlemede henüz yok.",
)


def is_opening(span: object) -> bool:
    start = span.get("start_s") if isinstance(span, dict) else None
    return (
        isinstance(start, (int, float)) and not isinstance(start, bool) and start <= OPENING_START_S
    )


def _coerce_remove_full_screen_opening(
    name: str, payload: dict, snapshot: dict, state: Any
) -> dict | None:
    section = snapshot.get("background") if isinstance(snapshot, dict) else None
    section = section if isinstance(section, dict) else {}
    if section.get("opening_removable") is True:
        return {"op": name}
    opening = any(is_opening(span) for span in section.get("full_screen_windows") or ())
    en, tr = OPENING_LOCKED_NOTE if opening else NO_OPENING_NOTE
    detail = say(en=en, tr=tr)
    state.reject(op=name, reason="capability_unavailable", detail=detail)
    if detail not in state.reply_notes:
        state.reply_notes.append(detail)
    return None


# KRI-1558: the speaker's layout per section.
SPEAKER_LAYOUTS = ("own", "full", "small", "picture")
MAX_LAYOUT_WINDOWS = 40
MIN_LAYOUT_S = 0.5
DEFAULT_SMALL_PRESET = "small_bottom_right"


def _sections_section(snapshot: dict) -> dict | None:
    section = snapshot.get("background") if isinstance(snapshot, dict) else None
    if isinstance(section, dict) and section.get("sections_available") is True:
        return section
    return None


def _sections_closed(name: str, state: Any) -> None:
    detail = say(
        en="I can't change where you are part by part on this edit yet.",
        tr="Bu düzenlemede yerini bölüm bölüm henüz değiştiremiyorum.",
    )
    state.reject(op=name, reason="capability_unavailable", detail=detail)
    if detail not in state.reply_notes:
        state.reply_notes.append(detail)


def _coerce_arrange_sections(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    if _sections_section(snapshot) is None:
        _sections_closed(name, state)
        return None
    out: dict[str, Any] = {"op": name}
    ids = payload.get("asset_ids")
    if ids is not None:
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_LAYOUT_WINDOWS:
            state.invalid_value()
            return None
        rows = [_pool_row(snapshot, asset_id) for asset_id in ids]
        if any(row is None or row.get("kind") not in ("image", "video") for row in rows):
            state.invalid_value()
            return None
        out["asset_ids"] = [row["id"] for row in rows if row is not None]
        out["kinds"] = [row["kind"] for row in rows if row is not None]
    return out


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _coerce_set_section_layout(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS  # noqa: PLC0415

    section = _sections_section(snapshot)
    if section is None:
        _sections_closed(name, state)
        return None
    layout = payload.get("speaker_layout")
    if layout not in SPEAKER_LAYOUTS:
        state.invalid_value()
        return None
    out: dict[str, Any] = {"op": name, "speaker_layout": layout}
    preset = payload.get("preset")
    if preset is not None:
        if layout != "small" or preset not in SPEAKER_FRAME_PRESETS:
            state.invalid_value()
            return None
        out["preset"] = preset
    numbers = payload.get("section_numbers")
    has_span = "start_s" in payload or "end_s" in payload
    if numbers is not None and has_span:
        state.invalid_value()
        return None
    if numbers is not None:
        listed = section.get("sections") or []
        if (
            not isinstance(numbers, list)
            or not numbers
            or any(isinstance(n, bool) or not isinstance(n, int) for n in numbers)
            or any(not 1 <= n <= len(listed) for n in numbers)
        ):
            state.invalid_value()
            return None
        out["section_numbers"] = sorted(set(numbers))
    elif has_span:
        duration = _number(section.get("duration_s")) or 0.0
        start = _number(payload.get("start_s", 0.0))
        end = _number(payload.get("end_s", duration))
        if start is None or end is None:
            state.invalid_value()
            return None
        start, end = max(0.0, start), min(end, duration) if duration > 0 else end
        if end - start < MIN_LAYOUT_S:
            state.invalid_value()
            return None
        out["start_s"], out["end_s"] = round(start, 3), round(end, 3)
    return out


def _window_index(payload: dict, snapshot: dict, state: Any) -> int | None:
    section = snapshot.get("background") if isinstance(snapshot, dict) else None
    windows = section.get("windows") if isinstance(section, dict) else None
    index = payload.get("window_index")
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or not isinstance(windows, list)
        or not 1 <= index <= len(windows)
    ):
        state.invalid_value()
        return None
    return index


def _coerce_remove_background_window(
    name: str, payload: dict, snapshot: dict, state: Any
) -> dict | None:
    if _sections_section(snapshot) is None:
        _sections_closed(name, state)
        return None
    index = _window_index(payload, snapshot, state)
    return None if index is None else {"op": name, "window_index": index}


def _coerce_retime_background_window(
    name: str, payload: dict, snapshot: dict, state: Any
) -> dict | None:
    if _sections_section(snapshot) is None:
        _sections_closed(name, state)
        return None
    index = _window_index(payload, snapshot, state)
    if index is None:
        return None
    out: dict[str, Any] = {"op": name, "window_index": index}
    for key in ("start_s", "end_s"):
        if key in payload:
            number = _number(payload[key])
            if number is None or number < 0:
                state.invalid_value()
                return None
            out[key] = round(number, 3)
    if "start_s" not in out and "end_s" not in out:
        state.invalid_value()
        return None
    return out


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
    # KRI-1394: offered by the BACKGROUND section's `opening_removable` (full-opening gate open).
    OpSpec(
        name="remove_full_screen_opening",
        required=frozenset(),
        fields=frozenset(),
        family=frozenset({"background", "speaker_background"}),
        coerce=_coerce_remove_full_screen_opening,
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
    # KRI-1558: their own family, in `allowed_op_families` only while the sections gate is open.
    OpSpec(
        name="arrange_sections",
        required=frozenset(),
        fields=frozenset({"asset_ids"}),
        family=frozenset({"speaker_sections"}),
        coerce=_coerce_arrange_sections,
    ),
    OpSpec(
        name="set_section_layout",
        required=frozenset({"speaker_layout"}),
        fields=frozenset({"speaker_layout", "section_numbers", "start_s", "end_s", "preset"}),
        family=frozenset({"speaker_sections"}),
        coerce=_coerce_set_section_layout,
    ),
    OpSpec(
        name="remove_background_window",
        required=frozenset({"window_index"}),
        fields=frozenset({"window_index"}),
        family=frozenset({"speaker_sections"}),
        coerce=_coerce_remove_background_window,
    ),
    OpSpec(
        name="retime_background_window",
        required=frozenset({"window_index"}),
        fields=frozenset({"window_index", "start_s", "end_s"}),
        family=frozenset({"speaker_sections"}),
        coerce=_coerce_retime_background_window,
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
    # KRI-1558: the speaker's layout per section is kept the same way.
    if isinstance(working, dict) and working.get("layout_windows"):
        document["layout_windows"] = _copy_layouts(working["layout_windows"])


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
    if working.get("speaker_frame") == frame and not working.get("layout_windows"):
        raise KriaEditorOpError("You're already there")
    # KRI-1558: one corner for the whole video replaces the layouts per section.
    rest = {key: value for key, value in working.items() if key != "layout_windows"}
    state.speaker_background = {**rest, "speaker_frame": frame}
    state.changed.add("speaker_background")
    state.summary = "Made you small in the corner"


def _op_clear_speaker_frame(state: Any, op: dict[str, Any]) -> None:  # noqa: ARG001
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    if "selector" in op:
        _set_occurrence_frame(state, op, {"mode": "full"})
        return
    working = _working_document(state)
    # KRI-1558: "put me back full screen" clears EVERY place that makes her small: the one corner,
    # the sections drawn small or as the picture alone, and any clip's own corner (a per-clip
    # frame answered "you're already full screen" before). Her full-screen sections stay.
    reset_rows = False
    for row in state.slots:
        override = row.get("speaker_frame_override")
        if isinstance(override, dict) and override.get("mode") not in (None, "inherit"):
            row["speaker_frame_override"] = {"mode": "inherit"}
            reset_rows = True
    if reset_rows:
        state.changed.add("timeline")
    document = dict(working) if isinstance(working, dict) else None
    changed = False
    if document is not None and document.pop("speaker_frame", None) is not None:
        changed = True
    if document is not None and document.get("layout_windows"):
        # Her own take and her cut-out at full size are full screen already.
        kept = [w for w in document["layout_windows"] if w.get("layout") in ("own", "full")]
        if len(kept) != len(document["layout_windows"]):
            changed = True
        if kept:
            document["layout_windows"] = _copy_layouts(kept)
        else:
            document.pop("layout_windows")
    if not changed and not reset_rows:
        raise KriaEditorOpError("You're already full screen")
    if changed:
        state.speaker_background = document
        state.changed.add("speaker_background")
    state.summary = "Put you back full screen"


def _op_remove_full_screen_opening(state: Any, op: dict[str, Any]) -> None:  # noqa: ARG001
    from app.services.kria_editor_ops import (  # noqa: PLC0415
        KriaEditorOpError,
        background_full_opening_open,
    )

    if not background_full_opening_open(state.job, state.variant):
        raise KriaEditorOpError("Taking away the full-screen opening isn't available on this edit")
    working = _working_document(state)
    spans = working.get("full_screen_windows") if isinstance(working, dict) else None
    opening = next((span for span in spans or () if is_opening(span)), None)
    if opening is None:
        raise KriaEditorOpError("There is no full-screen opening of you to take away")
    # The fill, the timed backgrounds, the corner and any other full-screen window (an ending)
    # stay, as `removeFullScreenOpening` keeps them.
    rest = [dict(span) for span in spans if span is not opening]
    document = {key: value for key, value in working.items() if key != "full_screen_windows"}
    if rest:
        document["full_screen_windows"] = rest
    state.speaker_background = document
    state.changed.add("speaker_background")
    state.summary = "Took away the full-screen opening"


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


def _copy_layouts(windows: object) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for window in windows or ():
        if not isinstance(window, dict):
            continue
        row = {k: window[k] for k in ("start_s", "end_s", "layout") if k in window}
        if isinstance(window.get("speaker_frame"), dict):
            row["speaker_frame"] = dict(window["speaker_frame"])
        out.append(row)
    return out


def _layout_row(start: float, end: float, layout: str, preset: str | None) -> dict[str, Any]:
    from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS  # noqa: PLC0415

    row: dict[str, Any] = {"start_s": round(start, 3), "end_s": round(end, 3), "layout": layout}
    if layout == "small":
        row["speaker_frame"] = dict(SPEAKER_FRAME_PRESETS[preset or DEFAULT_SMALL_PRESET])
    return row


def _merge_layouts(
    current: list[dict[str, Any]], spans: list[tuple[float, float]], new: dict[str, Any]
) -> list[dict[str, Any]]:
    """``current`` with ``spans`` (sorted, disjoint) set to ``new`` (a row without times).

    Windows they overlap are clipped around them; a piece left under half a second goes, and
    touching windows with the same layout merge."""
    pieces: list[dict[str, Any]] = []
    for window in current:
        parts = [(float(window["start_s"]), float(window["end_s"]))]
        for start, end in spans:
            parts = [
                piece
                for a, b in parts
                for piece in ((a, min(b, start)), (max(a, end), b))
                if piece[1] - piece[0] >= MIN_LAYOUT_S - 1e-9
            ]
        pieces.extend({**window, "start_s": round(a, 3), "end_s": round(b, 3)} for a, b in parts)
    pieces.extend({**new, "start_s": round(a, 3), "end_s": round(b, 3)} for a, b in spans)
    pieces.sort(key=lambda row: row["start_s"])
    merged: list[dict[str, Any]] = []
    for row in pieces:
        last = merged[-1] if merged else None
        if (
            last is not None
            and abs(last["end_s"] - row["start_s"]) < 1e-6
            and last.get("layout") == row.get("layout")
            and last.get("speaker_frame") == row.get("speaker_frame")
        ):
            last["end_s"] = row["end_s"]
        else:
            merged.append(dict(row))
    return merged


def _fill_and_kinds(working: object, ids: list[str], kinds: list[str]) -> dict[str, Any]:
    """The fill a rebuilt document keeps: the colour or picture now behind her, else the first
    picture named."""
    if isinstance(working, dict) and working.get("color"):
        return {"kind": "color", "color": working["color"]}
    if isinstance(working, dict) and working.get("media_id"):
        fill = {
            "media_id": working["media_id"],
            "kind": working["kind"],
            "source_start_s": working.get("source_start_s", 0.0),
        }
        if working.get("focus"):
            fill["focus"] = dict(working["focus"])
        return fill
    return {"media_id": ids[0], "kind": kinds[0], "source_start_s": 0.0}


def _op_arrange_sections(state: Any, op: dict[str, Any]) -> None:
    from app.pipeline.talking_sections import arrange_layouts  # noqa: PLC0415
    from app.services.kria_editor_ops import (  # noqa: PLC0415
        KriaEditorOpError,
        background_current,
        speaker_sections_for,
    )

    working = _working_document(state)
    ids: list[str] = list(op.get("asset_ids") or ())
    kinds: list[str] = list(op.get("kinds") or ())
    if not ids and isinstance(working, dict):
        # The pictures already behind her, in the order they show (each once).
        for window in working.get("windows") or ():
            if isinstance(window, dict) and window.get("media_id") not in ids:
                ids.append(window["media_id"])
                kinds.append(window.get("kind", "image"))
        if not ids and working.get("media_id"):
            ids, kinds = [working["media_id"]], [working.get("kind", "image")]
    if not ids:
        raise KriaEditorOpError(
            "Add photos or videos from the Visuals tab first, then I can lay them over the parts"
        )
    sections = speaker_sections_for(state.variant)
    arranged = arrange_layouts(sections, ids)
    if not arranged:
        raise KriaEditorOpError("This video is too short to change where you are part by part")
    kind_of = dict(zip(ids, kinds, strict=False))
    known_focus = _focus_by_media(working)
    document: dict[str, Any] = _fill_and_kinds(working, ids, kinds)
    windows = [
        {
            "media_id": row.media_id,
            "kind": kind_of.get(row.media_id, "image"),
            "source_start_s": 0.0,
            "start_s": row.start_s,
            "end_s": row.end_s,
            **({"focus": dict(known_focus[row.media_id])} if row.media_id in known_focus else {}),
        }
        for row in arranged
        if row.media_id is not None
    ]
    if windows:
        document["windows"] = windows
    document["layout_windows"] = [
        _layout_row(row.start_s, row.end_s, row.layout, row.preset) for row in arranged
    ]
    if isinstance(working, dict) and working.get("full_screen_windows"):
        document["full_screen_windows"] = [dict(span) for span in working["full_screen_windows"]]
    if document == background_current(state.job, state.variant):
        raise KriaEditorOpError("That's already how you're laid out")
    state.speaker_background = document
    state.changed.add("speaker_background")
    shown = sum(1 for row in arranged if row.layout != "own")
    state.summary = (
        f"Laid {len(windows)} picture{'s' if len(windows) != 1 else ''} over the parts of your "
        f"video: you're full screen at the start and the end, and {shown} "
        f"part{'s' if shown != 1 else ''} in between show the picture"
    )


def _op_set_section_layout(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import (  # noqa: PLC0415
        KriaEditorOpError,
        background_current,
        background_duration_s,
        speaker_sections_for,
    )

    working = _working_document(state)
    layout = op["speaker_layout"]
    if layout != "own" and not (
        isinstance(working, dict) and (working.get("media_id") or working.get("color"))
    ):
        raise KriaEditorOpError("Put a photo or video behind you first")
    duration = background_duration_s(state.variant)
    if "section_numbers" in op:
        sections = speaker_sections_for(state.variant)
        chosen = [s for s in sections if s.index in op["section_numbers"]]
        if len(chosen) != len(op["section_numbers"]):
            raise KriaEditorOpError("I couldn't find that part of the video")
        spans = [(s.start_s, s.end_s) for s in chosen]
        where = "in that part" if len(spans) == 1 else "in those parts"
    elif "start_s" in op:
        spans = [(float(op["start_s"]), float(op["end_s"]))]
        where = f"from {op['start_s']:g}s to {op['end_s']:g}s"
    else:
        if duration < MIN_LAYOUT_S:
            raise KriaEditorOpError("I can't tell how long this video is")
        spans = [(0.0, duration)]
        where = "for the whole video"
    # Touching spans (neighbouring sections) are one.
    joined: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if joined and start <= joined[-1][1] + 1e-6:
            joined[-1] = (joined[-1][0], max(joined[-1][1], end))
        else:
            joined.append((start, end))
    new = _layout_row(0.0, 0.0, layout, op.get("preset"))
    new = {k: v for k, v in new.items() if k not in ("start_s", "end_s")}
    current = _copy_layouts(working.get("layout_windows")) if isinstance(working, dict) else []
    merged = _merge_layouts(current, joined, new)
    if len(merged) > MAX_LAYOUT_WINDOWS:
        raise KriaEditorOpError("That's more changes of place than one video can hold")
    document: dict[str, Any] = dict(working) if isinstance(working, dict) else {}
    if not document:
        raise KriaEditorOpError("Put a photo or video behind you first")
    document["layout_windows"] = merged
    if document == background_current(state.job, state.variant):
        raise KriaEditorOpError("You're already like that there")
    state.speaker_background = document
    state.changed.add("speaker_background")
    state.summary = {
        "own": "Put you full screen",
        "full": "Put you big over the picture",
        "small": "Made you small in the corner",
        "picture": "Showed the picture alone",
    }[layout] + f" {where}"


def _op_remove_background_window(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    working = _working_document(state)
    windows = list(working.get("windows") or ()) if isinstance(working, dict) else []
    index = int(op["window_index"])
    if not 1 <= index <= len(windows):
        raise KriaEditorOpError("I couldn't find that picture")
    document = dict(working)
    rest = [dict(window) for i, window in enumerate(windows, start=1) if i != index]
    if rest:
        document["windows"] = rest
    else:
        document.pop("windows", None)
    state.speaker_background = document
    state.changed.add("speaker_background")
    state.summary = "Took that picture away"


def _op_retime_background_window(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import (  # noqa: PLC0415
        KriaEditorOpError,
        background_duration_s,
    )

    working = _working_document(state)
    windows = [dict(w) for w in (working.get("windows") or ())] if isinstance(working, dict) else []
    index = int(op["window_index"])
    if not 1 <= index <= len(windows):
        raise KriaEditorOpError("I couldn't find that picture")
    window = windows[index - 1]
    start = float(op.get("start_s", window["start_s"]))
    end = float(op.get("end_s", window["end_s"]))
    duration = background_duration_s(state.variant)
    if duration > 0:
        end = min(end, duration)
    if end - start < MIN_LAYOUT_S:
        raise KriaEditorOpError("A picture needs to show for at least half a second")
    before = windows[index - 2] if index >= 2 else None
    after = windows[index] if index < len(windows) else None
    if (before is not None and start < float(before["end_s"]) - 1e-6) or (
        after is not None and end > float(after["start_s"]) + 1e-6
    ):
        raise KriaEditorOpError("That would run over the next picture; move or remove it first")
    if abs(start - float(window["start_s"])) < 1e-6 and abs(end - float(window["end_s"])) < 1e-6:
        raise KriaEditorOpError("That picture already shows then")
    window["start_s"], window["end_s"] = round(start, 3), round(end, 3)
    document = dict(working)
    document["windows"] = windows
    state.speaker_background = document
    state.changed.add("speaker_background")
    state.summary = f"Moved that picture to {start:g}s-{end:g}s"


def register_handlers() -> None:
    from app.services import kria_editor_ops  # noqa: PLC0415

    kria_editor_ops.register_handler("set_background", _op_set_background)
    kria_editor_ops.register_handler("set_background_color", _op_set_background_color)
    kria_editor_ops.register_handler("remove_background", _op_remove_background)
    kria_editor_ops.register_handler("remove_full_screen_opening", _op_remove_full_screen_opening)
    kria_editor_ops.register_handler("set_background_sequence", _op_set_background_sequence)
    kria_editor_ops.register_handler("set_background_focus", _op_set_background_focus)
    kria_editor_ops.register_handler("set_speaker_frame", _op_set_speaker_frame)
    kria_editor_ops.register_handler("clear_speaker_frame", _op_clear_speaker_frame)
    kria_editor_ops.register_handler("arrange_sections", _op_arrange_sections)
    kria_editor_ops.register_handler("set_section_layout", _op_set_section_layout)
    kria_editor_ops.register_handler("remove_background_window", _op_remove_background_window)
    kria_editor_ops.register_handler("retime_background_window", _op_retime_background_window)
