"""Template matcher — greedy algorithm with hard per-clip usage cap.

match(recipe, clip_metas) → AssemblyPlan
consolidate_slots(recipe, clip_metas) → TemplateRecipe  (pre-match preprocessing)

Algorithm (match):
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


import dataclasses
import math
from collections import defaultdict

import structlog

from app.pipeline.agents.gemini_analyzer import (
    AssemblyPlan,
    AssemblyStep,
    ClipMeta,
    TemplateRecipe,
)

log = structlog.get_logger()

DURATION_TOLERANCE_PRIMARY_S = 2.0   # tight pass — prefer close duration matches
DURATION_TOLERANCE_FALLBACK_S = 6.0  # loose pass — only if no tight match exists

MAX_MERGED_DURATION_S = 20.0   # hard cap — no single slot longer than this
CONSOLIDATION_MIN_SLOTS = 2    # absolute floor (even if only 1 slot type)

# Curtain-close constraints (must stay in sync with text_overlay.py)
_MIN_CURTAIN_ANIMATE_S = 4.0
_CURTAIN_MAX_RATIO = 0.6


class TemplateMismatchError(Exception):
    """Raised when clips cannot satisfy the template's slot requirements."""

    def __init__(
        self, message: str, code: str = "TEMPLATE_CLIP_DURATION_MISMATCH"
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── Slot consolidation ───────────────────────────────────────────────────────


def _score_merge_pair(
    slot_a: dict,
    slot_b: dict,
    clip_metas: list[ClipMeta],
    interstitial_positions: set[int],
) -> float:
    """Score how desirable it is to merge two adjacent slots.

    Higher score = better merge candidate.  Returns -inf for merges that
    must never happen (hook involvement, excessive duration).
    """
    type_a = slot_a.get("slot_type", "broll")
    type_b = slot_b.get("slot_type", "broll")
    dur_a = float(
        slot_a.get("target_duration_s", slot_a.get("target_duration", 5.0))
    )
    dur_b = float(
        slot_b.get("target_duration_s", slot_b.get("target_duration", 5.0))
    )
    energy_a = float(slot_a.get("energy", 5.0))
    energy_b = float(slot_b.get("energy", 5.0))
    combined_dur = dur_a + dur_b
    pos_a = slot_a.get("position", 0)

    # Hard blocks — preserve hook/broll boundary but allow hook+hook merges
    if (type_a == "hook") != (type_b == "hook"):
        return float("-inf")  # never merge across hook/broll boundary
    if combined_dur >= MAX_MERGED_DURATION_S:
        return float("-inf")  # too long for a single shot

    score = 0.0

    # Same slot type bonus
    if type_a == type_b:
        score += 2.0

    # Similar energy
    if abs(energy_a - energy_b) < 2.0:
        score += 1.0

    # Both same non-hook content type (broll+broll preferred over hook+hook)
    if type_a == "broll" and type_b == "broll":
        score += 1.0

    # Prefer merging non-hook slots when possible
    if type_a != "hook" and type_b != "hook":
        score += 1.0

    # Clip moment fits merged duration — proportional to fit quality
    best_fit_score = 0.0
    for meta in clip_metas:
        for moment in meta.best_moments:
            if not isinstance(moment, dict):
                continue
            moment_dur = _moment_duration(moment)
            distance = abs(moment_dur - combined_dur)
            if distance <= DURATION_TOLERANCE_FALLBACK_S:
                fit = 3.0 * (1.0 - distance / DURATION_TOLERANCE_FALLBACK_S)
                best_fit_score = max(best_fit_score, fit)
    score += best_fit_score

    # Reasonable duration bonus
    if combined_dur < MAX_MERGED_DURATION_S:
        score += 1.0

    # Interstitial penalty — merging drops a creative transition
    if pos_a in interstitial_positions:
        score -= 2.0

    return score


def consolidate_slots(
    recipe: TemplateRecipe,
    clip_metas: list[ClipMeta],
) -> TemplateRecipe:
    """Merge adjacent slots when user has fewer clips than template slots.

    Returns the recipe unchanged when consolidation isn't needed.
    Runs between template/clip analysis and match().
    """
    n_slots = len(recipe.slots)
    if n_slots <= CONSOLIDATION_MIN_SLOTS:
        return recipe

    # Count unique clips
    unique_clip_ids = {m.clip_id for m in clip_metas}
    n_unique_clips = len(unique_clip_ids)

    if n_unique_clips >= n_slots:
        return recipe

    # Compute target slot count — preserve structural arc
    n_distinct_types = len(
        {s.get("slot_type", "broll") for s in recipe.slots}
    )
    target = max(n_unique_clips, n_distinct_types, CONSOLIDATION_MIN_SLOTS)

    if target >= n_slots:
        return recipe  # nothing to consolidate

    slots_to_remove = n_slots - target

    log.info(
        "consolidate_slots_start",
        n_slots=n_slots,
        n_unique_clips=n_unique_clips,
        target=target,
        merges_needed=slots_to_remove,
    )

    # Deep-copy slots to avoid mutating the original recipe's dicts
    slots = [
        dict(s) for s in sorted(
            recipe.slots, key=lambda s: s.get("position", 0)
        )
    ]

    # Collect interstitial after-slot positions
    interstitial_positions = {
        inter.get("after_slot", 0) for inter in (recipe.interstitials or [])
    }
    interstitials = list(recipe.interstitials or [])

    for _ in range(slots_to_remove):
        if len(slots) <= CONSOLIDATION_MIN_SLOTS:
            break

        # Score all adjacent pairs
        pairs: list[tuple[float, int]] = []
        for i in range(len(slots) - 1):
            score = _score_merge_pair(
                slots[i], slots[i + 1], clip_metas, interstitial_positions
            )
            pairs.append((score, i))

        if not pairs:
            break

        # Pick highest-scoring pair
        pairs.sort(key=lambda p: p[0], reverse=True)
        best_score, best_idx = pairs[0]

        if best_score == float("-inf"):
            break  # no viable merges remain

        slot_a = slots[best_idx]
        slot_b = slots[best_idx + 1]
        pos_a = slot_a.get("position", 0)
        pos_b = slot_b.get("position", 0)
        dur_a = float(
            slot_a.get(
                "target_duration_s", slot_a.get("target_duration", 5.0)
            )
        )
        dur_b = float(
            slot_b.get(
                "target_duration_s", slot_b.get("target_duration", 5.0)
            )
        )
        energy_a = float(slot_a.get("energy", 5.0))
        energy_b = float(slot_b.get("energy", 5.0))
        combined_dur = dur_a + dur_b

        # -- Merge execution --
        merged_slot = dict(slot_a)  # start from slot A
        merged_slot["target_duration_s"] = combined_dur
        merged_slot["priority"] = max(
            slot_a.get("priority", 1), slot_b.get("priority", 1)
        )
        # Weighted average energy
        merged_slot["energy"] = (
            (energy_a * dur_a + energy_b * dur_b) / combined_dur
        )
        # Mixed types → broll
        if slot_a.get("slot_type") != slot_b.get("slot_type"):
            merged_slot["slot_type"] = "broll"

        # -- Text overlay merge with intra-slot dedup --
        overlays_a = [dict(o) for o in slot_a.get("text_overlays", [])]
        overlays_b = list(slot_b.get("text_overlays", []))
        merged_overlays = list(overlays_a)

        for ob in overlays_b:
            shifted = dict(ob)
            shifted["start_s"] = float(shifted.get("start_s", 0.0)) + dur_a
            shifted["end_s"] = float(shifted.get("end_s", 0.0)) + dur_a
            if (
                "start_s_override" in shifted
                and shifted["start_s_override"] is not None
            ):
                shifted["start_s_override"] = (
                    float(shifted["start_s_override"]) + dur_a
                )
            if (
                "end_s_override" in shifted
                and shifted["end_s_override"] is not None
            ):
                shifted["end_s_override"] = (
                    float(shifted["end_s_override"]) + dur_a
                )

            # Intra-slot dedup: extend matching overlay instead of adding dup
            deduped = False
            for oa in merged_overlays:
                if oa.get("text") == shifted.get("text") and oa.get(
                    "position"
                ) == shifted.get("position"):
                    oa["end_s"] = max(
                        float(oa.get("end_s", 0.0)),
                        float(shifted.get("end_s", 0.0)),
                    )
                    deduped = True
                    break
            if not deduped:
                merged_overlays.append(shifted)

        merged_slot["text_overlays"] = merged_overlays

        # -- Remap interstitials --
        new_interstitials = []
        for inter in interstitials:
            after = inter.get("after_slot", 0)
            if after == pos_a:
                # Interior: cut point no longer exists — drop
                continue
            new_inter = dict(inter)
            if after == pos_b:
                # Tail: remap to merged slot's position
                new_inter["after_slot"] = pos_a
            elif after > pos_b:
                # Positions after merged pair shift down by 1
                new_inter["after_slot"] = after - 1
            new_interstitials.append(new_inter)
        interstitials = new_interstitials

        # Update interstitial_positions for next iteration
        interstitial_positions = {
            inter.get("after_slot", 0) for inter in interstitials
        }

        # Replace pair with merged slot
        slots = slots[:best_idx] + [merged_slot] + slots[best_idx + 2:]

    # -- Renumber positions sequentially (before curtain validation) --
    for i, slot in enumerate(slots):
        slot["position"] = i + 1

    # -- Post-merge curtain validation (after renumbering so lookups work) --
    validated_interstitials = []
    for inter in interstitials:
        if inter.get("type") == "curtain-close":
            after = inter.get("after_slot", 0)
            target_slot = next(
                (s for s in slots if s.get("position", 0) == after), None
            )
            if target_slot:
                slot_dur = float(
                    target_slot.get(
                        "target_duration_s",
                        target_slot.get("target_duration", 5.0),
                    )
                )
                max_curtain = slot_dur * _CURTAIN_MAX_RATIO
                if max_curtain < _MIN_CURTAIN_ANIMATE_S:
                    log.warning(
                        "consolidate_drop_curtain",
                        after_slot=after,
                        slot_dur=slot_dur,
                    )
                    continue  # drop this curtain
        validated_interstitials.append(inter)

    new_total = sum(
        float(s.get("target_duration_s", s.get("target_duration", 5.0)))
        for s in slots
    )

    result = dataclasses.replace(
        recipe,
        slots=slots,
        shot_count=len(slots),
        total_duration_s=new_total,
        interstitials=validated_interstitials,
    )
    log.info(
        "consolidate_slots_done",
        original_slots=n_slots,
        final_slots=len(slots),
        merges=n_slots - len(slots),
    )
    return result


# ── Coverage pass ────────────────────────────────────────────────────────────


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
            target_dur = float(
                slot.get("target_duration_s", slot.get("target_duration", 5.0))
            )
            for moment in meta.best_moments:
                if not isinstance(moment, dict):
                    continue
                if (
                    abs(_moment_duration(moment) - target_dur)
                    <= DURATION_TOLERANCE_FALLBACK_S
                ):
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
            target_dur = float(
                slot.get("target_duration_s", slot.get("target_duration", 5.0))
            )
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


# ── Hook legibility pre-pass ─────────────────────────────────────────────────


# Keywords parsed from Gemini moment descriptions to proxy "background quietness"
# without doing per-frame brightness analysis. Tuned for B1 scope (existing
# clip_meta only, no new image-analysis pass).
_LEGIBLE_DESC_KEYWORDS = frozenset({
    "wide", "static", "sky", "uniform", "scenic", "overhead",
    "minimal", "calm", "open", "still", "horizon", "sunset", "sunrise",
})
_BUSY_DESC_KEYWORDS = frozenset({
    "crowded", "busy", "fast", "chaotic", "complex", "fast-paced",
    "cluttered", "dense", "rapid",
})


def _hook_legibility_score(
    meta: ClipMeta,
    moment: dict,
    color: tuple[float, float] | None = None,
) -> float:
    """Score a (clip, moment) pair for title-text legibility.

    Higher = better backdrop for the white/red title rendered on the hook slot.

    Signal sources:
      - Lower energy → calmer, often less visually noisy → text reads better.
      - Description keywords → "wide/sky/scenic" hints at uniform backgrounds,
        "crowded/busy/chaotic" hints at backgrounds that swallow text.
      - Optional color tuple (avg_luma, red_ratio) from FFmpeg signalstats
        (see app.pipeline.color_probe). Dark frames boost contrast for white
        title text; red-dominant frames clash with the red "Thirds" overlay.

    Color weights: dark bonus capped at +2.0 (full black), red penalty capped
    at -3.0 (saturated red). Red weighted higher because color clash hurts
    readability more than low contrast on a non-red bright frame.

    Degraded or failed analyses score -inf so they never win the hook slot.
    """
    if meta.analysis_degraded or meta.failed:
        return float("-inf")
    energy = float(moment.get("energy", 5.0))
    score = -energy  # invert so higher = better
    desc = str(moment.get("description") or "").lower()
    for kw in _LEGIBLE_DESC_KEYWORDS:
        if kw in desc:
            score += 1.0
    for kw in _BUSY_DESC_KEYWORDS:
        if kw in desc:
            score -= 1.5
    if color is not None:
        avg_luma, red_ratio = color
        score += (255.0 - max(0.0, min(255.0, avg_luma))) / 255.0 * 2.0
        score -= max(0.0, min(1.0, red_ratio)) * 3.0
    return score


def _hook_legibility_pre_pass(
    slots: list[dict],
    clip_metas: list[ClipMeta],
    *,
    clip_paths: dict[str, str] | None = None,
) -> dict[int, tuple[ClipMeta, dict]]:
    """Pre-assign hook slots to the most legibility-friendly clip.

    Why before _minimum_coverage_pass: hook slots host the title text overlay,
    so the clip behind them must not fight the text. Coverage/greedy passes
    only score on duration+energy fit and ignore visual legibility.

    If clip_paths is provided (clip_id → local file path), each candidate gets
    a per-frame color probe at moment.start_s. Probe failures fall back to
    NEUTRAL_FALLBACK inside color_probe so a single broken clip doesn't drop
    the slot. Color contribution is skipped entirely when clip_paths is None
    (e.g., unit tests that mock out the probe).

    Returns: {hook_slot_position: (ClipMeta, moment)}.
    """
    # Local import to keep the matcher importable in environments without ffmpeg
    # (the import is a no-op for callers that pass clip_paths=None).
    from app.pipeline.color_probe import probe_clip_color  # noqa: PLC0415

    pre: dict[int, tuple[ClipMeta, dict]] = {}
    used: set[str] = set()
    color_cache: dict[tuple[str, float], tuple[float, float] | None] = {}

    def _color_for(meta: ClipMeta, moment: dict) -> tuple[float, float] | None:
        if not clip_paths:
            return None
        path = clip_paths.get(meta.clip_id)
        if not path:
            return None
        start_s = float(moment.get("start_s", 0.0) or 0.0)
        key = (meta.clip_id, round(start_s, 3))
        if key in color_cache:
            return color_cache[key]
        try:
            color = probe_clip_color(path, start_s)
        except Exception as exc:  # noqa: BLE001 — never fail the matcher on probe errors
            log.warning(
                "color_probe_unexpected",
                clip_id=meta.clip_id,
                start_s=start_s,
                err=str(exc),
            )
            color = None
        color_cache[key] = color
        return color

    for slot in [s for s in slots if s.get("slot_type") == "hook"]:
        target_dur = float(
            slot.get("target_duration_s", slot.get("target_duration", 5.0))
        )
        candidates: list[tuple[ClipMeta, dict, float, tuple[float, float] | None]] = []
        for meta in clip_metas:
            if meta.clip_id in used:
                continue
            for moment in meta.best_moments:
                if not isinstance(moment, dict):
                    continue
                if (
                    abs(_moment_duration(moment) - target_dur)
                    <= DURATION_TOLERANCE_FALLBACK_S
                ):
                    color = _color_for(meta, moment)
                    candidates.append(
                        (meta, moment, _hook_legibility_score(meta, moment, color), color)
                    )
        if not candidates:
            continue
        candidates.sort(key=lambda c: c[2], reverse=True)
        best_meta, best_moment, best_score, best_color = candidates[0]
        pos = slot.get("position", 1)
        pre[pos] = (best_meta, best_moment)
        used.add(best_meta.clip_id)
        log.info(
            "hook_legibility_pre_assigned",
            slot=pos,
            clip_id=best_meta.clip_id,
            score=round(best_score, 2),
            avg_luma=round(best_color[0], 1) if best_color else None,
            red_ratio=round(best_color[1], 3) if best_color else None,
        )
    return pre


# ── Greedy match ─────────────────────────────────────────────────────────────


def match(
    recipe: TemplateRecipe,
    clip_metas: list[ClipMeta],
    *,
    clip_paths: dict[str, str] | None = None,
) -> AssemblyPlan:
    """Greedy template match. Returns AssemblyPlan sorted by slot.position.

    Args:
        recipe: TemplateRecipe extracted from the reference TikTok.
        clip_metas: list of ClipMeta from Gemini clip analysis (may include
                    degraded/fallback metas — callers must threshold-filter
                    fatal failures before calling this).
        clip_paths: optional clip_id → local file path map. When provided,
                    the hook legibility pre-pass probes each candidate's
                    avg luminance + red ratio via FFmpeg signalstats so the
                    title sits on a dark, non-red backdrop. Tests omit this
                    to keep the matcher pure.

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
    slots_by_priority = sorted(
        recipe.slots, key=lambda s: s.get("priority", 1), reverse=True
    )

    n_slots = len(recipe.slots)
    n_clips = len(clip_metas)
    # Hard cap: spread usage as evenly as possible. ceil(5/5)=1, ceil(8/3)=3, etc.
    max_uses = max(1, math.ceil(n_slots / n_clips))
    clip_use_count: dict[str, int] = defaultdict(int)

    # Hook legibility pre-pass: pick the most title-friendly clip for each
    # hook slot BEFORE the coverage pass so the title doesn't end up over a
    # busy/bright background. Coverage pass then runs on remaining clips/slots.
    #
    # GUARD: skip pre-pass if it would drain the clip pool. With few clips
    # (e.g., 1 clip + 7 slots), the baseline reuses the same clip across
    # slots via max_uses; our pre-pass would claim that one clip and leave
    # zero for non-hook slots, raising TemplateMismatchError. Baseline first
    # in that case.
    hook_pre_candidate = _hook_legibility_pre_pass(
        recipe.slots, clip_metas, clip_paths=clip_paths
    )
    non_hook_slots_exist = any(
        s.get("position", 0) not in hook_pre_candidate
        for s in recipe.slots
    )
    remaining_clip_count = len(clip_metas) - len(hook_pre_candidate)
    if non_hook_slots_exist and remaining_clip_count == 0:
        log.info(
            "hook_legibility_pre_pass_skipped",
            reason="would_drain_clip_pool",
            clips=len(clip_metas),
            hook_assignments=len(hook_pre_candidate),
        )
        hook_pre = {}
    else:
        hook_pre = hook_pre_candidate

    used_clip_ids = {meta.clip_id for meta, _ in hook_pre.values()}
    remaining_metas = (
        [m for m in clip_metas if m.clip_id not in used_clip_ids]
        if hook_pre else clip_metas
    )
    remaining_slots = (
        [s for s in recipe.slots if s.get("position", 0) not in hook_pre]
        if hook_pre else recipe.slots
    )

    # Run coverage-first pre-assignment — guarantees maximum clip variety.
    # When hook_pre claimed clips/slots, coverage operates on the remainder.
    coverage_pre = _minimum_coverage_pass(remaining_slots, remaining_metas)
    pre_assigned = {**hook_pre, **coverage_pre}
    plan: list[AssemblyStep] = []

    # Seed plan + use counts from pre-assigned slots
    for pos, (meta, moment) in pre_assigned.items():
        slot = next(
            s for s in recipe.slots if s.get("position", 0) == pos
        )
        clip_use_count[meta.clip_id] += 1
        plan.append(
            AssemblyStep(slot=slot, clip_id=meta.clip_id, moment=moment)
        )
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
        target_dur = float(
            slot.get("target_duration_s", slot.get("target_duration", 5.0))
        )
        slot_position = slot.get("position", 1)
        slot_priority = slot.get("priority", 1)
        slot_energy = float(slot.get("energy", 5.0))

        # Two-pass candidate search: tight (±2s) preferred; loose (±6s) fallback
        tight_candidates = [
            (meta, moment)
            for meta in clip_metas
            for moment in meta.best_moments
            if isinstance(moment, dict)
            and abs(_moment_duration(moment) - target_dur)
            <= DURATION_TOLERANCE_PRIMARY_S
        ]
        # Use tight if any found; otherwise broaden to loose tolerance
        loose_candidates = tight_candidates or [
            (meta, moment)
            for meta in clip_metas
            for moment in meta.best_moments
            if isinstance(moment, dict)
            and abs(_moment_duration(moment) - target_dur)
            <= DURATION_TOLERANCE_FALLBACK_S
        ]

        if not loose_candidates:
            raise TemplateMismatchError(
                f"No clip fits slot {slot_position} "
                f"requiring ~{target_dur:.1f}s. "
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
        # (2) closest energy match to slot's musical intensity,
        # (3) highest absolute energy as final tiebreaker.
        best_meta, best_moment = max(
            candidates,
            key=lambda pair: (
                -clip_use_count[pair[0].clip_id],
                -abs(pair[1].get("energy", 5.0) - slot_energy),
                pair[1].get("energy", 5.0),
            ),
        )

        clip_use_count[best_meta.clip_id] += 1
        plan.append(
            AssemblyStep(
                slot=slot, clip_id=best_meta.clip_id, moment=best_moment
            )
        )

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
    sorted_plan = sorted(
        plan, key=lambda step: step.slot.get("position", 0)
    )
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
                    dur_i = float(
                        steps[i].slot.get("target_duration_s", 5.0)
                    )
                    dur_j = float(
                        steps[j].slot.get("target_duration_s", 5.0)
                    )
                    moment_i_dur = _moment_duration(steps[i].moment)
                    moment_j_dur = _moment_duration(steps[j].moment)

                    if (
                        abs(moment_i_dur - dur_j)
                        <= DURATION_TOLERANCE_PRIMARY_S
                        and abs(moment_j_dur - dur_i)
                        <= DURATION_TOLERANCE_PRIMARY_S
                    ):
                        steps[i].clip_id, steps[j].clip_id = (
                            steps[j].clip_id,
                            steps[i].clip_id,
                        )
                        steps[i].moment, steps[j].moment = (
                            steps[j].moment,
                            steps[i].moment,
                        )
                        break
    return steps


def _moment_duration(moment: dict) -> float:
    """Compute moment duration from start_s/end_s or fall back to 0."""
    start = float(moment.get("start_s", 0.0))
    end = float(moment.get("end_s", 0.0))
    return max(0.0, end - start)
