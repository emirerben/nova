"""Lower a structured creation request through the ordinary editor section command."""

from __future__ import annotations

import copy
from typing import Any
from uuid import uuid4

from app.agents._schemas.split_screen import InitialSplitScreen
from app.agents.edit_copilot import EditorOperationParseState, parse_editor_operation
from app.services import kria_editor_ops as editor


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
    duration = request.duration_s or total
    if duration < 0.1:
        raise editor.KriaEditorOpError("The split screen needs a positive duration")
    members = []
    for panel in request.panels:
        source = by_media.get(panel.media_id)
        if source is None:
            raise editor.KriaEditorOpError("A requested panel source is not in the approved media")
        from app.kria.clip_source_clock import resize_source_window, set_source_start

        row = copy.deepcopy(source)
        row["slot_id"] = str(uuid4())
        if panel.source_start_s is not None:
            if row.get("freeze_hold_s"):
                row = set_source_start(row, panel.source_start_s)
            else:
                row["in_s"] = panel.source_start_s
                if "source_end_s" in row:
                    row["source_end_s"] = row["in_s"] + editor._slot_duration(
                        row
                    ) * editor._slot_rate(row)
        row = resize_source_window(row, duration)
        row["removed"] = False
        members.append(row)
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

                kept = slice_source_window(row, low - left, high - left)
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
    result["duration_s"] = editor._split_timeline_segments(
        result["user_timeline"]["slots"], result["user_timeline"]["split_screen_sections"]
    )[1]
    return result
