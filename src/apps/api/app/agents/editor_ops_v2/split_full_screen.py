"""Part of a split screen plays full screen, from chat (KRI-1394).

The editor's "Full screen from playhead" and "Full screen until playhead".

``make_split_full_screen {section_id, at_s, side, panel?}`` is ``NativeEditorSession
.makeSplitFullScreen`` with the playhead at ``at_s`` (video seconds) and, when ``panel`` is given,
that panel's clip selected. ``side: "from"`` plays the section full screen from ``at_s`` to its
end, ``"until"`` from its start to ``at_s``. The cut is planned by the server port of the phone's
planner (``app/kria/split_full_screen.py``, proven equal on
``tests/fixtures/split_full_screen.json``) and compiles into the Save sections the phone writes:
``timeline_slots`` and ``split_screen_sections``, plus every timed lane ``removeTime`` moves when
footage runs out.

Chat differs from a tap in one place: a named panel with no footage on that side is refused, where
the phone quietly uses the top-most panel that has some (KRI-129: no silent overrides).

Refused where the phone moves something this port does not: a Talking source edit (its lanes
follow the speech projection), a narrated edit (its clips' source spans are refit to the
voiceover), and text the phone would re-time (``GuidedLabelRebase``) that lies over the section,
or after it when time is lost. The snapshot's ``split_full_screen`` section is present only where
the op may run, so every other snapshot and prompt is byte-identical.
"""

from __future__ import annotations

import copy
import math
from typing import Any

from app.agents.editor_ops_v2 import OpSpec
from app.kria.reply_language import say
from app.kria.split_full_screen import (
    MIN_SIDE_S,
    SIDES,
    Lanes,
    cut_range,
    panels_with_footage,
    plan_split_full_screen,
    remove_time,
    section_at,
    section_places,
)

OP = "make_split_full_screen"
CLOSED_NOTE = (
    "Part of a split screen can't go full screen from chat on this edit.",
    "Bu düzenlemede bölünmüş ekranın bir kısmını sohbetten tam ekran yapamıyorum.",
)
_TALKING_SLOT_PREFIX = "native-phone-talking-source"
# Text the phone keeps where it is (``GuidedLabelRebase.isUntouched``).
_GUIDED_TEXT_IDS = ("guided-title", "guided-closing-title")
_EDGE_S = 0.05
_EPS = 1e-6


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def unavailable_reason(variant: dict[str, Any], rows: list[dict[str, Any]]) -> str | None:
    """Why this edit cannot make a split full screen from chat at all, or None."""
    if variant.get("resolved_archetype") == "narrated":
        return "A narrated edit fits each clip to the voiceover; make it full screen in the editor"
    if any(str(row.get("slot_id") or "").startswith(_TALKING_SLOT_PREFIX) for row in rows):
        return "A Talking edit's lanes follow the speech; make it full screen in the editor"
    return None


