"""Build the chat copilot's phone snapshot for an authored thread, through the real builder.

The authored briefs carry footage facts and a hand-built reference edit, not a snapshot. To ask
a real model about them we need what the iPhone chat would send, so the snapshot comes from
``build_editor_snapshot`` over a synthetic device variant, never from a hand-written dict.
Fixtures store the result: replay never rebuilds it, so a change to the builder does not move a
recorded score until the thread is re-recorded on purpose.
"""

from __future__ import annotations

import types
import uuid
from typing import Any

from .models import FinalPlan, Footage, PlanText

SEED_TEXT = {
    "font_family": "Inter",
    "size_px": 72,
    "color": "#FFFFFF",
    "effect": "static",
    "alignment": "center",
    "position": "middle",
}


JOB_ID = uuid.UUID(int=1)
# The editor opens clip operations only for sources under the job's durable prefix.
SOURCE_PREFIX = f"generative-jobs/{JOB_ID}/sources/"


def seed_plan(footage: Footage) -> FinalPlan:
    """Every clip once, in attachment order, no text: the edit a first render hands the creator."""
    from .models import PlanClip

    clips: list[PlanClip] = []
    cursor = 0.0
    for clip in footage.clips:
        clips.append(PlanClip(clip_id=clip.clip_id, start_s=cursor, end_s=cursor + clip.duration_s))
        cursor += clip.duration_s
    return FinalPlan(clips=clips, total_duration_s=cursor)


def _text_row(text: PlanText) -> dict[str, Any]:
    row: dict[str, Any] = {
        **SEED_TEXT,
        "id": text.id,
        "text": text.text,
        "start_s": text.start_s,
        "end_s": text.end_s,
        "role": "generative_intro",
    }
    for key in ("font_family", "position", "alignment", "x_frac", "y_frac"):
        value = getattr(text, key)
        if value is not None:
            row[key] = value
    return row


def variant_for(footage: Footage, plan: FinalPlan) -> dict[str, Any]:
    by_id = {c.clip_id: c for c in footage.clips}
    index_of = {c.clip_id: i for i, c in enumerate(footage.clips)}
    slots = []
    for index, clip in enumerate(plan.clips):
        record = by_id[clip.clip_id]
        slots.append(
            {
                "slot_id": f"slot-{index + 1}",
                "clip_index": index_of[clip.clip_id],
                "media_id": clip.clip_id,
                "source_gcs_path": f"{SOURCE_PREFIX}{clip.clip_id}.mp4",
                "source_duration_s": record.duration_s,
                "in_s": clip.source_start_s or 0.0,
                "duration_beats": None,
                "duration_s": clip.duration_s,
                "output_start_s": clip.start_s,
                "output_end_s": clip.end_s,
                "removed": False,
                "transition_after": "cut",
                "media_kind": "video",
            }
        )
    return {
        "variant_id": "original_text",
        "render_status": "ready",
        "render_generation_id": "gen-1",
        "render_destination": "device",
        "resolved_archetype": "montage",
        "text_elements": [_text_row(t) for t in plan.texts],
        "ai_timeline": {"beat_grid": [], "slots": slots},
    }


def job_for(footage: Footage, variant: dict[str, Any]) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=JOB_ID,
        user_id=1,
        status="variants_ready",
        mode="generative",
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": [f"{SOURCE_PREFIX}{c.clip_id}.mp4" for c in footage.clips]},
    )


def clip_context_for(footage: Footage) -> dict[str, Any]:
    """The clip facts and the one-line "what the clip shows" the chat path hands the copilot."""
    return {
        "facts": {
            c.clip_id: [
                {"kind": f.kind, "value": f.value, "provenance": f.provenance} for f in c.facts
            ]
            for c in footage.clips
            if c.facts
        },
        "seen": {c.clip_id: c.subject for c in footage.clips if c.subject},
    }


def phone_snapshot(footage: Footage, plan: FinalPlan) -> dict[str, Any]:
    from app.services.kria_editor_ops import build_editor_snapshot

    variant = variant_for(footage, plan)
    snapshot = build_editor_snapshot(
        job_for(footage, variant), variant, clip_context=clip_context_for(footage)
    )
    # The pool names sources by upload index; the slots and the scorer use the footage ids.
    ids = [c.clip_id for c in footage.clips]
    for key in ("source_pool", "replacement_source_pool"):
        for row in snapshot.get(key) or []:
            row["media_id"] = ids[row["clip_index"]]
    return snapshot
