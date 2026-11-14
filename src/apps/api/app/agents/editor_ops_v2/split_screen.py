"""AI commands for timeline-owned sections and ordinary clip membership."""

from __future__ import annotations

import copy
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.editor_ops_v2 import OpSpec
from app.kria.split_screen import (
    SplitScreenSection,
    layout_split_timeline,
    sync_collapsed_section_durations,
)


class SectionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CreateSection(SectionCommand):
    slot_indexes: list[int] = Field(min_length=1)
    panel_count: Literal[2, 3]
    start_s: float | None = Field(default=None, ge=0, le=1800)
    end_s: float | None = Field(default=None, gt=0, le=1800)
    full_length: bool = False
    duration_s: float | None = Field(default=None, ge=0.1, le=1800)
    panel_weights: list[float] | None = None
    gutter: float = Field(default=0, ge=0, le=0.1)
    background_color: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    collapse_empty_panels: bool | None = None


class UpdateSection(SectionCommand):
    section_id: str
    duration_s: float | None = Field(default=None, ge=0.1, le=1800)
    panel_count: Literal[2, 3] | None = None
    panel_weights: list[float] | None = None
    gutter: float | None = Field(default=None, ge=0, le=0.1)
    background_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    collapse_empty_panels: bool | None = None


class AssignPanel(SectionCommand):
    section_id: str
    slot_index: int = Field(ge=0, strict=True)
    panel_index: int = Field(ge=0, le=2, strict=True)
    offset_s: float = Field(default=0, ge=0, le=1800)
    duplicate: bool = False


class AddPanelClip(SectionCommand):
    section_id: str
    panel_index: int = Field(ge=0, le=2, strict=True)
    clip_index: int = Field(ge=0, strict=True)
    offset_s: float | None = Field(default=None, ge=0, le=1800)
    duration_s: float | None = Field(default=None, ge=0.1, le=1800)
    grow_section: bool = True


class SwapPanels(SectionCommand):
    section_id: str
    first_panel: int = Field(ge=0, le=2, strict=True)
    second_panel: int = Field(ge=0, le=2, strict=True)


class RemoveSection(SectionCommand):
    section_id: str
    keep_panel: int = Field(default=0, ge=0, le=2, strict=True)


class DuplicateClip(SectionCommand):
    slot_index: int = Field(ge=0, strict=True)


class ReplaceClipSource(DuplicateClip):
    clip_index: int = Field(ge=0, strict=True)


class MoveSplitClip(DuplicateClip):
    direction: Literal["earlier", "later"]


class MoveSplitScreen(SectionCommand):
    section_id: str
    direction: Literal["earlier", "later"]


_OCCURRENCE_MODELS = {
    "duplicate_clip": DuplicateClip,
    "replace_clip_source": ReplaceClipSource,
    "move_split_clip": MoveSplitClip,
    "move_split_screen": MoveSplitScreen,
}


_MODELS = {
    **_OCCURRENCE_MODELS,
    "create_split_screen": CreateSection,
    "update_split_screen": UpdateSection,
    "assign_split_panel": AssignPanel,
    "add_split_panel_clip": AddPanelClip,
    "swap_split_panels": SwapPanels,
    "remove_split_screen": RemoveSection,
}
_MEMBERSHIP = ("split_section_id", "split_panel_index", "split_offset_s")


