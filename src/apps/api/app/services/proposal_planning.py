"""One planning boundary for semantic drafts and explicit legacy compatibility."""

from __future__ import annotations

from dataclasses import asdict

from app.agents._runtime import RunContext, TerminalError
from app.agents.edit_proposal import (
    DraftStoryBeat,
    EditProposalAgent,
    EditProposalAgentInput,
    EditProposalAgentOutput,
    EditProposalMedia,
)
from app.config import settings
from app.schemas.edit_frame_schedule import EditFrameSchedule, FrameScheduledMoment
from app.schemas.edit_proposal import EditProposalSnapshot


class SemanticPlanningError(ValueError):
    """Actionable failure that must never enter the request-blind fallback."""

    def __init__(self, code: str, reason: str, diagnostics: dict) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.diagnostics = diagnostics


def plan_edit_proposal(
    input: EditProposalAgentInput,
    *,
    ctx: RunContext | None = None,
    force_semantic: bool = False,
) -> EditProposalAgentOutput:
    """Assess capacity before any paid call, then compile editorial intent once."""

    from app.agents._model_client import default_client

    if not (force_semantic or settings.edit_proposal_semantic_enabled):
        return EditProposalAgent(default_client()).run(input, ctx=ctx)

    from app.agents.semantic_edit_proposal import SemanticEditProposalAgent
    from app.services.semantic_edit_scheduler import (
        FeasibilityError,
        assess_semantic_feasibility,
        schedule_semantic_edit,
    )

    feasibility = assess_semantic_feasibility(input)
    diagnostics = {
        "direction": input.direction,
        "prompt_version": SemanticEditProposalAgent.spec.prompt_version,
        "scheduler_version": 1,
        "compiler_version": 8,
        "feasibility": asdict(feasibility),
        "outcome": "infeasible" if feasibility.status == "infeasible" else "planning",
        "fallback_reason": None,
    }
    if feasibility.status == "infeasible":
        raise SemanticPlanningError("semantic_edit_infeasible", feasibility.reason, diagnostics)
    # Retain the requested target in input: both preflight and allocation use
    # the same pure scheduler and report the exact same clamp, if any.
    try:
        semantic = SemanticEditProposalAgent(default_client()).run(input, ctx=ctx)
    except TerminalError as exc:
        diagnostics.update(outcome="semantic_rejected", fallback_reason=str(exc)[:500])
        raise SemanticPlanningError(
            "semantic_edit_planning_failed",
            "Kria couldn't preserve every requested part of this edit. "
            "Retry or revise the request.",
            diagnostics,
        ) from exc
    diagnostics["semantic_plan"] = semantic.model_dump(mode="json")
    try:
        result = schedule_semantic_edit(semantic, input)
    except FeasibilityError as exc:
        diagnostics.update(
            outcome="schedule_rejected",
            feasibility=asdict(exc.report),
            fallback_reason=exc.reason,
        )
        raise SemanticPlanningError("semantic_edit_infeasible", exc.reason, diagnostics) from exc
    diagnostics.update(
        outcome="compiled",
        schedule=result.schedule.model_dump(mode="json"),
        repairs=[*getattr(semantic, "repairs", []), *result.repairs],
        feasibility=asdict(result.feasibility),
    )
    return EditProposalAgentOutput(
        title=input.opening_title or semantic.title or "A few moments",
        duration_s=result.duration_s,
        story_beats=[DraftStoryBeat(**beat.model_dump()) for beat in result.story_beats],
        fast_cuts=result.fast_cuts,
        montage_text_bindings=result.montage_text_bindings,
        montage_audio=input.montage_audio or semantic.montage_audio,
        mixed_media_timing=input.mixed_media_timing,
        repairs=diagnostics["repairs"][:64],
        frame_schedule=result.schedule,
        semantic_plan=diagnostics["semantic_plan"],
        planning_diagnostics=diagnostics,
        scheduled_story_beats=result.story_beats,
    )


def snapshot_agent_input(
    snapshot: EditProposalSnapshot, *, creator_request: str = ""
) -> EditProposalAgentInput:
    """Project only server-owned media and the already-approved constraints."""

    return EditProposalAgentInput(
        direction=snapshot.direction,
        goal=snapshot.goal,
        pace=snapshot.pace,
        creator_request=creator_request,
        target_duration_s=snapshot.duration_s,
        media_scope=snapshot.media_scope,
        selected_media_ids=snapshot.selected_media_ids,
        video_reuse_policy=snapshot.video_reuse_policy or "once",
        mixed_media_timing=snapshot.mixed_media_timing,
        montage_cadence=snapshot.montage_cadence,
        montage_audio=snapshot.montage_audio,
        opening_title=snapshot.opening_title,
        opening_title_duration_s=snapshot.opening_title_duration_s,
        shot_labels=snapshot.shot_labels,
        closing_title=snapshot.closing_title,
        narration_duration_s=snapshot.narration.duration_s if snapshot.narration else None,
        narration_words=[word.model_dump() for word in snapshot.narration.words]
        if snapshot.narration
        else [],
        clip_intents=snapshot.clip_intents,
        media=[
            EditProposalMedia(
                media_id=ref.media_id,
                kind=ref.kind,
                lane=ref.lane,
                duration_s=ref.duration_s,
                source_filename=ref.source_filename,
                user_context=ref.user_context,
                subject=str(ref.analysis.get("subject") or ""),
                description=str(ref.analysis.get("description") or ""),
                on_screen_text=str(ref.analysis.get("on_screen_text") or ""),
                best_moments=list(ref.analysis.get("best_moments") or []),
            )
            for ref in snapshot.media
        ],
    )


