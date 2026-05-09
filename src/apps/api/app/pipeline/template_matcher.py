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

# Sentinel clip_id for slots locked to the original template's source video.
# Resolved to template.gcs_path's local download in the orchestrator.
LOCKED_TEMPLATE_CLIP_ID = "__TEMPLATE_LOCKED__"


def _is_locked_slot(slot: dict) -> bool:
    return bool(slot.get("locked", False))

# Curtain-close constraints (must stay in sync with text_overlay.py)
_MIN_CURTAIN_ANIMATE_S = 4.0
_CURTAIN_MAX_RATIO = 0.6


# ── Ball-action ranking bonus ─────────────────────────────────────────────────
# When recipe.clip_filter_hint mentions football/ball, give moments whose
# description matches the whitelist a hard ranking advantage so they outrank
# vague/non-action moments even when energy/duration fits worse.
# Mirror of gemini_analyzer._BALL_WHITELIST (kept in sync deliberately).
_BALL_ACTION_KEYWORDS = (
    "ball", "top ", " top", "shot", "şut", "vuruş", "pass", "pas ", "asist",
    "goal", "gol", "dribble", "çalım", "save", "kurtarış", "kurtardı",
    "tackle", "tekleme", "header", "kafa", "cross", "ortaladı",
    "strike", "volley", "voleyle", "korner", "corner", "freekick", "frikik",
    "penalty", "penaltı", "intercept", "kesme", "control", "kontrol",
    "attack", "atak", "hücum", "finish", "bitiriş",
)
_FOOTBALL_HINT_KEYS = ("ball", "top", "futbol", "football", "soccer")


def _is_football_hint(hint: str) -> bool:
    if not hint:
        return False
    h = hint.lower()
    return any(k in h for k in _FOOTBALL_HINT_KEYS)


def _ball_bonus(moment: dict) -> float:
    """Returns 2.0 if moment description mentions ball-action, else 0.0.

    Caller decides whether to apply the bonus based on filter_hint. Always
    safe to call; returns 0.0 on missing/empty description.
    """
    if not isinstance(moment, dict):
        return 0.0
    desc = (moment.get("description") or "").lower()
    if not desc:
        return 0.0
    return 2.0 if any(k in desc for k in _BALL_ACTION_KEYWORDS) else 0.0


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
    if _is_locked_slot(slot_a) or _is_locked_slot(slot_b):
        return float("-inf")  # locked slots have exact source ranges, never merge
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

    # Per-recipe floor: travel templates with snappy pacing want all 24 slots
    # preserved even if the user uploads only 15 clips (matcher rotates clips
    # across slots instead of merging slots into longer chunks).
    recipe_min_slots = int(getattr(recipe, "min_slots", 0) or 0)

    # Count unique clips
    unique_clip_ids = {m.clip_id for m in clip_metas}
    n_unique_clips = len(unique_clip_ids)

    if n_unique_clips >= n_slots or recipe_min_slots >= n_slots:
        return recipe

    # Compute target slot count — preserve structural arc
    n_distinct_types = len(
        {s.get("slot_type", "broll") for s in recipe.slots}
    )
    target = max(
        n_unique_clips, n_distinct_types, CONSOLIDATION_MIN_SLOTS, recipe_min_slots
    )

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
    apply_ball_bonus: bool = False,
) -> dict[int, tuple[ClipMeta, dict]]:
    """Pre-assign clips to slots to maximize clip coverage (variety).

    Assigns the most-constrained clips first (fewest valid slots) so they
    don't get squeezed out by the greedy quality pass.

    Returns:
        dict mapping slot_position → (ClipMeta, moment) for pre-assigned slots.
    """
    # Locked slots are filled with template source — they don't accept user clips.
    unlocked_slots = [s for s in slots if not _is_locked_slot(s)]

    # Build compatibility matrix: for each clip, which slots are duration-compatible?
    # When apply_ball_bonus is True, prefer ball-action moments per (clip, slot) pair
    # so the most-constrained-clip-first heuristic still picks the ball moment.
    clip_valid_slots: dict[str, list[tuple[dict, dict]]] = {}
    for meta in clip_metas:
        valid: list[tuple[dict, dict]] = []
        for slot in unlocked_slots:
            target_dur = float(
                slot.get("target_duration_s", slot.get("target_duration", 5.0))
            )
            candidates_for_slot: list[dict] = []
            for moment in meta.best_moments:
                if not isinstance(moment, dict):
                    continue
                if abs(_moment_duration(moment) - target_dur) <= DURATION_TOLERANCE_FALLBACK_S:
                    candidates_for_slot.append(moment)
            if not candidates_for_slot:
                continue
            if apply_ball_bonus:
                # Pick the ball-action moment if any matches; else first available.
                best = max(candidates_for_slot,
                           key=lambda m: (_ball_bonus(m), m.get("energy", 5.0)))
            else:
                best = candidates_for_slot[0]
            valid.append((slot, best))
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
            ball = _ball_bonus(moment) if apply_ball_bonus else 0.0
            dur_fit = -abs(_moment_duration(moment) - target_dur)
            energy_fit = -abs(moment.get("energy", 5.0) - slot_energy)
            score = (ball, dur_fit, energy_fit)
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


