"""Template matcher — greedy algorithm with variety penalty.

match(recipe, clip_metas) → AssemblyPlan

Algorithm:
  1. Sort slots by priority (highest first) — greedy assigns best clips to
     most important slots first.
  2. For each slot, find all (clip, moment) pairs where the moment duration
     is within ±6s of the slot's target duration.
  3. Score each pair: moment.energy - 0.3 if same clip as previous slot.
  4. Pick the highest-scoring pair.
  5. CRITICAL: sort final plan by slot.position before returning (temporal
     order for FFmpeg concat — greedy builds in priority order).

Raises TemplateMismatchError when:
  - clip_metas is empty
  - No clip can satisfy a slot's duration requirement
"""


import structlog

from app.pipeline.agents.gemini_analyzer import AssemblyPlan, AssemblyStep, ClipMeta, TemplateRecipe

log = structlog.get_logger()

DURATION_TOLERANCE_S = 6.0  # ±6s from target_duration_s is acceptable
VARIETY_PENALTY = 0.3       # deducted when same clip fills consecutive slots


class TemplateMismatchError(Exception):
    """Raised when clips cannot satisfy the template's slot requirements."""

    def __init__(self, message: str, code: str = "TEMPLATE_CLIP_DURATION_MISMATCH") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def match(recipe: TemplateRecipe, clip_metas: list[ClipMeta]) -> AssemblyPlan:
    """Greedy template match. Returns AssemblyPlan sorted by slot.position.

    Args:
        recipe: TemplateRecipe extracted from the reference TikTok.
        clip_metas: list of ClipMeta from Gemini clip analysis (may include
                    degraded/fallback metas — callers must threshold-filter
                    fatal failures before calling this).

    Raises:
        TemplateMismatchError: if no clips are provided, or if any slot
                               cannot be satisfied by duration.
    """
    if not clip_metas:
        raise TemplateMismatchError(
            "No clips provided for template matching",
            code="TEMPLATE_CLIP_DURATION_MISMATCH",
        )

    if not recipe.slots:
        raise TemplateMismatchError(
            "Template recipe has no slots",
            code="TEMPLATE_CLIP_DURATION_MISMATCH",
        )

    # Sort slots by priority descending — assign best moments to highest-priority slots
    slots_by_priority = sorted(recipe.slots, key=lambda s: s.get("priority", 1), reverse=True)

    plan: list[AssemblyStep] = []
    last_clip_id: str | None = None

    for slot in slots_by_priority:
        target_dur = float(slot.get("target_duration_s", slot.get("target_duration", 5.0)))
        slot_position = slot.get("position", 1)
        slot_priority = slot.get("priority", 1)

        # Collect all (meta, moment) pairs within duration tolerance
        candidates = [
            (meta, moment)
            for meta in clip_metas
            for moment in meta.best_moments
            if isinstance(moment, dict)
            and abs(_moment_duration(moment) - target_dur) <= DURATION_TOLERANCE_S
        ]

        if not candidates:
            raise TemplateMismatchError(
                f"No clip fits slot {slot_position} requiring ~{target_dur:.1f}s. "
                f"Upload clips with moments ≥{max(0.0, target_dur - DURATION_TOLERANCE_S):.0f}s.",
                code="TEMPLATE_CLIP_DURATION_MISMATCH",
            )

        # Score: energy minus variety penalty for repeated adjacent clip
        best_meta, best_moment = max(
            candidates,
            key=lambda pair: pair[1].get("energy", 5.0) - (
                VARIETY_PENALTY if pair[0].clip_id == last_clip_id else 0.0
            ),
        )

        plan.append(AssemblyStep(slot=slot, clip_id=best_meta.clip_id, moment=best_moment))
        last_clip_id = best_meta.clip_id

        log.debug(
            "slot_assigned",
            position=slot_position,
            priority=slot_priority,
            clip_id=best_meta.clip_id,
            energy=best_moment.get("energy"),
            target_dur=target_dur,
        )

    # CRITICAL: sort by slot.position before returning — FFmpeg concat needs temporal order
    sorted_plan = sorted(plan, key=lambda step: step.slot.get("position", 0))

    clips_used = len({s.clip_id for s in sorted_plan})
    log.info("template_match_done", slots=len(sorted_plan), clips_used=clips_used)
    return AssemblyPlan(steps=sorted_plan)


def _moment_duration(moment: dict) -> float:
    """Compute moment duration from start_s/end_s or fall back to 0."""
    start = float(moment.get("start_s", 0.0))
    end = float(moment.get("end_s", 0.0))
    return max(0.0, end - start)
