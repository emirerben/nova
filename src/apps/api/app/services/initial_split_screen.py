"""Lower a structured creation request through the ordinary editor section command."""

from __future__ import annotations

import copy
from typing import Any
from uuid import uuid4

from app.agents._schemas.split_screen import InitialSplitScreen, SplitPanelClip
from app.agents.edit_copilot import EditorOperationParseState, parse_editor_operation
from app.kria.split_screen import layout_split_timeline
from app.services import kria_editor_ops as editor


def _clip_widths(clips: list[SplitPanelClip], section: float) -> list[float]:
    """Each clip's wanted length: its own, else an equal share of what the others leave."""
    fixed = sum(c.duration_s or 0.0 for c in clips)
    open_ended = sum(c.duration_s is None for c in clips)
    share = (section - fixed) / open_ended if open_ended else 0.0
    if open_ended and share < 0.1:
        raise editor.KriaEditorOpError(
            "The panel's clips leave no time for the ones without a length"
        )
    return [share if c.duration_s is None else c.duration_s for c in clips]


def apply_initial_split_screen(
    job: Any,
    variant: dict,
    request: InitialSplitScreen | dict,
    *,
    source_rows: list[dict] | None = None,
) -> dict:
    request = InitialSplitScreen.model_validate(request)
    if not editor.split_screen_available(job, variant):
        raise editor.KriaEditorOpError("Split screen is unavailable for this edit")
    if editor._variant_split_sections(variant):
        raise editor.KriaEditorOpError("Use section editing for an existing split screen")
    ordinary = editor._variant_slots(variant, job)
    sources = source_rows if source_rows is not None else ordinary
    by_media: dict[str, dict] = {}
    for row in sources:
        media_id = str(row.get("media_id") or "")
        if not media_id:
            continue
        if media_id in by_media and by_media[media_id]["clip_index"] != row["clip_index"]:
            raise editor.KriaEditorOpError("An approved source has ambiguous identity")
        by_media.setdefault(media_id, row)
    _, total = editor._split_timeline_segments(ordinary, [])
    longest_explicit = max(
        sum(clip.duration_s or 0.0 for clip in panel.sequence) for panel in request.panels
    )
    duration = request.duration_s or max(total, longest_explicit)
    if duration < 0.1:
        raise editor.KriaEditorOpError("The split screen needs a positive duration")
    from app.kria.clip_source_clock import resize_source_window, set_source_start

    members = []
    later_clips: list[tuple[int, float, dict]] = []
    for panel_index, panel in enumerate(request.panels):
        clips = panel.sequence
        cursor = 0.0
        for position, (clip, width) in enumerate(
            zip(clips, _clip_widths(clips, duration), strict=True)
        ):
            source = by_media.get(clip.media_id)
            if source is None:
                raise editor.KriaEditorOpError(
                    "A requested panel source is not in the approved media"
                )
            if duration - cursor < 0.1:
                raise editor.KriaEditorOpError("The panel's clips run past the end of the section")
            row = editor.copy_source_occurrence(source)
            row["slot_id"] = str(uuid4())
            if clip.source_start_s is not None:
                if row.get("freeze_hold_s"):
                    row = set_source_start(row, clip.source_start_s)
                else:
                    row["in_s"] = clip.source_start_s
                    if "source_end_s" in row:
                        row["source_end_s"] = row["in_s"] + editor._slot_duration(
                            row
                        ) * editor._slot_rate(row)
            row = resize_source_window(row, min(width, duration - cursor))
            row["removed"] = False
            if position < len(clips) - 1:
                row["transition_after"] = "cut"
                row.pop("transition_duration_s", None)
            if position == 0:
                members.append(row)
            else:
                later_clips.append((panel_index, cursor, row))
            cursor += editor._slot_duration(row)
    before, after = [], []
    if request.start_s is not None:
        start, end = request.start_s, request.start_s + duration
        if end > total + 1e-6:
            raise editor.KriaEditorOpError("The requested section is outside the planned output")
        segments, _ = editor._split_timeline_segments(ordinary, [])
        for row, segment in zip(
            (r for r in ordinary if not r.get("removed")), segments, strict=True
        ):
            left, right = segment["output_start_s"], segment["output_end_s"]
            for low, high, destination in (
                (left, min(right, start), before),
                (max(left, end), right, after),
            ):
                if high - low < 0.1:
                    continue
                from app.kria.clip_source_clock import slice_source_window

                kept = slice_source_window(
                    editor.copy_source_occurrence(row), low - left, high - left
                )
                kept["slot_id"] = str(uuid4())
                destination.append(kept)
    staged = copy.deepcopy(variant)
    staged.pop("guided_draft_slots", None)
    staged["user_timeline"] = {"slots": before + members + after}
    staged["editor_timeline_mode"] = "authored"
    snapshot = editor.build_editor_snapshot(job, staged)
    snapshot["editor_ops_version"] = 2
    raw = {
        "op": "create_split_screen",
        "slot_indexes": list(range(len(before), len(before) + len(members))),
        "panel_count": len(members),
        "duration_s": duration,
        "gutter": request.gutter,
        "background_color": request.background_color,
    }
    if request.panel_weights is not None:
        raw["panel_weights"] = request.panel_weights
    parsed = parse_editor_operation(raw, snapshot, EditorOperationParseState(1.0))
    if parsed is None:
        raise editor.KriaEditorOpError("The requested split screen cannot be applied")
    compiled = editor.compile_editor_ops(job, staged, [parsed])
    payload = compiled.payload.model_dump(mode="json", exclude_none=True)
    result = editor.project_editor_draft(staged, payload, job)
    if later_clips:
        _append_panel_clips(result["user_timeline"], later_clips)
    result["duration_s"] = editor._split_timeline_segments(
        result["user_timeline"]["slots"], result["user_timeline"]["split_screen_sections"]
    )[1]
    return result


def _append_panel_clips(timeline: dict, later_clips: list[tuple[int, float, dict]]) -> None:
    """Join each panel's second and later clips to the section the section command made.

    `create_split_screen` places one source per panel, so the rest are laid back to back after it
    and `layout_split_timeline` is the check that the whole section still lays out.
    """
    slots = timeline["slots"]
    section_id = timeline["split_screen_sections"][0]["id"]
    for panel, offset, row in later_clips:
        row.update(split_section_id=section_id, split_panel_index=panel, split_offset_s=offset)
        last = max(
            i
            for i, member in enumerate(slots)
            if member.get("split_section_id") == section_id
            and member.get("split_panel_index") == panel
        )
        slots.insert(last + 1, row)
    try:
        layout_split_timeline(slots, timeline["split_screen_sections"])
    except ValueError as exc:
        raise editor.KriaEditorOpError(str(exc)) from exc
