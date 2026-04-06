"""Template matcher — greedy algorithm with hard per-clip usage cap.

match(recipe, clip_metas) → AssemblyPlan

Algorithm:
  1. Sort slots by priority (highest first) — greedy assigns best clips to
     most important slots first.
  2. For each slot, run a two-pass candidate search:
     - Tight pass (±2s): prefer clips whose moment duration closely matches.
     - Loose pass (±6s): only used when no tight candidate exists.
  3. First try candidates where clip_use_count < max_uses (hard cap).
     If none qualify, fall back to any candidate within duration tolerance.
  4. Pick the highest-scoring pair (by moment energy).
  5. CRITICAL: sort final plan by slot.position before returning (temporal
     order for FFmpeg concat — greedy builds in priority order).

Hard cap: each clip can be used at most ceil(n_slots / n_clips) times.
With 5 clips, 5 slots → max 1 use per clip.
With 3 clips, 8 slots → max 3 uses per clip.

Raises TemplateMismatchError when:
  - clip_metas is empty
  - No clip can satisfy a slot's duration requirement
"""


import math
from collections import defaultdict

import structlog

from app.pipeline.agents.gemini_analyzer import AssemblyPlan, AssemblyStep, ClipMeta, TemplateRecipe

log = structlog.get_logger()

DURATION_TOLERANCE_PRIMARY_S = 2.0   # tight pass — prefer close duration matches
DURATION_TOLERANCE_FALLBACK_S = 6.0  # loose pass — only if no tight match exists


class TemplateMismatchError(Exception):
    """Raised when clips cannot satisfy the template's slot requirements."""

    def __init__(self, message: str, code: str = "TEMPLATE_CLIP_DURATION_MISMATCH") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _minimum_coverage_pass(
    slots: list[dict],
    clip_metas: list[ClipMeta],
) -> dict[int, tuple[ClipMeta, dict]]:
    """Pre-assign clips to slots to maximize clip coverage (variety).

    Assigns the most-constrained clips first (fewest valid slots) so they
    don't get squeezed out by the greedy quality pass.

    Returns:
        dict mapping slot_position → (ClipMeta, moment) for pre-assigned slots.
    """
    # Build compatibility matrix: for each clip, which slots are duration-compatible?
    clip_valid_slots: dict[str, list[tuple[dict, dict]]] = {}
    for meta in clip_metas:
        valid: list[tuple[dict, dict]] = []
        for slot in slots:
            target_dur = float(slot.get("target_duration_s", slot.get("target_duration", 5.0)))
            for moment in meta.best_moments:
                if not isinstance(moment, dict):
                    continue
                if abs(_moment_duration(moment) - target_dur) <= DURATION_TOLERANCE_FALLBACK_S:
                    valid.append((slot, moment))
                    break  # one match per slot is enough for coverage
        clip_valid_slots[meta.clip_id] = valid

    # Sort clips by constraint degree ascending — most constrained placed first
    sorted_clips = sorted(
        clip_metas,
        key=lambda m: len(clip_valid_slots.get(m.clip_id, [])),
    )

    assigned_positions: set[int] = set()
    used_clips: set[str] = set()
    pre_assigned: dict[int, tuple[ClipMeta, dict]] = {}

    for meta in sorted_clips:
        if meta.clip_id in used_clips:
            continue
        valid = clip_valid_slots.get(meta.clip_id, [])
        if not valid:
            log.warning("coverage_skip_no_valid_slots", clip_id=meta.clip_id)
            continue

        # Find best available slot (not yet pre-assigned)
        best_slot = None
        best_moment = None
        best_score = None
        for slot, moment in valid:
            pos = slot.get("position", 0)
            if pos in assigned_positions:
                continue
            target_dur = float(slot.get("target_duration_s", slot.get("target_duration", 5.0)))
            slot_energy = float(slot.get("energy", 5.0))
            dur_fit = -abs(_moment_duration(moment) - target_dur)
            energy_fit = -abs(moment.get("energy", 5.0) - slot_energy)
            score = (dur_fit, energy_fit)
            if best_score is None or score > best_score:
                best_score = score
                best_slot = slot
                best_moment = moment

        if best_slot is not None and best_moment is not None:
            pos = best_slot.get("position", 0)
            assigned_positions.add(pos)
            used_clips.add(meta.clip_id)
            pre_assigned[pos] = (meta, best_moment)

    log.info(
        "coverage_pass_done",
        total_clips=len(clip_metas),
        assigned=len(pre_assigned),
        skipped=len(clip_metas) - len(pre_assigned),
    )
    return pre_assigned


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

    n_slots = len(recipe.slots)
    n_clips = len(clip_metas)
    # Hard cap: spread usage as evenly as possible. ceil(5/5)=1, ceil(8/3)=3, etc.
    max_uses = max(1, math.ceil(n_slots / n_clips))
    clip_use_count: dict[str, int] = defaultdict(int)

    # Run coverage-first pre-assignment — guarantees maximum clip variety
    pre_assigned = _minimum_coverage_pass(recipe.slots, clip_metas)
    plan: list[AssemblyStep] = []

    # Seed plan + use counts from pre-assigned slots
    for pos, (meta, moment) in pre_assigned.items():
        slot = next(s for s in recipe.slots if s.get("position", 0) == pos)
        clip_use_count[meta.clip_id] += 1
        plan.append(AssemblyStep(slot=slot, clip_id=meta.clip_id, moment=moment))
        log.debug(
            "slot_pre_assigned",
            position=pos,
            clip_id=meta.clip_id,
            energy=moment.get("energy"),
        )

    pre_assigned_positions = set(pre_assigned.keys())

    for slot in slots_by_priority:
        # Skip slots already handled by coverage pass
        if slot.get("position", 0) in pre_assigned_positions:
            continue
        target_dur = float(slot.get("target_duration_s", slot.get("target_duration", 5.0)))
        slot_position = slot.get("position", 1)
        slot_priority = slot.get("priority", 1)
        slot_energy = float(slot.get("energy", 5.0))

        # Two-pass candidate search: tight (±2s) preferred; loose (±6s) fallback
        tight_candidates = [
            (meta, moment)
            for meta in clip_metas
            for moment in meta.best_moments
            if isinstance(moment, dict)
            and abs(_moment_duration(moment) - target_dur) <= DURATION_TOLERANCE_PRIMARY_S
        ]
        # Use tight if any found; otherwise broaden to loose tolerance
        loose_candidates = tight_candidates or [
            (meta, moment)
            for meta in clip_metas
            for moment in meta.best_moments
            if isinstance(moment, dict)
            and abs(_moment_duration(moment) - target_dur) <= DURATION_TOLERANCE_FALLBACK_S
        ]

        if not loose_candidates:
            raise TemplateMismatchError(
                f"No clip fits slot {slot_position} requiring ~{target_dur:.1f}s. "
                "Upload clips with moments "
                f"≥{max(0.0, target_dur - DURATION_TOLERANCE_FALLBACK_S):.0f}s.",
                code="TEMPLATE_CLIP_DURATION_MISMATCH",
            )

        # First pass: only clips under usage cap
        capped_candidates = [
            (meta, moment)
            for meta, moment in loose_candidates
            if clip_use_count[meta.clip_id] < max_uses
        ]

        # Fall back to any candidate if all clips under cap are exhausted
        candidates = capped_candidates if capped_candidates else loose_candidates

        # Scoring: (1) least-used clips first (round-robin ensures all clips featured),
        # (2) closest energy match to slot's musical intensity (cohesive music-footage sync),
        # (3) highest absolute energy as final tiebreaker.
        # Energy match is negative distance: -|moment_energy - slot_energy| → closer = higher score.
        best_meta, best_moment = max(
            candidates,
            key=lambda pair: (
                -clip_use_count[pair[0].clip_id],
                -abs(pair[1].get("energy", 5.0) - slot_energy),
                pair[1].get("energy", 5.0),
            ),
        )

        clip_use_count[best_meta.clip_id] += 1
        plan.append(AssemblyStep(slot=slot, clip_id=best_meta.clip_id, moment=best_moment))

        log.debug(
            "slot_assigned",
            position=slot_position,
            priority=slot_priority,
            clip_id=best_meta.clip_id,
            energy=best_moment.get("energy"),
            target_dur=target_dur,
            use_count=clip_use_count[best_meta.clip_id],
            cap_enforced=bool(capped_candidates),
        )

    # CRITICAL: sort by slot.position before returning — FFmpeg concat needs temporal order
    sorted_plan = sorted(plan, key=lambda step: step.slot.get("position", 0))
    sorted_plan = _dedup_adjacent(sorted_plan)

    clips_used = len({s.clip_id for s in sorted_plan})
    log.info("template_match_done", slots=len(sorted_plan), clips_used=clips_used)
    return AssemblyPlan(steps=sorted_plan)