def _timing_contract(snapshot: EditProposalSnapshot) -> dict:
    data = snapshot.model_dump(mode="json")
    for field in (
        "frame_schedule",
        "title",
        "opening_title",
        "opening_title_duration_s",
        "closing_title",
        "shot_labels",
        "font_family",
        "text_color",
        "licensed_sfx",
        "montage_text_bindings",
        "montage_audio",
        "goal",
        "output_orientation",
        "output_orientation_reason",
    ):
        data.pop(field, None)
    for beat in data["story_beats"]:
        for field in ("topic", "thought", "thought_source"):
            beat.pop(field, None)
    return data


def refresh_snapshot_schedule(
    snapshot: EditProposalSnapshot,
    *,
    previous: EditProposalSnapshot | None = None,
    creator_request: str = "",
) -> EditProposalSnapshot:
    """Regenerate a correction's schedule; never trust the client's schedule.

    Text-only edits preserve approved source windows. Human fast-cut edits
    already specify windows, so quantize and validate those instead of asking
    an agent or moving the selected footage. Story edits reallocate semantic
    chapters with the same scheduler used for initial drafting.
    """

    from app.pipeline.guided_story import validate_frame_schedule

    if (
        previous
        and previous.frame_schedule
        and _timing_contract(snapshot) == _timing_contract(previous)
    ):
        result = snapshot.model_copy(update={"frame_schedule": previous.frame_schedule})
        validate_frame_schedule(result)
        return result
    if snapshot.direction == "fast_montage" and snapshot.fast_cuts:
        cursor = 0
        moments = []
        cuts = []
        by_id = {ref.media_id: ref for ref in snapshot.media}
        for cut in snapshot.fast_cuts:
            start, end = round(cut.source_start_s * 30), round(cut.source_end_s * 30)
            duration = end - start
            ref = by_id[cut.media_id]
            moments.append(
                FrameScheduledMoment(
                    moment_id=cut.cut_id,
                    beat_id=cut.cut_id,
                    media_id=cut.media_id,
                    source_start_frame=start,
                    source_end_frame=end,
                    output_start_frame=cursor,
                    output_end_frame=cursor + duration,
                    layout=snapshot.image_layout
                    if ref.kind == "image" and snapshot.image_layout
                    else "fullscreen",
                    role=cut.role,
                )
            )
            cuts.append(
                cut.model_copy(
                    update={
                        "source_start_s": start / 30,
                        "source_end_s": end / 30,
                        "output_duration_s": duration / 30,
                        "beat_align": False,
                    }
                )
            )
            cursor += duration
        schedule = EditFrameSchedule(
            total_frames=cursor, transition_frames=0, direction=snapshot.direction, moments=moments
        )
        result = EditProposalSnapshot.model_validate(
            {
                **snapshot.model_dump(mode="json"),
                "frame_schedule": schedule.model_dump(),
                "fast_cuts": [cut.model_dump() for cut in cuts],
            }
        )
    else:
        from app.schemas.semantic_edit import (
            SemanticChapter,
            SemanticEditPlan,
            SemanticSource,
            SemanticTextBinding,
        )
        from app.services.semantic_edit_scheduler import schedule_semantic_edit

        input = snapshot_agent_input(snapshot, creator_request=creator_request)
        # Persisted beats already carry the creator's exact copy and stable
        # identity. A semantic chapter may have been split into several beats,
        # or one source expanded into repeats, so initial-agent label-count
        # validation is not a valid contract for a human revision.
        semantic = SemanticEditPlan(
            title=snapshot.title,
            chapters=[
                SemanticChapter(
                    chapter_id=beat.beat_id,
                    topic=beat.topic,
                    thought=beat.thought,
                    role="hook"
                    if index == 0
                    else "payoff"
                    if index == len(snapshot.story_beats) - 1
                    else "build",
                    weight=beat.duration_s,
                    layout=beat.layout,
                    sources=[
                        SemanticSource(media_id=media_id, weight=beat.media_ids.count(media_id))
                        for media_id in dict.fromkeys(beat.media_ids)
                    ],
                )
                for index, beat in enumerate(snapshot.story_beats)
            ],
            montage_audio=snapshot.montage_audio,
            text_bindings=[
                SemanticTextBinding(text=binding.text, media_ids=[binding.media_id])
                for binding in snapshot.montage_text_bindings
            ],
        )
        scheduled = schedule_semantic_edit(semantic, input)
        if abs(scheduled.duration_s - float(snapshot.duration_s)) > 0.000001:
            raise ValueError(
                "The revised chapters cannot fill the approved length. "
                "Change the target or add media."
            )
        beats = scheduled.story_beats
        sources = {beat.beat_id: beat.thought_source for beat in snapshot.story_beats}
        for beat in beats:
            beat.thought_source = sources.get(beat.beat_id, "user")
        result = EditProposalSnapshot.model_validate(
            {
                **snapshot.model_dump(mode="json"),
                "frame_schedule": scheduled.schedule.model_dump(),
                "story_beats": [beat.model_dump() for beat in beats],
            }
        )
    validate_frame_schedule(result)
    return result