def split_full_screen_snapshot(
    job: Any, variant: dict[str, Any], rows: list[dict[str, Any]], sections: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Each split section chat can make part full screen, with the ``at_s`` range, or None."""
    from app.services.kria_editor_ops import split_screen_available  # noqa: PLC0415

    if not sections or not split_screen_available(job, variant):
        return None
    if unavailable_reason(variant, rows) is not None:
        return None
    try:
        places, _total = section_places(rows, sections)
    except (ValueError, TypeError):
        return None
    counts = {s["id"]: s.get("panel_count") for s in sections}
    offered = []
    for place in places:
        span = cut_range(place)
        if span is None:
            continue
        offered.append(
            {
                "section_id": place.section_id,
                "start_s": round(place.start_s, 3),
                "end_s": round(place.end_s, 3),
                "at_s_min": round(span[0], 3),
                "at_s_max": round(span[1], 3),
                "panel_count": counts.get(place.section_id),
            }
        )
    return offered or None


def _refuse(state: Any) -> None:
    detail = say(en=CLOSED_NOTE[0], tr=CLOSED_NOTE[1])
    state.reject(op=OP, reason="capability_unavailable", detail=detail)
    if detail not in state.reply_notes:
        state.reply_notes.append(detail)


def _coerce(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    offered = snapshot.get("split_full_screen") if isinstance(snapshot, dict) else None
    families = (
        set(snapshot.get("allowed_op_families") or []) if isinstance(snapshot, dict) else set()
    )
    if not isinstance(offered, list) or not offered or not families & {"clip", "timeline"}:
        _refuse(state)
        return None
    section = next(
        (
            s
            for s in offered
            if isinstance(s, dict) and s.get("section_id") == payload.get("section_id")
        ),
        None,
    )
    if section is None:
        state.reject(
            op=name,
            reason="stale_target",
            detail="section_id must be a section listed in SPLIT FULL SCREEN",
        )
        return None
    side = payload.get("side")
    if side not in SIDES:
        state.invalid_value(
            "side is from (full screen from at_s to the section's end) or until (from its "
            "start to at_s)."
        )
        return None
    at = _num(payload.get("at_s"))
    low, high = _num(section.get("at_s_min")), _num(section.get("at_s_max"))
    if at is None or low is None or high is None or not low - 0.0005 <= at <= high + 0.0005:
        state.invalid_value(
            f"at_s is a moment in video seconds between {low} and {high}, at least "
            f"{MIN_SIDE_S}s from the section's start and end."
        )
        return None
    out: dict[str, Any] = {"section_id": section["section_id"], "at_s": at, "side": side}
    if payload.get("panel") is not None:
        panel = payload["panel"]
        count = section.get("panel_count")
        if isinstance(panel, bool) or not isinstance(panel, int) or not 0 <= panel < (count or 0):
            state.invalid_value("panel is 0 (top), 1 or 2, one of the section's panels.")
            return None
        out["panel"] = panel
    return out


def _source_facts(job: Any, variant: dict[str, Any]) -> tuple[dict[int, float], set[int]]:
    """What the phone reads from the files themselves: each video's real length and which
    sources are photos, from the item's ready source catalog."""
    from app.pipeline.editor_render_window import RenderWindowUnavailable  # noqa: PLC0415
    from app.services.phone_editor_sources import (  # noqa: PLC0415
        immutable_video_source_duration,
        phone_editor_source_revision,
    )

    try:
        sources = (phone_editor_source_revision(job, variant) or {}).get("sources") or []
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}, set()
    measured: dict[int, float] = {}
    stills = {i for i, source in enumerate(sources) if source.get("kind") == "image"}
    for index, source in enumerate(sources):
        if source.get("kind") != "video":
            continue
        try:
            length = immutable_video_source_duration(job, variant, index)
        except (RenderWindowUnavailable, ValueError, KeyError, TypeError, AttributeError):
            continue
        if _num(length) is not None:
            measured[index] = float(length)
    return measured, stills


def _untouched_text(row: dict[str, Any]) -> bool:
    params = row.get("source_params") if isinstance(row.get("source_params"), dict) else {}
    if params.get("source") == "caption_cue":
        return True
    kind = params.get("narration_label_kind")
    if kind is not None and kind != "participant":
        return True
    return str(row.get("id") or "").startswith("lyric_")


def _phone_retimes(row: dict[str, Any]) -> bool:
    """``GuidedLabelRebase.isGuidedText`` on a clip-timeline edit, where every text rides it."""
    if row.get("removed") or _untouched_text(row):
        return False
    text_id = str(row.get("id") or "")
    if text_id.startswith(("clip-label-", "guided-pinned-")) or text_id in _GUIDED_TEXT_IDS:
        return True
    params = row.get("source_params") if isinstance(row.get("source_params"), dict) else {}
    return params.get("read_only") is not True


def _text_in_the_way(
    text: list[dict[str, Any]], start: float, end: float, total: float, lost: bool
) -> bool:
    for row in text:
        if not isinstance(row, dict) or not _phone_retimes(row):
            continue
        a, b = _num(row.get("start_s")), _num(row.get("end_s"))
        if a is None or b is None:
            continue
        if not lost and a <= _EDGE_S and b >= total - _EDGE_S:
            continue  # whole-video text keeps its place while the length does not change
        if a < end - _EPS and b > start + _EPS:
            return True
        if lost and b > start + _EPS:
            return True
    return False


def _why_not(rows: list, sections: list, section_id: str, at: float, side: str) -> str:
    place = section_at(rows, sections, at)
    if place is None or place.section_id != section_id:
        return "That moment is not inside the split screen"
    span = cut_range(place)
    if span is None or not span[0] - 0.0005 <= at <= span[1] + 0.0005:
        return (
            f"Pick a moment at least {MIN_SIDE_S}s from the split screen's start and end "
            "(see SPLIT FULL SCREEN)"
        )
    if not panels_with_footage(rows, sections, section_id=section_id, playhead_s=at, side=side):
        return "No panel has footage on that side of the moment"
    return "That would leave nothing in the split screen"