# ── Greedy match ─────────────────────────────────────────────────────────────


def match(
    recipe: TemplateRecipe,
    clip_metas: list[ClipMeta],
    pinned_assignments: dict[int, str] | None = None,
    filter_hint: str = "",
) -> AssemblyPlan:
    """Greedy template match. Returns AssemblyPlan sorted by slot.position.

    Args:
        recipe: TemplateRecipe extracted from the reference TikTok.
        clip_metas: list of ClipMeta from Gemini clip analysis (may include
                    degraded/fallback metas — callers must threshold-filter
                    fatal failures before calling this).
        pinned_assignments: optional {slot_position: clip_id} pinning specific
                    clips to specific slots (e.g. user's face clip → slot 1).
                    Pinned slots bypass coverage + greedy passes; the pinned
                    clip's best matching moment is used, falling back to a
                    synthetic head-of-clip moment if no moment fits the slot.

    Raises:
        TemplateMismatchError: if no clips are provided, if any slot cannot be
                               satisfied by duration, or if a pinned clip is
                               not present in clip_metas.
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

    plan: list[AssemblyStep] = []
    locked_positions: set[int] = set()

    # Pre-emit locked slots — they bypass user-clip distribution entirely.
    # The orchestrator resolves LOCKED_TEMPLATE_CLIP_ID to the template's
    # downloaded source file at render time.
    for slot in recipe.slots:
        if not _is_locked_slot(slot):
            continue
        src_start = float(slot.get("source_start_s", 0.0))
        src_end = float(slot.get("source_end_s", src_start + float(
            slot.get("target_duration_s", slot.get("target_duration", 0.0))
        )))
        plan.append(AssemblyStep(
            slot=slot,
            clip_id=LOCKED_TEMPLATE_CLIP_ID,
            moment={
                "start_s": src_start,
                "end_s": src_end,
                "energy": float(slot.get("energy", 5.0)),
                "description": "locked template source",
            },
        ))
        locked_positions.add(slot.get("position", 0))

    unlocked_slots = [s for s in recipe.slots if not _is_locked_slot(s)]
    if not unlocked_slots:
        # Whole template is locked — no user clips to match.
        return AssemblyPlan(steps=sorted(plan, key=lambda s: s.slot.get("position", 0)))

    pinned_assignments = pinned_assignments or {}
    clip_meta_by_id = {m.clip_id: m for m in clip_metas}
    for pos, clip_id in pinned_assignments.items():
        if clip_id not in clip_meta_by_id:
            raise TemplateMismatchError(
                f"Pinned clip {clip_id} for slot {pos} not in analyzed clips",
                code="TEMPLATE_PIN_MISSING_CLIP",
            )

    # Sort slots by priority descending — assign best moments to highest-priority slots
    slots_by_priority = sorted(
        unlocked_slots, key=lambda s: s.get("priority", 1), reverse=True
    )

    n_slots = len(unlocked_slots)
    n_clips = len(clip_metas)
    # Hard cap: spread usage as evenly as possible. ceil(5/5)=1, ceil(8/3)=3, etc.
    max_uses = max(1, math.ceil(n_slots / n_clips))
    clip_use_count: dict[str, int] = defaultdict(int)

    apply_ball_bonus = _is_football_hint(filter_hint)
    pre_assigned_positions: set[int] = set()
    pinned_clip_ids: set[str] = set()
    used_moments: set[tuple[str, float, float]] = set()  # variety: dedup moments across slots

    # ── Phase 0: pinned assignments — non-negotiable, processed first ─────
    for pos, clip_id in pinned_assignments.items():
        slot = next((s for s in unlocked_slots if s.get("position", 0) == pos), None)
        if slot is None:
            raise TemplateMismatchError(
                f"Pinned slot {pos} not found in recipe",
                code="TEMPLATE_PIN_INVALID_SLOT",
            )
        meta = clip_meta_by_id[clip_id]
        moment = _resolve_pinned_moment(meta, slot)
        clip_use_count[clip_id] += 1
        pinned_clip_ids.add(clip_id)
        pre_assigned_positions.add(pos)
        plan.append(AssemblyStep(slot=slot, clip_id=clip_id, moment=moment))
        log.info("slot_pinned", position=pos, clip_id=clip_id,
                 target_dur=slot.get("target_duration_s"))

    # Coverage pass excludes locked slots, pinned slots, and pinned clips.
    coverage_slots = [
        s for s in unlocked_slots if s.get("position", 0) not in pre_assigned_positions
    ]
    coverage_metas = [m for m in clip_metas if m.clip_id not in pinned_clip_ids]
    pre_assigned = (
        _minimum_coverage_pass(coverage_slots, coverage_metas, apply_ball_bonus=apply_ball_bonus)
        if coverage_metas else {}
    )

    # Seed used_moments from coverage to give the greedy variety pass an accurate base
    for _pos, (_meta, _mom) in pre_assigned.items():
        used_moments.add((_meta.clip_id,
                          float(_mom.get("start_s", 0.0)),
                          float(_mom.get("end_s", 0.0))))

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

    pre_assigned_positions.update(pre_assigned.keys())

    # Greedy phase must NEVER reuse pinned clips — they represent intentional
    # user choices (face/intro). Reusing one in another slot breaks the contract.
    greedy_metas = [m for m in clip_metas if m.clip_id not in pinned_clip_ids]

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

        # Two-pass candidate search: tight (±2s) preferred; loose (±6s) fallback.
        # Greedy candidates exclude pinned clips so a pinned face/hook clip
        # never bleeds into a non-pinned slot.
        tight_candidates = [
            (meta, moment)
            for meta in greedy_metas
            for moment in meta.best_moments
            if isinstance(moment, dict)
            and abs(_moment_duration(moment) - target_dur)
            <= DURATION_TOLERANCE_PRIMARY_S
        ]
        # Use tight if any found; otherwise broaden to loose tolerance
        loose_candidates = tight_candidates or [
            (meta, moment)
            for meta in greedy_metas
            for moment in meta.best_moments
            if isinstance(moment, dict)
            and abs(_moment_duration(moment) - target_dur)
            <= DURATION_TOLERANCE_FALLBACK_S
        ]

        if not loose_candidates:
            min_useful_s = max(0.0, target_dur - DURATION_TOLERANCE_FALLBACK_S)
            max_useful_s = target_dur + DURATION_TOLERANCE_FALLBACK_S
            hint = (
                f"Upload longer clips — slot {slot_position} needs a moment "
                f"about {target_dur:.0f}s long"
            )
            if min_useful_s > 0:
                hint += (
                    f" (we accept moments {min_useful_s:.0f}s–{max_useful_s:.0f}s)"
                )
            hint += "."
            raise TemplateMismatchError(
                f"No clip fits slot {slot_position} "
                f"requiring ~{target_dur:.1f}s. {hint}",
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

        # Scoring (highest tuple wins):
        # (1) ball-action bonus (when filter_hint is football) — ball moments outrank vague,
        # (2) variety penalty — moments already used in another slot rank lower,
        # (3) least-used clips first (round-robin ensures all clips featured),
        # (4) closest energy match to slot's musical intensity,
        # (5) highest absolute energy as final tiebreaker.
        def _score_candidate(pair: tuple[ClipMeta, dict]) -> tuple:
            meta, moment = pair
            ball = _ball_bonus(moment) if apply_ball_bonus else 0.0
            mkey = (meta.clip_id,
                    float(moment.get("start_s", 0.0)),
                    float(moment.get("end_s", 0.0)))
            variety = -1.0 if mkey in used_moments else 0.0
            return (
                ball,
                variety,
                -clip_use_count[meta.clip_id],
                -abs(moment.get("energy", 5.0) - slot_energy),
                moment.get("energy", 5.0),
            )

        best_meta, best_moment = max(candidates, key=_score_candidate)
        used_moments.add((best_meta.clip_id,
                          float(best_moment.get("start_s", 0.0)),
                          float(best_moment.get("end_s", 0.0))))

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
        # Locked steps reference the template source — never swap them.
        if (
            steps[i].clip_id == LOCKED_TEMPLATE_CLIP_ID
            or steps[i - 1].clip_id == LOCKED_TEMPLATE_CLIP_ID
        ):
            continue
        if steps[i].clip_id == steps[i - 1].clip_id:
            for j in range(i + 1, len(steps)):
                if steps[j].clip_id == LOCKED_TEMPLATE_CLIP_ID:
                    continue  # don't swap into locked position
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


def _resolve_pinned_moment(meta: ClipMeta, slot: dict) -> dict:
    """Pick the best moment for a clip pinned to a specific slot.

    Order: tight-tolerance match → loose-tolerance match → synthetic head-of-clip
    window. The synthetic fallback preserves user intent — if the user pins
    a face clip, we use the first ``target_duration_s`` of it even when Gemini
    didn't surface a matching moment.
    """
    target_dur = float(
        slot.get("target_duration_s", slot.get("target_duration", 5.0))
    )
    tight: list[dict] = []
    loose: list[dict] = []
    for moment in meta.best_moments:
        if not isinstance(moment, dict):
            continue
        delta = abs(_moment_duration(moment) - target_dur)
        if delta <= DURATION_TOLERANCE_PRIMARY_S:
            tight.append(moment)
        if delta <= DURATION_TOLERANCE_FALLBACK_S:
            loose.append(moment)

    if tight:
        return max(tight, key=lambda m: m.get("energy", 5.0))
    if loose:
        return max(loose, key=lambda m: m.get("energy", 5.0))

    return {
        "start_s": 0.0,
        "end_s": target_dur,
        "energy": float(slot.get("energy", 6.0)),
        "description": f"head-of-clip ({target_dur:.1f}s) for pinned slot",
    }