def _coerce(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    if name in _OCCURRENCE_MODELS:
        return _coerce_occurrence(name, payload, snapshot, state)
    if snapshot.get("split_screen_available") is not True:
        state.reject(
            op=name,
            reason="capability_unavailable",
            detail="Split screen is unavailable for this edit",
        )
        return None
    try:
        clean = _MODELS[name].model_validate(payload).model_dump(exclude_none=True)
        if isinstance(payload.get("panel_count"), bool):
            raise ValueError("invalid panel count")
        slots = snapshot.get("slots") or []
        if name == "create_split_screen":
            indexes = clean["slot_indexes"]
            if any(
                isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(slots)
                for i in payload["slot_indexes"]
            ):
                raise ValueError("invalid slot index")
            if len(set(indexes)) != len(indexes):
                raise ValueError("duplicate slot index")
        else:
            section = next(
                (
                    s
                    for s in snapshot.get("split_screen_sections", [])
                    if s["id"] == clean["section_id"]
                ),
                None,
            )
            if section is None:
                raise ValueError("unknown section")
            for key in ("panel_index", "first_panel", "second_panel", "keep_panel"):
                if key in clean and clean[key] >= section["panel_count"]:
                    raise ValueError("panel outside section")
            if name == "assign_split_panel" and clean["slot_index"] >= len(slots):
                raise ValueError("invalid slot index")
            if name == "add_split_panel_clip" and not any(
                r.get("clip_index") == clean["clip_index"] and r.get("ready")
                for r in snapshot.get("replacement_source_pool", [])
            ):
                raise ValueError("Source is not approved")
        if "panel_weights" in clean and (
            len(clean["panel_weights"]) not in (2, 3) or any(w <= 0 for w in clean["panel_weights"])
        ):
            raise ValueError("invalid weights")
        return clean
    except (ValidationError, ValueError, TypeError):
        state.invalid_value()
        return None


def _coerce_occurrence(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    try:
        command = _OCCURRENCE_MODELS[name].model_validate(payload).model_dump()
        if not set(snapshot.get("allowed_op_families", [])) & {"timeline", "clip", "clips"}:
            raise ValueError("Timeline editing is unavailable")
        if "slot_index" in command:
            row = snapshot["slots"][command["slot_index"]]
            split = bool(row.get("split_section_id"))
        else:
            split = True
        if (split or name.startswith("move_split")) and not snapshot.get("split_screen_available"):
            raise ValueError("Split editing is unavailable")
        if name == "replace_clip_source" and not any(
            r.get("clip_index") == command["clip_index"] and r.get("ready")
            for r in snapshot.get("replacement_source_pool", [])
        ):
            raise ValueError("Source is not approved")
        return command
    except (ValidationError, ValueError, IndexError, KeyError, TypeError):
        state.invalid_value()
        return None


SPECS = [
    OpSpec(
        name=name,
        required=frozenset(k for k, f in model.model_fields.items() if f.is_required()),
        fields=frozenset(model.model_fields),
        family=frozenset({"clip", "timeline"}),
        coerce=_coerce,
    )
    for name, model in _MODELS.items()
]


def validate_state(state: Any) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError

    live_sections = {r.get("split_section_id") for r in state.slots if not r.get("removed")}
    state.split_screen_sections[:] = [
        s for s in state.split_screen_sections if s["id"] in live_sections
    ]
    try:
        layout_split_timeline(state.slots, state.split_screen_sections)
        sync_collapsed_section_durations(state.slots, state.split_screen_sections)
    except (ValueError, TypeError) as exc:
        raise KriaEditorOpError(str(exc)) from exc


def _bound_duration(row: dict, room: float, editor: Any) -> None:
    if room < 0.1:
        raise ValueError("The section leaves less than 0.1 seconds for this clip")
    duration = editor._slot_duration(row)
    if duration <= room + 1e-6:
        return
    from app.kria.clip_source_clock import slice_source_window

    row.update(slice_source_window(row, 0, room))


def _apply(state: Any, op: dict[str, Any]) -> None:
    from app.services import kria_editor_ops as editor

    if op["op"] in _OCCURRENCE_MODELS:
        _apply_occurrence(state, op)
        return
    if not editor.split_screen_available(state.job, state.variant):
        raise editor.KriaEditorOpError("Split screen is unavailable for this edit")
    name = op["op"]
    try:
        command = (
            _MODELS[name]
            .model_validate({k: v for k, v in op.items() if k != "op"})
            .model_dump(exclude_none=True)
        )
        if command.get("collapse_empty_panels") and not editor.split_collapse_available(
            state.job, state.variant
        ):
            raise ValueError("Collapsing panels is unavailable for this edit")
        if name == "create_split_screen":
            indexes = [editor._snapshot_slot_index(state, i) for i in command["slot_indexes"]]
            if len(set(indexes)) != len(indexes) or sorted(indexes) != list(
                range(min(indexes), max(indexes) + 1)
            ):
                raise ValueError("Choose consecutive clips for a split section")
            rows = [state.slots[i] for i in indexes]
            if any(r.get("split_section_id") or r.get("removed") for r in rows):
                raise ValueError("Choose ordinary active clips for a new section")
            count = command["panel_count"]
            full = command["full_length"]
            before, after = [], []
            if "start_s" in command or "end_s" in command:
                if len(rows) != 1 or full:
                    raise ValueError("A bounded split section needs one source occurrence")
                source = editor.copy_source_occurrence(rows[0])
                span = editor._slot_duration(source)
                start, end = command.get("start_s", 0), command.get("end_s", span)
                if end > span or end - start < 0.1:
                    raise ValueError("The split section must fit inside the selected clip")
                from app.kria.clip_source_clock import slice_source_window

                if start:
                    before.append(slice_source_window(source, 0, start))
                middle = slice_source_window(source, start, end)
                middle["slot_id"] = str(uuid4())
                rows = [middle]
                if end < span:
                    suffix = slice_source_window(source, end, span)
                    suffix["slot_id"] = str(uuid4())
                    after.append(suffix)
            if len(rows) > count and not full:
                raise ValueError("Choose at most one source clip per panel, or use full_length")
            serial_starts, _, serial_duration = layout_split_timeline(rows, [])
            duration = command.get(
                "duration_s",
                serial_duration if full else max(editor._slot_duration(r) for r in rows),
            )
            section = SplitScreenSection(
                id=str(uuid4()),
                duration_s=duration,
                panel_count=count,
                panel_weights=command.get("panel_weights", [1] * count),
                gutter=command["gutter"],
                background_color=command["background_color"],
                collapse_empty_panels=command.get("collapse_empty_panels", False),
            ).model_dump()
            members = []
            for panel in range(count):
                panel_rows = rows if full else [rows[panel] if panel < len(rows) else rows[0]]
                for source_index, source in enumerate(panel_rows):
                    offset = serial_starts[source_index] if full else 0.0
                    row = editor.copy_source_occurrence(source)
                    if (full and panel > 0) or (not full and panel >= len(rows)):
                        row["slot_id"] = str(uuid4())
                    row.update(
                        split_section_id=section["id"],
                        split_panel_index=panel,
                        split_offset_s=offset,
                    )
                    _bound_duration(row, duration - offset, editor)
                    members.append(row)
            state.slots[min(indexes) : max(indexes) + 1] = before + members + after
            state.split_screen_sections.append(section)
        else:
            section = next(
                (s for s in state.split_screen_sections if s["id"] == command["section_id"]), None
            )
            if section is None:
                raise ValueError("The split section no longer exists")
            members = [
                r
                for r in state.slots
                if r.get("split_section_id") == section["id"] and not r.get("removed")
            ]
            if name == "update_split_screen":
                updates = {k: v for k, v in command.items() if k != "section_id"}
                if "panel_count" in updates and "panel_weights" not in updates:
                    updates["panel_weights"] = [1] * updates["panel_count"]
                revised = SplitScreenSection.model_validate({**section, **updates}).model_dump()
                if not any(
                    row["split_panel_index"] < revised["panel_count"]
                    and row["split_offset_s"] < revised["duration_s"]
                    for row in members
                ):
                    raise ValueError(
                        "This edit leaves only an empty section background with no media occurrence"
                    )
                for row in members:
                    if (
                        row["split_panel_index"] >= revised["panel_count"]
                        or row["split_offset_s"] >= revised["duration_s"]
                    ):
                        row["removed"] = True
                    else:
                        _bound_duration(row, revised["duration_s"] - row["split_offset_s"], editor)
                if revised["panel_count"] > section["panel_count"]:
                    for source in members:
                        if source["split_panel_index"] == 0 and not source.get("removed"):
                            row = editor.copy_source_occurrence(source)
                            row["slot_id"] = str(uuid4())
                            row["split_panel_index"] = 2
                            position = max(
                                i
                                for i, r in enumerate(state.slots)
                                if r.get("split_section_id") == section["id"]
                            )
                            state.slots.insert(position + 1, row)
                section.pop("collapse_empty_panels", None)
                section.update(revised)
            elif name == "assign_split_panel":
                index = editor._snapshot_slot_index(state, command["slot_index"])
                row = state.slots[index]
                if row.get("removed"):
                    raise ValueError("The selected clip was removed")
                if command["duplicate"]:
                    row = editor.copy_source_occurrence(row)
                    row["slot_id"] = str(uuid4())
                else:
                    state.slots.remove(row)
                    row = editor.copy_source_occurrence(row)
                row.update(
                    split_section_id=section["id"],
                    split_panel_index=command["panel_index"],
                    split_offset_s=command["offset_s"],
                )
                _bound_duration(row, section["duration_s"] - command["offset_s"], editor)
                remaining = [
                    i
                    for i, r in enumerate(state.slots)
                    if r.get("split_section_id") == section["id"]
                ]
                state.slots.insert(max(remaining) + 1 if remaining else index, row)
            elif name == "add_split_panel_clip":
                _add_panel_clip(state, section, members, command)
            elif name == "swap_split_panels":
                a, b = command["first_panel"], command["second_panel"]
                if max(a, b) >= section["panel_count"]:
                    raise ValueError("The panel does not exist")
                for row in members:
                    panel = row["split_panel_index"]
                    row["split_panel_index"] = b if panel == a else a if panel == b else panel
            else:
                kept = sorted(
                    [r for r in members if r["split_panel_index"] == command["keep_panel"]],
                    key=lambda r: r["split_offset_s"],
                )
                if not kept:
                    raise ValueError("Choose a panel containing media to unstack")
                first = min(state.slots.index(r) for r in members)
                state.slots[:] = [
                    r for r in state.slots if r.get("split_section_id") != section["id"]
                ]
                for row in kept:
                    for key in _MEMBERSHIP:
                        row[key] = None
                state.slots[first:first] = kept
                state.split_screen_sections.remove(section)
        validate_state(state)
    except (ValueError, TypeError) as exc:
        raise editor.KriaEditorOpError(str(exc)) from exc
    state.changed.add("timeline")


# A photo added to a panel plays this long, as on the phone (`addedClipDurationS`).
_ADDED_PHOTO_S = 3.0


def _add_panel_clip(state: Any, section: dict, members: list[dict], command: dict) -> None:
    """Put an approved source into a panel like the phone's `insertSplitPanelSource`.

    It starts after the panel's last clip unless `offset_s` says where. The phone refuses a full
    panel because its person can lengthen the section by hand. Chat has no such step, so the
    section grows to hold the whole clip, unless `grow_section` is false and the clip is cut to
    the room that is left.
    """
    from app.services import kria_editor_ops as editor

    source = next(
        (
            r
            for r in editor.replacement_source_pool(state.job, state.variant)
            if r["clip_index"] == command["clip_index"] and r.get("ready")
        ),
        None,
    )
    if source is None:
        raise ValueError("Source is not approved")
    panel = command["panel_index"]
    if panel >= section["panel_count"]:
        raise ValueError("The panel does not exist")
    photo = source["kind"] == "image"
    native = _ADDED_PHOTO_S if photo else source.get("duration_s")
    if native is None or native < 0.1:
        raise ValueError("The selected video needs an approved measured duration")
    length = command.get("duration_s", native)
    if not photo:
        length = min(length, native)
    peers = [r for r in members if r["split_panel_index"] == panel]
    offset = command.get("offset_s")
    if offset is None:
        offset = max((r["split_offset_s"] + editor._slot_duration(r) for r in peers), default=0.0)
    if any(
        offset < r["split_offset_s"] + editor._slot_duration(r) - 1e-6
        and offset + length > r["split_offset_s"] + 1e-6
        for r in peers
    ):
        raise ValueError("This panel already has a clip at that time. Choose a later offset_s.")
    if command["grow_section"]:
        section["duration_s"] = max(section["duration_s"], offset + length)
    else:
        length = min(length, section["duration_s"] - offset)
        if length < 0.1:
            raise ValueError("This panel is full. Shorten a clip in it to make room.")
    row = {
        "slot_id": str(uuid4()),
        "clip_index": command["clip_index"],
        "media_id": source["media_id"],
        "media_kind": source["kind"],
        "in_s": 0.0,
        "duration_s": length,
        "duration_beats": None,
        "removed": False,
        "transition_after": "cut",
        "look_preset": "none",
        "source_start_locked": True,
        "split_section_id": section["id"],
        "split_panel_index": panel,
        "split_offset_s": offset,
    }
    if not photo:
        row["source_duration_s"] = native
    last = max(i for i, r in enumerate(state.slots) if r.get("split_section_id") == section["id"])
    state.slots.insert(last + 1, row)


def live_timeline_units(rows: list[dict]) -> list[list[dict]]:
    units: list[list[dict]] = []
    sections: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("removed"):
            continue
        section = row.get("split_section_id")
        if section and section in sections:
            sections[section].append(row)
        else:
            unit = [row]
            units.append(unit)
            if section:
                sections[section] = unit
    return units


def install_live_units(rows: list[dict], units: list[list[dict]]) -> None:
    live = iter(row for unit in units for row in unit)
    rows[:] = [row if row.get("removed") else next(live) for row in rows]


def _apply_occurrence(state: Any, op: dict) -> None:
    from app.services import kria_editor_ops as editor

    name = op["op"]
    try:
        command = (
            _OCCURRENCE_MODELS[name]
            .model_validate({k: v for k, v in op.items() if k != "op"})
            .model_dump()
        )
        if editor._editor_capabilities(state.job, state.variant).get("timeline") is not True:
            raise ValueError("Timeline editing is unavailable")
        row = None
        if "slot_index" in command:
            index = editor._snapshot_slot_index(state, command["slot_index"])
            row = state.slots[index]
        if (
            name.startswith("move_split") or (row and row.get("split_section_id"))
        ) and not editor.split_screen_available(state.job, state.variant):
            raise ValueError("Split editing is unavailable")
        if name == "duplicate_clip":
            duplicate = editor.copy_source_occurrence(row)
            duplicate.update(slot_id=str(uuid4()), parent_segment_id=None)
            if row.get("split_section_id"):
                duplicate["split_offset_s"] = row["split_offset_s"] + editor._slot_duration(row)
            state.slots.insert(index + 1, duplicate)
        elif name == "replace_clip_source":
            source = next(
                (
                    r
                    for r in editor.replacement_source_pool(state.job, state.variant)
                    if r["clip_index"] == command["clip_index"] and r.get("ready")
                ),
                None,
            )
            if source is None:
                raise ValueError("Source is not approved")
            photo = source["kind"] == "image"
            frame = row.get("speaker_frame_override") or {}
            speaker = frame.get("mode") in {"custom", "full"}
            if photo and speaker:
                raise ValueError("Use default framing before replacing this speaker with a photo")
            duration = source.get("duration_s")
            if not photo and (duration is None or duration < 0.1):
                raise ValueError("The selected video needs an approved measured duration")
            for key in (
                "asset_id",
                "media_id",
                "media_kind",
                "kind",
                "source_duration_s",
                "source_duration",
                "source_end_s",
                "source_gcs_path",
                "native_source_span_s",
                "native_composite_source",
                "freeze_frame_s",
                "freeze_hold_s",
            ):
                row.pop(key, None)
            row.update(
                clip_index=command["clip_index"],
                in_s=0,
                media_kind=source["kind"],
                speaker_subject=speaker,
                freeze_hold_s=0,
                duration_beats=None,
                source_start_locked=True,
            )
            if photo:
                row["playback_rate"] = 1.0
            else:
                row["source_duration_s"] = duration
                row["duration_s"] = min(
                    editor._slot_duration(row), duration / editor._slot_rate(row)
                )
                for key in ("layout", "still_layout"):
                    if row.get(key) == "supporting_card":
                        row[key] = "fullscreen"
        elif name == "move_split_clip":
            if not row.get("split_section_id"):
                raise ValueError("Select a split panel clip")
            peers = sorted(
                [
                    r
                    for r in state.slots
                    if not r.get("removed")
                    and r.get("split_section_id") == row["split_section_id"]
                    and r.get("split_panel_index") == row["split_panel_index"]
                ],
                key=lambda r: r["split_offset_s"],
            )
            position = next(i for i, r in enumerate(peers) if r is row)
            target = position + (-1 if command["direction"] == "earlier" else 1)
            if not 0 <= target < len(peers):
                raise ValueError("There is no neighboring clip in this panel")
            other = peers[target]
            earlier, later = sorted([row, other], key=lambda r: r["split_offset_s"])
            start = earlier["split_offset_s"]
            gap = max(0, later["split_offset_s"] - start - editor._slot_duration(earlier))
            later["split_offset_s"] = start
            earlier["split_offset_s"] = start + editor._slot_duration(later) + gap
        else:
            groups = live_timeline_units(state.slots)
            position = next(
                i
                for i, group in enumerate(groups)
                if group[0].get("split_section_id") == command["section_id"]
            )
            target = position + (-1 if command["direction"] == "earlier" else 1)
            if not 0 <= target < len(groups):
                raise ValueError("There is no neighboring timeline section")
            groups[position], groups[target] = groups[target], groups[position]
            install_live_units(state.slots, groups)
        validate_state(state)
    except (ValueError, TypeError, StopIteration) as exc:
        raise editor.KriaEditorOpError(str(exc) or "The target is no longer available") from exc
    state.changed.add("timeline")


def rebase_split_canvas(
    state: Any,
    before: dict[str, Any],
    *,
    text_timing: str,
    explicit_text_ids: set[str],
    initial: dict[str, Any],
) -> None:
    """Canvas timing follows serial units, never an individual panel's source clock."""

    def windows(
        rows: list[dict], sections: list[dict]
    ) -> list[tuple[frozenset[str], float, float]]:
        starts, _, _ = layout_split_timeline(rows, sections)
        active = [r for r in rows if not r.get("removed")]
        section_by_id = {s["id"]: s for s in sections}
        result = []
        seen = set()
        from app.services.kria_editor_ops import _slot_duration

        for row, start in zip(active, starts, strict=True):
            key = row.get("split_section_id") or row["slot_id"]
            if key in seen:
                continue
            seen.add(key)
            if row.get("split_section_id"):
                start -= row["split_offset_s"]
                duration = section_by_id[key]["duration_s"]
            else:
                duration = _slot_duration(row)
            members = (
                frozenset(str(r["slot_id"]) for r in active if r.get("split_section_id") == key)
                if row.get("split_section_id")
                else frozenset({str(row["slot_id"])})
            )
            result.append((members, start, start + duration))
        return result

    old = windows(state.initial_slots, state.initial_split_screen_sections)
    new = windows(state.slots, state.split_screen_sections)
    if old == new or not old or not new:
        return
    destinations = {}
    for members, _, _ in old:
        matches = [(a, b) for other, a, b in new if members & other]
        if matches:
            destinations[members] = (min(a for a, _ in matches), max(b for _, b in matches))
    old_end, new_end = max(r[2] for r in old), max(r[2] for r in new)

    def point(time: float) -> float:
        if time >= old_end:
            return new_end + time - old_end
        for key, start, end in reversed(old):
            if start <= time < end:
                if key not in destinations:
                    return next(
                        (destinations[k][0] for k, a, _ in old if a >= end and k in destinations),
                        new_end,
                    )
                a, b = destinations[key]
                return min(b, a + time - start)
        return min(time, new_end)

    def span(start: float, end: float) -> tuple[float, float]:
        parts = []
        for key, a, b in old:
            if a < end and b > start and key in destinations:
                x, y = destinations[key]
                parts.append((min(y, x + max(0, start - a)), min(y, x + min(b - a, end - a))))
        if not parts:
            return point(start), point(start)
        return min(p[0] for p in parts), new_end + end - old_end if end > old_end else max(
            p[1] for p in parts
        )

    from app.services.kria_editor_ops import copy_source_occurrence

    speech = any(
        copy_source_occurrence(r).get("speaker_subject") is True for r in state.initial_slots
    )
    for lane, original in before.items():
        if lane == "captions" and speech:
            continue
        if original is None:
            continue
        current = getattr(state, lane)
        by_id = {row.get("id"): row for row in current or []}
        for index, source in enumerate(original):
            if lane == "text" and (
                text_timing != "follow" or str(source.get("id") or "") in explicit_text_ids
            ):
                continue
            row = (
                by_id.get(source.get("id"))
                if source.get("id") is not None
                else (current[index] if current and index < len(current) else None)
            )
            if lane != "text":
                prior_rows = initial.get(lane) or []
                prior = (
                    next((r for r in prior_rows if r.get("id") == source.get("id")), None)
                    if source.get("id") is not None
                    else (prior_rows[index] if index < len(prior_rows) else None)
                )
                timing_keys = ("at_s", "start_s", "end_s", "start", "end")
                if prior is None or any(prior.get(key) != source.get(key) for key in timing_keys):
                    if row is None:
                        row = copy.deepcopy(source)
                        current.append(row)
                    for key in timing_keys:
                        if key in source:
                            row[key] = source[key]
                    continue
            if row is None:
                continue
            if (source.get("source_params") or {}).get("segment_id"):
                continue
            if any(
                source.get(key)
                for key in ("segment_id", "source_segment_id", "timeline_clip_id", "slot_id")
            ):
                continue
            if "at_s" in source:
                row["at_s"] = round(point(float(source["at_s"])), 6)
            else:
                sk = "start_s" if "start_s" in source else "start"
                ek = "end_s" if "end_s" in source else "end"
                if sk not in source or ek not in source:
                    continue
                row[sk], row[ek] = (round(t, 6) for t in span(float(source[sk]), float(source[ek])))
            if lane == "text" and not source.get("segment_id"):
                row.pop("segment_id", None)
            if lane == "captions":
                for word in row.get("words") or []:
                    if "start_s" in word and "end_s" in word:
                        word["start_s"], word["end_s"] = (
                            round(t, 6) for t in span(float(word["start_s"]), float(word["end_s"]))
                        )
            state.changed.add(lane)
    for lane in ("captions", "drawings", "media_overlays", "visual_blocks", "camera_effects"):
        rows = getattr(state, lane)
        if rows is not None and lane in state.changed:
            rows[:] = [
                row
                for row in rows
                if not ("start_s" in row and "end_s" in row and row["end_s"] <= row["start_s"])
            ]
    if state.speaker_background is not None:
        for key in ("windows", "full_screen_windows"):
            if state.speaker_background.get(key) != (initial.get("speaker_background") or {}).get(
                key
            ):
                continue
            for window in state.speaker_background.get(key) or []:
                window["start_s"], window["end_s"] = (
                    round(t, 6) for t in span(float(window["start_s"]), float(window["end_s"]))
                )
            if state.speaker_background.get(key):
                state.speaker_background[key] = [
                    window
                    for window in state.speaker_background[key]
                    if window["end_s"] - window["start_s"] >= 0.5
                ]
                state.speaker_background[key].sort(key=lambda window: window["start_s"])
                if not state.speaker_background[key]:
                    state.speaker_background.pop(key)
                state.changed.add("speaker_background")


def register_handlers() -> None:
    from app.services.kria_editor_ops import register_handler

    for name in _MODELS:
        register_handler(name, _apply)


def _keep_output_window(state: Any, start: float, end: float) -> None:
    from app.kria.clip_source_clock import slice_source_window
    from app.services.kria_editor_ops import KriaEditorOpError

    starts, sections, total = layout_split_timeline(state.slots, state.split_screen_sections)
    if start < 0 or end > total + 1e-6 or end - start < 0.1:
        raise KriaEditorOpError("The output trim must leave at least 0.1 seconds")
    windows = {s.id: (max(start, s.start), min(end, s.end)) for s in sections}
    for row, row_start in zip(
        (r for r in state.slots if not r.get("removed")), starts, strict=True
    ):
        row_end = row_start + float(row["duration_s"])
        left, right = max(start, row_start), min(end, row_end)
        if right - left < 0.1 - 1e-6:
            row["removed"] = True
            continue
        row.update(slice_source_window(row, left - row_start, right - row_start))
        if row.get("split_section_id"):
            row["split_offset_s"] = left - windows[row["split_section_id"]][0]
    state.split_screen_sections[:] = [
        s for s in state.split_screen_sections if windows[s["id"]][1] - windows[s["id"]][0] >= 0.1
    ]
    for section in state.split_screen_sections:
        low, high = windows[section["id"]]
        section["duration_s"] = high - low
        if not any(
            not r.get("removed") and r.get("split_section_id") == section["id"] for r in state.slots
        ):
            raise KriaEditorOpError(
                "This trim leaves only an empty section background with no media occurrence"
            )
    validate_state(state)
    state.changed.add("timeline")


def trim_output_start(state: Any, amount: float) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError

    if amount <= 0:
        raise KriaEditorOpError("The requested output trim has no effect")
    total = layout_split_timeline(state.slots, state.split_screen_sections)[2]
    _keep_output_window(state, amount, total)


def retarget_output(state: Any, target: float, strategy: str) -> None:
    from app.kria.clip_source_clock import resize_source_window
    from app.services.kria_editor_ops import KriaEditorOpError

    total = layout_split_timeline(state.slots, state.split_screen_sections)[2]
    if strategy == "trim_tail" and target < total:
        _keep_output_window(state, 0, target)
        return
    sections = {s["id"]: s for s in state.split_screen_sections}
    units = []
    seen = set()
    for row in state.slots:
        if row.get("removed"):
            continue
        section_id = row.get("split_section_id")
        if section_id:
            if section_id not in seen:
                units.append(
                    (
                        sections[section_id],
                        [
                            r
                            for r in state.slots
                            if not r.get("removed") and r.get("split_section_id") == section_id
                        ],
                    )
                )
                seen.add(section_id)
        else:
            units.append((None, [row]))
    lengths = [
        float(section["duration_s"] if section else rows[0]["duration_s"])
        for section, rows in units
    ]
    original = copy.deepcopy(units)
    overlap = sum(lengths) - total
    desired = [
        (
            (target + overlap) / len(units)
            if strategy == "uniform"
            else (target + overlap) * length / sum(lengths)
        )
        for length in lengths
    ]
    if strategy == "trim_tail":
        desired = lengths[:]
        desired[-1] += target - total
    if min(desired) < 0.1:
        raise KriaEditorOpError(
            "The requested length leaves a timeline unit shorter than 0.1 seconds"
        )
    for _ in range(6):
        for index, ((section, rows), (old_section, old_rows)) in enumerate(
            zip(units, original, strict=True)
        ):
            duration = desired[index]
            factor = duration / lengths[index]
            if section:
                section["duration_s"] = duration
                for row, old in zip(rows, old_rows, strict=True):
                    offset = float(old["split_offset_s"]) * factor
                    wanted = min(float(old["duration_s"]) * factor, duration - offset)
                    if wanted < 0.1 - 1e-6:
                        raise KriaEditorOpError(
                            "The requested length leaves a panel clip shorter than 0.1 seconds"
                        )
                    row.update(resize_source_window(old, wanted))
                    row["split_offset_s"] = offset
            else:
                rows[0].update(resize_source_window(old_rows[0], duration))
        fit_panel_coverage(state)
        reached = layout_split_timeline(state.slots, state.split_screen_sections)[2]
        if abs(reached - target) <= 1e-6:
            state.changed.add("timeline")
            return
        adjustable = [i for i, (section, _) in enumerate(units) if section]
        if not adjustable:
            break
        index = adjustable[-1]
        desired[index] += target - reached
        if desired[index] < 0.1:
            break
    raise KriaEditorOpError("The requested duration cannot fit the available source footage")


def fit_panel_coverage(state: Any) -> None:
    from app.kria.clip_source_clock import slice_source_window
    from app.kria.split_screen import transition_overlap

    sections = {section["id"]: section for section in state.split_screen_sections}
    for row in state.slots:
        section = sections.get(row.get("split_section_id"))
        if section is None or row.get("removed"):
            continue
        offset = float(row["split_offset_s"])
        room = float(section["duration_s"]) - offset
        following = [
            r
            for r in state.slots
            if not r.get("removed")
            and r.get("split_section_id") == section["id"]
            and r.get("split_panel_index") == row["split_panel_index"]
            and float(r.get("split_offset_s", 0)) > offset
        ]
        if following:
            next_row = min(following, key=lambda r: r["split_offset_s"])
            room = min(
                room, next_row["split_offset_s"] - offset + transition_overlap(row, next_row)
            )
        if float(row["duration_s"]) > room + 1e-6:
            row.update(slice_source_window(row, 0, room))
