"""Part of a split section plays full screen: the server port of the iPhone editor's planner.

``plan_split_full_screen`` is ``NativeEditorSession.splitFullScreenResult`` (with ``piece(of:)``,
``NativeClipSourceClock``, ``fitCollapseSectionDurations`` and ``splitScreenIsValid``), and
``remove_time`` is ``NativeSplitTimelineRebase.removeTime``, the lane step ``makeSplitFullScreen``
runs on the result. The section is cut at the playhead. Every panel clip is split there; one
panel's footage on the chosen side leaves the section as ordinary full-screen clips. A gap in that
panel is filled by the clip before it (the first clip reaches back to the cut); where footage runs
out only the empty time goes, and the lanes after it move earlier with it.

The phone reads two things the timeline rows do not carry: the real length of each source file
(``ResolvedEditorSource.asset.duration``) and whether a source is a photo. They come in as
``measured`` and ``stills``. Nothing else is device-only: no frames, no faces.

``tests/fixtures/split_full_screen.json`` holds the shared cases; ``tests/kria/
test_split_full_screen_parity.py`` asserts this module against it and
``NativeSplitFullScreenParityTests`` asserts the Swift planner against the same file.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from app.kria.split_screen import SplitScreenSection, layout_split_timeline
from app.services.editor_limits import EDITOR_MIN_CLIP_S, FRAME_S

Side = Literal["from", "until"]
SIDES: tuple[Side, ...] = ("from", "until")
# The playhead needs at least this much of the section on each side of it.
MIN_SIDE_S = 0.5
_EPS = 1e-6
_MEMBERSHIP = ("split_section_id", "split_panel_index", "split_offset_s")
_MAX_TIMELINE_S = 1800.0
_SPEAKER_WINDOW_MIN_S = 0.5


@dataclass(frozen=True)
class SplitFullScreenPlan:
    slots: list[dict[str, Any]]
    sections: list[dict[str, Any]]
    # Timeline seconds the video loses where the footage ran out, disjoint and ascending.
    lost: tuple[tuple[float, float], ...]
    panel: int


@dataclass(frozen=True)
class SectionPlace:
    section_id: str
    start_s: float
    end_s: float


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _present(row: dict[str, Any], key: str) -> bool:
    """Swift ``raw[key] != nil``: the key is there, even as null."""
    return key in row


def _section_of(row: dict[str, Any]) -> str | None:
    value = row.get("split_section_id")
    return value if isinstance(value, str) else None


def _panel_of(row: dict[str, Any]) -> int | None:
    value = _num(row.get("split_panel_index"))
    if value is None or not math.isfinite(value) or value != round(value) or not 0 <= value <= 2:
        return None
    return int(value)


def _offset(row: dict[str, Any]) -> float:
    value = _num(row.get("split_offset_s"))
    return 0.0 if value is None else value


def _length(row: dict[str, Any]) -> float | None:
    return _num(row.get("duration_s"))


def _removed(row: dict[str, Any]) -> bool:
    return row.get("removed") is True


def _transition(row: dict[str, Any]) -> str:
    value = row.get("transition_after")
    return value if isinstance(value, str) else "cut"


def _round_ms(value: float) -> float:
    """Swift ``(value * 1000).rounded() / 1000``: halves round away from zero."""
    scaled = value * 1000
    return math.copysign(math.floor(abs(scaled) + 0.5), scaled) / 1000


def _set_cut(row: dict[str, Any]) -> None:
    row["transition_after"] = "cut"
    row["transition_duration_s"] = None


def _clear_membership(row: dict[str, Any]) -> None:
    for key in _MEMBERSHIP:
        row.pop(key, None)


def is_still(row: dict[str, Any], stills: frozenset[int]) -> bool:
    """``isStillSlot``: the row says it is a photo, or its source file is one."""
    kind = row.get("media_kind")
    if not isinstance(kind, str):
        kind = row.get("kind") if isinstance(row.get("kind"), str) else None
    return kind == "image" or row.get("clip_index") in stills


def _stored_source_length(row: dict[str, Any]) -> float | None:
    """``source_duration_s``, else ``source_duration``; a present key wins even when null."""
    if "source_duration_s" in row:
        return _num(row["source_duration_s"])
    return _num(row.get("source_duration"))


def source_rate(row: dict[str, Any]) -> float:
    """``effectiveSourceRate`` on the row's own timeline clip: an explicit ``playback_rate``, else
    the projected source span over the slot length (slowed narration, older retimes)."""
    explicit = _num(row.get("playback_rate"))
    if explicit is not None and math.isfinite(explicit) and explicit > 0:
        return explicit
    length = _length(row)
    moving = max(EDITOR_MIN_CLIP_S, length if length is not None else 0.0)
    start = max(0.0, _num(row.get("in_s")) or 0.0)
    total = _stored_source_length(row)
    available = None if total is None else max(0.0, total - start)
    end = _num(row.get("source_end_s"))
    selected = None
    if end is not None and math.isfinite(end - start) and end - start > 0:
        selected = end - start
    native = _num(row.get("native_source_span_s"))
    span = selected if selected is not None else native if native is not None else moving
    span = min(span, available if available is not None else math.inf)
    derived = span / max(EDITOR_MIN_CLIP_S, length if length is not None else span)
    return derived if math.isfinite(derived) and derived > 0 else 1.0


@dataclass
class _Clock:
    """``NativeClipSourceClock``: moving footage followed by a held frame."""

    start: float
    end: float
    rate: float
    hold: float
    frame: float | None

    @property
    def moving(self) -> float:
        return max(0.0, self.end - self.start) / self.rate

    @property
    def duration(self) -> float:
        return self.moving + self.hold

    @classmethod
    def of(cls, row: dict[str, Any]) -> _Clock:
        start = _num(row.get("in_s")) or 0.0
        rate = _num(row.get("playback_rate"))
        rate = 1.0 if rate is None else rate
        hold = max(0.0, _num(row.get("freeze_hold_s")) or 0.0)
        frame = _num(row.get("freeze_frame_s"))
        length = _length(row)
        end = frame
        if end is None:
            end = _num(row.get("source_end_s"))
        if end is None:
            native = _num(row.get("native_source_span_s"))
            span = native if native is not None else max(0.0, (length or 0.0) - hold) * rate
            end = start + span
        clock = cls(start, end, rate, hold, frame)
        if frame is not None:
            clock.hold = max(0.0, (length if length is not None else clock.duration) - clock.moving)
        return clock

    def sliced(self, lower: float, upper: float) -> _Clock:
        moving = self.moving
        start = max(0.0, self.start + min(lower, moving) * self.rate)
        result = _Clock(
            start,
            max(start, self.start + min(upper, moving) * self.rate),
            self.rate,
            max(0.0, upper - max(lower, moving)),
            self.frame,
        )
        if result.end - result.start < _EPS and result.hold > 0 and self.frame is not None:
            result.end = self.frame
            result.start = max(0.0, self.frame - FRAME_S)
            result.rate = 1.0
            result.hold = max(0.0, upper - lower - result.moving)
        return result

    def applied(self, row: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(row)
        out.update(in_s=self.start, duration_s=self.duration, duration_beats=None)
        out["source_end_s"] = self.end
        out["playback_rate"] = self.rate
        out.pop("native_source_span_s", None)
        if self.frame is not None and self.hold > 0:
            out["freeze_frame_s"] = self.frame
            out["freeze_hold_s"] = self.hold
        else:
            out["freeze_frame_s"] = None
            out["freeze_hold_s"] = None
        return out


def _piece(row: dict[str, Any], a: float, b: float, length: float, rate: float) -> dict[str, Any]:
    """``piece(of:from:to:length:rate:)``: output seconds ``a``..``b`` of ``row`` on its own source
    clock and at its own place in its panel."""
    late, early = a > _EPS, b < length - _EPS
    if not (late or early):
        return copy.deepcopy(row)
    if _num(row.get("freeze_frame_s")) is not None:
        piece = _Clock.of(row).sliced(a, b).applied(row)
    else:
        piece = copy.deepcopy(row)
        in_s = _num(row.get("in_s")) or 0.0
        piece["in_s"] = in_s + a * rate
        piece["duration_s"] = b - a
        if early and _present(row, "source_end_s"):
            piece["source_end_s"] = in_s + b * rate
    piece["duration_beats"] = None
    if late:
        piece["source_start_locked"] = True
    if early:
        _set_cut(piece)
    if _section_of(row) is not None:
        piece["split_offset_s"] = _offset(row) + a
    return piece


def _transition_overlap(left: dict[str, Any], left_length: float, right_length: float) -> float:
    """``NativeEditorInteraction.transitionOverlap``: owned by the left clip, rounded to ms."""
    if _transition(left) == "cut":
        return 0.0
    requested = _num(left.get("transition_duration_s"))
    overlap = min(
        0.3, 0.3 if requested is None else requested, left_length * 0.3, right_length * 0.3
    )
    return _round_ms(overlap) if overlap >= 0.1 else 0.0


def fit_collapse_durations(rows: list[dict[str, Any]], sections: list[dict[str, Any]]) -> None:
    """``fitCollapseSectionDurations``: a collapsing section lasts until its longest member ends."""
    for section in sections:
        if section.get("collapse_empty_panels") is not True:
            continue
        ends = [
            _offset(row) + length
            for row in rows
            if not _removed(row)
            and _section_of(row) == section["id"]
            and (length := _length(row)) is not None
        ]
        if ends and math.isfinite(max(ends)) and abs(max(ends) - section["duration_s"]) > _EPS:
            section["duration_s"] = max(ends)


def split_valid(rows: list[dict[str, Any]], sections: list[dict[str, Any]]) -> bool:
    """``EditorDocument.splitScreenIsValid``."""
    try:
        parsed = [SplitScreenSection.model_validate(section) for section in sections]
    except (ValueError, TypeError):
        return False
    by_id = {section.id: section for section in parsed}
    if len(sections) > 100 or len(by_id) != len(parsed):
        return False
    if any(not section.id.strip() for section in parsed):
        return False
    seen: set[str] = set()
    previous: str | None = None
    live = [row for row in rows if not _removed(row)]
    for row in live:
        section_id = _section_of(row)
        if section_id is None:
            if any(row.get(key) is not None for key in _MEMBERSHIP):
                return False
            previous = None
            continue
        section = by_id.get(section_id)
        panel, offset, length = _panel_of(row), _num(row.get("split_offset_s")), _length(row)
        if (
            section is None
            or panel is None
            or panel >= section.panel_count
            or offset is None
            or not math.isfinite(offset)
            or offset < 0
            or length is None
            or not math.isfinite(length)
            or length < EDITOR_MIN_CLIP_S
            or offset + length > section.duration_s + _EPS
        ):
            return False
        if previous != section_id:
            if section_id in seen:
                return False
            seen.add(section_id)
        previous = section_id
    for section in parsed:
        for panel in range(section.panel_count):
            members = sorted(
                (r for r in live if _section_of(r) == section.id and _panel_of(r) == panel),
                key=_offset,
            )
            for i, right in enumerate(members):
                for j in range(i):
                    left = members[j]
                    overlap = _offset(left) + (_length(left) or 0.0) - _offset(right)
                    if overlap <= _EPS:
                        continue
                    requested = _num(left.get("transition_duration_s"))
                    allowed = min(
                        0.3,
                        0.3 if requested is None else requested,
                        (_length(left) or 0.0) * 0.3,
                        (_length(right) or 0.0) * 0.3,
                    )
                    if not (
                        j == i - 1
                        and _offset(right) > _offset(left)
                        and _transition(left) != "cut"
                        and allowed >= 0.1
                        and overlap <= allowed + _EPS
                    ):
                        return False
    if seen != set(by_id):
        return False
    if not sections:
        return True
    try:
        return layout_split_timeline(rows, sections)[2] <= _MAX_TIMELINE_S
    except (ValueError, TypeError):
        return False


def section_places(
    rows: list[dict[str, Any]], sections: list[dict[str, Any]]
) -> tuple[list[SectionPlace], float]:
    """Each section's timeline window in list order, and the video's length."""
    starts, _placed, total = layout_split_timeline(rows, sections)
    first_start: dict[str, float] = {}
    for row, start in zip((r for r in rows if not _removed(r)), starts, strict=True):
        section_id = _section_of(row)
        if section_id is not None and section_id not in first_start:
            first_start[section_id] = start - _offset(row)
    places = [
        SectionPlace(s["id"], first_start[s["id"]], first_start[s["id"]] + float(s["duration_s"]))
        for s in sections
        if s["id"] in first_start
    ]
    return places, total


def section_at(
    rows: list[dict[str, Any]], sections: list[dict[str, Any]], playhead_s: float
) -> SectionPlace | None:
    """``splitContextAtPlayhead``: the first section whose window holds the playhead."""
    places, total = section_places(rows, sections)
    for place in places:
        inside = playhead_s < place.end_s or (
            playhead_s <= place.end_s and place.end_s >= total - _EPS
        )
        if place.start_s <= playhead_s and inside:
            return place
    return None


def cut_range(place: SectionPlace) -> tuple[float, float] | None:
    """The playhead positions (timeline seconds) a cut of this section is offered at."""
    low, high = place.start_s + MIN_SIDE_S, place.end_s - MIN_SIDE_S
    return (low, high) if high >= low else None


def _overlap(
    row: dict[str, Any], section_id: str, part: tuple[float, float]
) -> tuple[float, float] | None:
    length = _length(row)
    if _removed(row) or _section_of(row) != section_id or length is None:
        return None
    start, end = max(_offset(row), part[0]), min(_offset(row) + length, part[1])
    return (start, end) if end - start >= EDITOR_MIN_CLIP_S else None


def _parts(duration: float, cut: float, side: Side) -> tuple[tuple[float, float], ...]:
    full = (cut, duration) if side == "from" else (0.0, cut)
    kept = (0.0, cut) if side == "from" else (cut, duration)
    return full, kept


def _cut(place: SectionPlace, playhead_s: float) -> float:
    return _round_ms(playhead_s - place.start_s)


def panels_with_footage(
    rows: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    *,
    section_id: str,
    playhead_s: float,
    side: Side,
) -> list[int]:
    """Panels that have footage on the side that would go full screen, ascending."""
    section = next((s for s in sections if s.get("id") == section_id), None)
    place = section_at(rows, sections, playhead_s)
    if section is None or place is None or place.section_id != section_id:
        return []
    full, _kept = _parts(float(section["duration_s"]), _cut(place, playhead_s), side)
    return sorted(
        {
            panel
            for row in rows
            if _overlap(row, section_id, full) is not None and (panel := _panel_of(row)) is not None
        }
    )


def plan_split_full_screen(
    rows: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    *,
    section_id: str,
    playhead_s: float,
    side: Side,
    selected_panel: int | None = None,
    measured: Mapping[int, float] | None = None,
    stills: Iterable[int] = (),
    new_id: Callable[[], str] = lambda: str(uuid4()),
) -> SplitFullScreenPlan | None:
    """The timeline after ``makeSplitFullScreen``, or None where the phone does nothing.

    ``selected_panel`` is the panel of the selected clip: it goes full screen when it has footage
    on that side, else the first panel that does (the phone with nothing selected). ``measured``
    maps a clip index to its source file's real length; ``stills`` lists photo clip indexes.
    """
    measured = dict(measured or {})
    still_set = frozenset(stills)
    try:
        if not split_valid(rows, sections):
            return None
        place = section_at(rows, sections, playhead_s)
    except (ValueError, TypeError):
        return None
    section = next((s for s in sections if s.get("id") == section_id), None)
    if section is None or place is None or place.section_id != section_id:
        return None
    members = [
        i for i, row in enumerate(rows) if not _removed(row) and _section_of(row) == section_id
    ]
    if not members:
        return None
    first, last = members[0], members[-1]
    duration = float(section["duration_s"])
    cut = _cut(place, playhead_s)
    if not (cut >= MIN_SIDE_S - _EPS and duration - cut >= MIN_SIDE_S - _EPS):
        return None
    base = place.start_s
    full, kept = _parts(duration, cut, side)
    block = rows[first : last + 1]
    footage = {
        panel
        for row in block
        if _overlap(row, section_id, full) is not None and (panel := _panel_of(row)) is not None
    }
    if not footage:
        return None
    panel = selected_panel if selected_panel in footage else min(footage)

    section_rows: list[dict[str, Any]] = []
    run: list[dict[str, Any]] = []  # {slot, start, end, rate}
    for row in block:
        length = _length(row)
        if length is None or _removed(row) or _section_of(row) != section_id:
            section_rows.append(copy.deepcopy(row))
            continue
        rate = source_rate(row)
        candidates = [
            (_overlap(row, section_id, kept), True),
            (_overlap(row, section_id, full) if _panel_of(row) == panel else None, False),
        ]
        parts = sorted(
            ((span[0], span[1], in_section) for span, in_section in candidates if span),
            key=lambda part: part[0],
        )
        for position, (start, end, in_section) in enumerate(parts):
            piece = _piece(row, start - _offset(row), end - _offset(row), length, rate)
            if position > 0:
                piece["slot_id"] = new_id()
                piece["parent_segment_id"] = (
                    None if is_still(row, still_set) else row.get("slot_id")
                )
            if in_section:
                if side == "until":
                    piece["split_offset_s"] = float(start - cut)
                section_rows.append(piece)
            else:
                _clear_membership(piece)
                run.append({"slot": piece, "start": start, "end": end, "rate": rate})
    if not any(not _removed(r) and _section_of(r) == section_id for r in section_rows) or not run:
        return None
    run.sort(key=lambda item: item["start"])

    def grow(index: int, amount: float, at_start: bool) -> float:
        """Lengthens run clip ``index`` by up to ``amount`` of its own footage."""
        slot, rate = run[index]["slot"], run[index]["rate"]
        length = _length(slot)
        if amount <= _EPS or _num(slot.get("freeze_frame_s")) is not None or length is None:
            return 0.0
        still = is_still(slot, still_set)
        in_s = _num(slot.get("in_s")) or 0.0
        stored_end = _num(slot.get("source_end_s"))
        source_end = stored_end if stored_end is not None else in_s + length * rate
        if still:
            room = math.inf
        elif at_start:
            room = in_s / rate
        else:
            known = [
                v
                for v in (_stored_source_length(slot), measured.get(slot.get("clip_index")))
                if v is not None
            ]
            room = max(0.0, min(known) - source_end) / rate if known else 0.0
        grown = min(amount, room)
        if grown <= _EPS:
            return 0.0
        longer = copy.deepcopy(slot)
        longer["duration_s"] = length + grown
        longer["duration_beats"] = None
        if at_start:
            if not still:
                longer["in_s"] = in_s - grown * rate
                longer["source_start_locked"] = True
            run[index]["start"] -= grown
        else:
            if _present(slot, "source_end_s"):
                longer["source_end_s"] = source_end + grown * rate
            run[index]["end"] += grown
        run[index]["slot"] = longer
        return grown

    lost: list[tuple[float, float]] = []
    covered = full[0]
    for index in range(len(run)):
        gap = run[index]["start"] - covered
        if gap > _EPS:
            forward = grow(index - 1, gap, False) if index > 0 else 0.0
            backward = grow(index, gap - forward, True)
            if gap - forward - backward > _EPS:
                lost.append((covered + forward, run[index]["start"]))
        covered = max(covered, run[index]["end"])
    tail = full[1] - covered
    if tail > _EPS and run:
        forward = grow(len(run) - 1, tail, False)
        if tail - forward > _EPS:
            lost.append((covered + forward, full[1]))
    # Full-screen clips crossfade by their transition's own length. An overlap they cannot
    # replay becomes a cut.
    for index in reversed(range(1, len(run))):
        if not run[index - 1]["end"] > run[index]["start"] + _EPS:
            continue
        left = run[index - 1]["slot"]
        length = _length(left) or 0.0
        shared = run[index - 1]["end"] - run[index]["start"]
        replayed = _transition_overlap(left, length, _length(run[index]["slot"]) or 0.0)
        if abs(replayed - shared) <= 0.001:
            continue
        run[index - 1]["slot"] = _piece(left, 0.0, length - shared, length, run[index - 1]["rate"])
        run[index - 1]["end"] = run[index]["start"]
    for index in range(len(run)):
        if index == len(run) - 1 or run[index]["end"] <= run[index + 1]["start"] + _EPS:
            _set_cut(run[index]["slot"])

    before = [copy.deepcopy(row) for row in rows[:first]]
    if side == "until":
        previous = next((i for i in reversed(range(len(before))) if not _removed(before[i])), None)
        if (
            previous is not None
            and _section_of(before[previous]) is None
            and _transition(before[previous]) != "cut"
        ):
            # It met the section with a cut; it meets the full-screen clips the same way.
            _set_cut(before[previous])
    run_rows = [item["slot"] for item in run]
    ordered = section_rows + run_rows if side == "from" else run_rows + section_rows
    after = [copy.deepcopy(row) for row in rows[last + 1 :]]
    next_rows = before + ordered + after
    next_sections = copy.deepcopy(sections)
    target = next(s for s in next_sections if s["id"] == section_id)
    target["duration_s"] = kept[1] - kept[0]
    fit_collapse_durations(next_rows, next_sections)
    remaining = float(target["duration_s"])
    if kept[1] - kept[0] - remaining > _EPS:
        lost.append((kept[0] + remaining, kept[1]))
    if not split_valid(next_rows, next_sections):
        return None
    # transactDocument: a section with no live member goes, collapse sections refit.
    live = {_section_of(r) for r in next_rows if not _removed(r)}
    next_sections = [s for s in next_sections if s["id"] in live]
    fit_collapse_durations(next_rows, next_sections)
    if not split_valid(next_rows, next_sections):
        return None
    return SplitFullScreenPlan(
        slots=next_rows,
        sections=next_sections,
        lost=tuple((base + a, base + b) for a, b in lost),
        panel=panel,
    )


def _point(spans: tuple[tuple[float, float], ...], time: float) -> float:
    return time - sum(max(0.0, min(time, end) - start) for start, end in spans)


def _move_span(row: dict[str, Any], spans: tuple[tuple[float, float], ...]) -> None:
    start, end = _num(row.get("start_s")), _num(row.get("end_s"))
    if start is not None and end is not None:
        row["start_s"], row["end_s"] = _point(spans, start), _point(spans, end)


def _drop_empty(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if not (
            (start := _num(row.get("start_s"))) is not None
            and (end := _num(row.get("end_s"))) is not None
            and end <= start
        )
    ]


@dataclass
class Lanes:
    """The timed lanes ``removeTime`` moves on an edit that is not a Talking source edit."""

    captions: list[dict[str, Any]]
    sound_effects: list[dict[str, Any]]
    media_overlays: list[dict[str, Any]]
    visual_blocks: list[dict[str, Any]]
    camera_effects: list[dict[str, Any]]
    drawings: list[dict[str, Any]]
    speaker_background: dict[str, Any] | None


def _speaker_window_valid(window: dict[str, Any]) -> bool:
    start, end = _num(window.get("start_s")), _num(window.get("end_s"))
    return (
        start is not None
        and end is not None
        and math.isfinite(start)
        and math.isfinite(end)
        and start >= 0
        and end <= _MAX_TIMELINE_S
        and end - start >= _SPEAKER_WINDOW_MIN_S
    )


def remove_time(lanes: Lanes, lost: Iterable[tuple[float, float]]) -> Lanes:
    """``NativeSplitTimelineRebase.removeTime``: the timeline lost ``lost`` (disjoint). A lane after
    a span moves earlier by its length; a lane inside one shrinks to its start and, when nothing of
    it is left, goes. Text is not moved here (only a Talking source edit moves it)."""
    spans = tuple((a, b) for a, b in lost if b > a)
    out = copy.deepcopy(lanes)
    if not spans:
        return out
    for cue in out.captions:
        _move_span(cue, spans)
        for word in cue.get("words") or []:
            if isinstance(word, dict):
                _move_span(word, spans)
    for effect in out.sound_effects:
        at = _num(effect.get("at_s"))
        if at is not None:
            effect["at_s"] = _point(spans, at)
        else:
            _move_span(effect, spans)
    for lane in (out.media_overlays, out.visual_blocks, out.camera_effects, out.drawings):
        for row in lane:
            _move_span(row, spans)
    if out.speaker_background is not None:
        for key in ("windows", "full_screen_windows"):
            windows = out.speaker_background.get(key)
            if not isinstance(windows, list):
                continue
            for window in windows:
                _move_span(window, spans)
            kept = [w for w in windows if isinstance(w, dict) and _speaker_window_valid(w)]
            out.speaker_background[key] = sorted(kept, key=lambda w: float(w["start_s"]))
    out.captions = _drop_empty(out.captions)
    out.media_overlays = _drop_empty(out.media_overlays)
    out.visual_blocks = _drop_empty(out.visual_blocks)
    out.camera_effects = _drop_empty(out.camera_effects)
    out.drawings = _drop_empty(out.drawings)
    return out
