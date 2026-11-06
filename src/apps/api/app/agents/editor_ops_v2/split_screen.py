"""AI commands for timeline-owned sections and ordinary clip membership."""

from __future__ import annotations

import copy
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.editor_ops_v2 import OpSpec
from app.kria.split_screen import SplitScreenSection, layout_split_timeline


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


class UpdateSection(SectionCommand):
    section_id: str
    duration_s: float | None = Field(default=None, ge=0.1, le=1800)
    panel_count: Literal[2, 3] | None = None
    panel_weights: list[float] | None = None
    gutter: float | None = Field(default=None, ge=0, le=0.1)
    background_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class AssignPanel(SectionCommand):
    section_id: str
    slot_index: int = Field(ge=0, strict=True)
    panel_index: int = Field(ge=0, le=2, strict=True)
    offset_s: float = Field(default=0, ge=0, le=1800)
    duplicate: bool = False


class SwapPanels(SectionCommand):
    section_id: str
    first_panel: int = Field(ge=0, le=2, strict=True)
    second_panel: int = Field(ge=0, le=2, strict=True)


class RemoveSection(SectionCommand):
    section_id: str
    keep_panel: int = Field(default=0, ge=0, le=2, strict=True)


_MODELS = {
    "create_split_screen": CreateSection,
    "update_split_screen": UpdateSection,
    "assign_split_panel": AssignPanel,
    "swap_split_panels": SwapPanels,
    "remove_split_screen": RemoveSection,
}
_MEMBERSHIP = ("split_section_id", "split_panel_index", "split_offset_s")


def _coerce(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
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
        if "panel_weights" in clean and (
            len(clean["panel_weights"]) not in (2, 3) or any(w <= 0 for w in clean["panel_weights"])
        ):
            raise ValueError("invalid weights")
        return clean
    except (ValidationError, ValueError, TypeError):
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

    if not editor.split_screen_available(state.job, state.variant):
        raise editor.KriaEditorOpError("Split screen is unavailable for this edit")
    name = op["op"]
    try:
        command = (
            _MODELS[name]
            .model_validate({k: v for k, v in op.items() if k != "op"})
            .model_dump(exclude_none=True)
        )
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
