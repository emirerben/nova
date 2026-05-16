"""Agent-driven clip→slot matching for agentic templates.

`agentic_match()` calls the `clip_router` agent to assign user clips to
template slots. For each assignment, it picks the clip's top-scoring
best_moment to populate the AssemblyStep. This replaces the greedy
heuristic in `template_matcher.match()` for templates with is_agentic=True.

`agentic_match_or_fallback()` wraps it with a fall-back to the greedy
matcher when the agent fails — a single flaky LLM call shouldn't take
down a user's job.

shot_ranker is intentionally NOT used here in v1; clip_router needs only
one ranked signal per clip (hook_score), and adding a second LLM hop for
moment-level ranking is more cost/latency than it earns for templates
where each clip has 1-3 best_moments anyway. Layer shot_ranker in once
we see clip_router make consistently good slot assignments.
"""

from __future__ import annotations

import structlog

from app.pipeline.agents.gemini_analyzer import (
    AssemblyPlan,
    AssemblyStep,
    ClipMeta,
    TemplateRecipe,
)
from app.pipeline.template_matcher import match

log = structlog.get_logger()


def _top_moment(meta: ClipMeta) -> dict | None:
    """Return the highest-energy best_moment from a clip, or None."""
    moments = meta.best_moments or []
    if not moments:
        return None
    return max(moments, key=lambda m: float(m.get("energy", 0.0)))


def _candidate_clips_payload(clip_metas: list[ClipMeta]) -> list[dict]:
    """Build clip_router CandidateClip inputs from analyzed user clips."""
    candidates: list[dict] = []
    for meta in clip_metas:
        if meta.failed or not meta.best_moments:
            continue
        top = _top_moment(meta)
        if top is None:
            continue
        candidates.append(
            {
                "id": meta.clip_id,
                "duration_s": float(top["end_s"]) - float(top["start_s"]),
                "energy": float(top.get("energy", 0.0)),
                "hook_score": float(meta.hook_score or 0.0),
                "description": (str(top.get("description", "")) or meta.hook_text or "")[
                    :240
                ],  # clip_router caps descriptions; keep payload small
            }
        )
    return candidates


def _slot_specs_payload(slots: list[dict]) -> list[dict]:
    """Build clip_router SlotSpec inputs from a consolidated recipe."""
    return [
        {
            "position": int(slot.get("position", i)),
            "target_duration_s": float(slot.get("target_duration_s", 3.0)),
            "slot_type": str(slot.get("slot_type", "broll")),
            "energy": float(slot.get("energy", 0.0)),
        }
        for i, slot in enumerate(slots)
    ]


def agentic_match(
    recipe: TemplateRecipe,
    clip_metas: list[ClipMeta],
    job_id: str | None = None,
) -> AssemblyPlan:
    """Build an AssemblyPlan using clip_router instead of the greedy matcher.

    Raises any exception the agent or input shaping raises — the caller is
    expected to wrap with a fallback when fault tolerance is desired (see
    `agentic_match_or_fallback`).
    """
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import RunContext  # noqa: PLC0415
    from app.agents.clip_router import (  # noqa: PLC0415
        ClipRouterAgent,
        ClipRouterInput,
    )

    candidates = _candidate_clips_payload(clip_metas)
    slot_specs = _slot_specs_payload(recipe.slots)

    if not candidates:
        raise ValueError(
            "agentic_match: no usable candidates (all clips failed analysis "
            "or lack best_moments). Caller should fall back to greedy match."
        )

    # Phase 4 perf: content-hash the ClipRouterInput and check Redis before
    # the LLM call. Same-clips + same-recipe test-job reruns (e.g. operator
    # iterating in the Test tab without changing clips or the recipe) skip the
    # ~3-5s LLM hop. Cache falls open on any error so behavior on miss is
    # identical to today.
    from app.pipeline.clip_router_cache import (  # noqa: PLC0415
        get_cached_assignments,
        set_cached_assignments,
    )

    router_input = ClipRouterInput(
        slots=slot_specs,
        candidates=candidates,
        copy_tone=getattr(recipe, "copy_tone", "") or "",
        creative_direction=getattr(recipe, "creative_direction", "") or "",
    )

    assignments = get_cached_assignments(router_input)
    if assignments is not None:
        log.info(
            "agentic_match_clip_router_cache_hit",
            job_id=job_id,
            assignment_count=len(assignments),
            slot_count=len(slot_specs),
        )
    else:
        agent = ClipRouterAgent(default_client())
        out = agent.run(router_input, ctx=RunContext(job_id=job_id))
        assignments = out.assignments
        set_cached_assignments(router_input, assignments)

    # Index clip_metas by id for O(1) lookup; same for slots.
    meta_by_id = {m.clip_id: m for m in clip_metas}
    slot_by_pos = {int(s.get("position", i)): s for i, s in enumerate(recipe.slots)}

    steps: list[AssemblyStep] = []
    used_clip_ids: set[str] = set()
    for assignment in assignments:
        slot = slot_by_pos.get(int(assignment.slot_position))
        meta = meta_by_id.get(str(assignment.candidate_id))
        if slot is None or meta is None:
            log.warning(
                "agentic_match_invalid_assignment",
                slot_position=assignment.slot_position,
                candidate_id=assignment.candidate_id,
                known_slots=list(slot_by_pos.keys()),
                known_clips=list(meta_by_id.keys()),
            )
            continue
        moment = _top_moment(meta)
        if moment is None:
            continue
        steps.append(AssemblyStep(slot=slot, clip_id=meta.clip_id, moment=moment))
        used_clip_ids.add(meta.clip_id)

    # Ensure every slot gets covered. If clip_router under-assigned (e.g.
    # missed a slot), fall back to greedy for the remainder so the user
    # always gets a complete video. Greedy matches against the missing
    # slots only.
    assigned_positions = {int(s.slot.get("position", -1)) for s in steps}
    missing = [s for s in recipe.slots if int(s.get("position", -1)) not in assigned_positions]
    if missing:
        log.warning(
            "agentic_match_partial_coverage",
            assigned=len(steps),
            missing=len(missing),
        )
        # Build a recipe shim covering only the missing slots, run greedy.
        from dataclasses import replace  # noqa: PLC0415

        gap_recipe = replace(recipe, slots=missing)
        gap_plan = match(gap_recipe, clip_metas)
        steps.extend(gap_plan.steps)

    # Final ordering: slot.position ascending (temporal order for concat).
    steps.sort(key=lambda s: int(s.slot.get("position", 0)))
    return AssemblyPlan(steps=steps)


def agentic_match_or_fallback(
    recipe: TemplateRecipe,
    clip_metas: list[ClipMeta],
    pinned_assignments: dict[int, str] | None = None,
    filter_hint: str = "",
    job_id: str | None = None,
) -> AssemblyPlan:
    """Run agentic_match; on agent failure, fall back to greedy match.

    A single flaky LLM call on the matching step shouldn't fail the user's
    whole job — and the greedy matcher's output is acceptable. We log the
    fallback loudly so evals can spot agent regressions.
    """
    try:
        return agentic_match(recipe, clip_metas, job_id=job_id)
    except Exception as exc:
        log.warning(
            "agentic_match_fell_back_to_greedy",
            job_id=job_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return match(
            recipe,
            clip_metas,
            pinned_assignments=pinned_assignments,
            filter_hint=filter_hint,
        )