def _op_make_split_full_screen(state: Any, op: dict[str, Any]) -> None:
    from app.services import kria_editor_ops as editor  # noqa: PLC0415

    if not editor.split_screen_available(state.job, state.variant):
        raise editor.KriaEditorOpError("Split screen is unavailable for this edit")
    if "timeline" in state.changed:
        raise editor.KriaEditorOpError(
            "Make part of the split full screen on its own, then ask for the next change"
        )
    reason = unavailable_reason(state.variant, state.slots)
    if reason:
        raise editor.KriaEditorOpError(reason)
    section_id, side = str(op.get("section_id") or ""), op.get("side")
    at = _num(op.get("at_s"))
    if side not in SIDES or at is None:
        raise editor.KriaEditorOpError("Give the moment (at_s) and the side (from or until)")
    sections = state.split_screen_sections
    if not any(s.get("id") == section_id for s in sections):
        raise editor.KriaEditorOpError("The split section no longer exists")
    panel = op.get("panel")
    if panel is not None and panel not in panels_with_footage(
        state.slots, sections, section_id=section_id, playhead_s=at, side=side
    ):
        raise editor.KriaEditorOpError(
            f"Panel {panel} has no footage on that side of the moment, so it cannot fill the "
            "screen there"
        )
    measured, stills = _source_facts(state.job, state.variant)
    plan = plan_split_full_screen(
        state.slots,
        sections,
        section_id=section_id,
        playhead_s=at,
        side=side,
        selected_panel=panel,
        measured=measured,
        stills=stills,
    )
    if plan is None:
        raise editor.KriaEditorOpError(_why_not(state.slots, sections, section_id, at, side))
    place = section_at(state.slots, sections, at)
    total = section_places(state.slots, sections)[1]
    if place is not None and _text_in_the_way(
        state.text, place.start_s, place.end_s, total, bool(plan.lost)
    ):
        raise editor.KriaEditorOpError(
            "Text is timed over the split screen; the editor re-times it in ways chat cannot "
            "match, so make it full screen in the editor"
        )
    state.slots[:] = plan.slots
    state.split_screen_sections[:] = plan.sections
    state.split_full_screen = {"lost": plan.lost, "slots": copy.deepcopy(plan.slots)}
    state.changed.add("timeline")
    state.summary = "Made part of the split screen full screen"


def follow_lost_time(state: Any) -> None:
    """The lane step of ``makeSplitFullScreen``: ``removeTime`` over the lost spans, in place of
    the serial-unit rebase every other split edit gets (the phone passes
    ``rebasesSplitLanes: false``). Only lanes that change reach the Save."""
    from app.services import kria_editor_ops as editor  # noqa: PLC0415

    record = state.split_full_screen
    if not record["lost"]:
        return
    visual = (
        state.visual_blocks
        if state.visual_blocks is not None
        else editor._visual_media_rows(state.job, state.variant)
    )
    before = Lanes(
        captions=state.captions,
        sound_effects=state.sound_effects,
        media_overlays=state.media_overlays,
        visual_blocks=visual,
        camera_effects=state.camera_effects,
        drawings=state.drawings,
        speaker_background=state.speaker_background,
    )
    moved = remove_time(before, record["lost"])
    for lane in ("captions", "sound_effects", "media_overlays", "camera_effects", "drawings"):
        if getattr(moved, lane) != getattr(before, lane):
            setattr(state, lane, getattr(moved, lane))
            state.changed.add(lane)
    if moved.speaker_background != before.speaker_background:
        state.speaker_background = moved.speaker_background
        state.changed.add("speaker_background")
    if moved.visual_blocks != before.visual_blocks:
        if editor._editor_capabilities(state.job, state.variant).get("visual_blocks") is not True:
            raise editor.KriaEditorOpError(
                "Pictures are timed after the part that goes; make it full screen in the editor"
            )
        state.visual_blocks = moved.visual_blocks
        state.changed.add("visual_media")


SPECS = [
    OpSpec(
        name=OP,
        required=frozenset({"section_id", "at_s", "side"}),
        fields=frozenset({"section_id", "at_s", "side", "panel"}),
        family=frozenset({"clip", "timeline"}),
        coerce=_coerce,
        closed_note=CLOSED_NOTE,
    )
]


def register_handlers() -> None:
    from app.services import kria_editor_ops  # noqa: PLC0415

    kria_editor_ops.register_handler(OP, _op_make_split_full_screen)
