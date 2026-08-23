"""Direction-specific replanning over an existing analyzed proposal snapshot."""

from __future__ import annotations

from app.agents._model_client import default_client
from app.agents._runtime import RunContext
from app.agents.edit_proposal import (
    EditProposalAgent,
    EditProposalAgentInput,
    EditProposalMedia,
)
from app.schemas.edit_proposal import EditProposalSnapshot, FastMontageCut, StoryBeat


def _compatibility_beats(cuts: list[FastMontageCut]) -> list[StoryBeat]:
    """Keep older readers functional while fast_cuts remain authoritative."""

    beats: list[StoryBeat] = []
    for index in range(0, len(cuts), 4):
        group = cuts[index : index + 4]
        media_ids = list(dict.fromkeys(cut.media_id for cut in group))
        beats.append(
            StoryBeat(
                beat_id=f"fast-beat-{index // 4 + 1}",
                topic="Fast montage",
                thought="",
                thought_source="ai_draft",
                media_ids=media_ids,
                layout="fullscreen",
                duration_s=max(1.0, min(12.0, sum(cut.output_duration_s for cut in group))),
            )
        )
    return beats


def plan_direction_snapshot(
    source: EditProposalSnapshot,
    *,
    direction: str,
    goal: str,
    pace: str,
    duration_s: int,
    idea: str = "",
    theme: str = "",
    job_id: str | None = None,
) -> EditProposalSnapshot:
    """Run the canonical proposal planner using the already-analyzed media.

    This deliberately accepts an immutable server snapshot rather than a
    Copilot/browser timeline. Direction replacement can therefore reuse exact
    media identities and analysis without trusting model-authored source cuts.
    """

    media = [
        EditProposalMedia(
            media_id=ref.media_id,
            lane=ref.lane,
            kind=ref.kind,
            source_filename=ref.source_filename,
            duration_s=ref.duration_s,
            user_context=ref.user_context,
            subject=str(ref.analysis.get("subject") or ""),
            description=str(ref.analysis.get("description") or ""),
            on_screen_text=str(ref.analysis.get("on_screen_text") or ""),
            best_moments=list(ref.analysis.get("best_moments") or []),
        )
        for ref in source.media
    ]
    output = EditProposalAgent(default_client()).run(
        EditProposalAgentInput(
            idea=idea[:500],
            theme=theme[:500],
            direction=direction,
            goal=goal[:500],
            pace=pace,
            target_duration_s=max(3, min(60, int(duration_s))),
            media=media,
        ),
        ctx=RunContext(job_id=job_id) if job_id else None,
    )
    cuts = output.fast_cuts if direction == "fast_montage" else None
    if direction == "fast_montage":
        if not cuts:
            raise ValueError("fast montage planner returned no source-aware cuts")
        beats = _compatibility_beats(cuts)
    else:
        beats = [
            StoryBeat(
                beat_id=f"beat-{index + 1}",
                topic=beat.topic,
                thought=beat.thought,
                thought_source="ai_draft",
                media_ids=beat.media_ids,
                layout=beat.layout,
                duration_s=beat.duration_s,
            )
            for index, beat in enumerate(output.story_beats)
        ]
    return source.model_copy(
        update={
            "direction": direction,
            "goal": goal,
            "pace": pace,
            "duration_s": output.duration_s,
            "title": output.title,
            "story_beats": beats,
            "fast_cuts": cuts,
        }
    )