def _dedup_adjacent(steps: list[AssemblyStep]) -> list[AssemblyStep]:
    """Swap clip assignments between adjacent-duplicate steps when duration-compatible.

    Iterates left-to-right through the position-sorted plan. When step[i] uses
    the same clip as step[i-1], finds the nearest step[j] (j > i) with a
    different clip and swaps their clip_id + moment — but only if both moments
    fit their new slots within the tight tolerance (±2s).

    Best-effort: if no compatible swap exists, accepts the adjacent duplicate.
    """
    for i in range(1, len(steps)):
        if steps[i].clip_id == steps[i - 1].clip_id:
            for j in range(i + 1, len(steps)):
                if steps[j].clip_id != steps[i - 1].clip_id:
                    dur_i = float(steps[i].slot.get("target_duration_s", 5.0))
                    dur_j = float(steps[j].slot.get("target_duration_s", 5.0))
                    moment_i_dur = _moment_duration(steps[i].moment)
                    moment_j_dur = _moment_duration(steps[j].moment)

                    if (abs(moment_i_dur - dur_j) <= DURATION_TOLERANCE_PRIMARY_S
                            and abs(moment_j_dur - dur_i) <= DURATION_TOLERANCE_PRIMARY_S):
                        steps[i].clip_id, steps[j].clip_id = steps[j].clip_id, steps[i].clip_id
                        steps[i].moment, steps[j].moment = steps[j].moment, steps[i].moment
                        break
    return steps


def _moment_duration(moment: dict) -> float:
    """Compute moment duration from start_s/end_s or fall back to 0."""
    start = float(moment.get("start_s", 0.0))
    end = float(moment.get("end_s", 0.0))
    return max(0.0, end - start)
