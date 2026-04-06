"""Unit tests for pipeline/template_matcher.py — pure functions, no DB/Gemini."""

import pytest

from app.pipeline.agents.gemini_analyzer import AssemblyPlan, AssemblyStep, ClipMeta, TemplateRecipe
from app.pipeline.template_matcher import (
    TemplateMismatchError,
    _dedup_adjacent,
    _minimum_coverage_pass,
    match,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_recipe(slots: list[dict]) -> TemplateRecipe:
    return TemplateRecipe(
        shot_count=len(slots),
        total_duration_s=sum(s.get("target_duration_s", 5.0) for s in slots),
        hook_duration_s=3.0,
        slots=slots,
        copy_tone="casual",
        caption_style="bold",
    )


def _make_clip(clip_id: str, moments: list[dict]) -> ClipMeta:
    return ClipMeta(
        clip_id=clip_id,
        transcript="test transcript",
        hook_text="test hook",
        hook_score=7.0,
        best_moments=moments,
    )


def _slot(position: int, target_dur: float, priority: int = 5, slot_type: str = "broll") -> dict:
    return {
        "position": position,
        "target_duration_s": target_dur,
        "priority": priority,
        "slot_type": slot_type,
    }


def _moment(start_s: float, end_s: float, energy: float = 7.0) -> dict:
    return {"start_s": start_s, "end_s": end_s, "energy": energy, "description": "test"}


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestTemplateMatcher:
    def test_happy_path_returns_assembly_plan(self):
        recipe = _make_recipe([
            _slot(1, 5.0, priority=10, slot_type="hook"),
            _slot(2, 10.0, priority=5, slot_type="broll"),
        ])
        clips = [
            _make_clip("clip_a", [_moment(0.0, 5.0, energy=9.0)]),
            _make_clip("clip_b", [_moment(0.0, 10.0, energy=6.0)]),
        ]

        plan = match(recipe, clips)

        assert isinstance(plan, AssemblyPlan)
        assert len(plan.steps) == 2

    def test_output_sorted_by_slot_position(self):
        """Critical: FFmpeg concat needs temporal order, not priority order."""
        recipe = _make_recipe([
            _slot(1, 5.0, priority=1),   # low priority, first position
            _slot(2, 5.0, priority=10),  # high priority, second position
        ])
        # One clip covers both durations
        clips = [
            _make_clip("clip_a", [
                _moment(0.0, 5.0, energy=8.0),
                _moment(5.0, 10.0, energy=9.0),
            ]),
        ]

        plan = match(recipe, clips)

        positions = [step.slot["position"] for step in plan.steps]
        assert positions == sorted(positions), "Steps must be sorted by slot.position"

    def test_variety_penalty_prefers_different_clips(self):
        """Adjacent slots should prefer different clips when options exist."""
        recipe = _make_recipe([
            _slot(1, 5.0, priority=5),
            _slot(2, 5.0, priority=5),
        ])
        clip_a = _make_clip("clip_a", [_moment(0.0, 5.0, energy=8.0)])
        clip_b = _make_clip("clip_b", [_moment(0.0, 5.0, energy=7.8)])  # slightly lower energy

        plan = match(recipe, [clip_a, clip_b])

        clip_ids = [step.clip_id for step in plan.steps]
        # With variety penalty -0.3 on clip_a for second slot, clip_b should be preferred
        assert len(set(clip_ids)) == 2, "Variety penalty should cause different clips to be used"

    def test_empty_clips_raises_template_mismatch(self):
        recipe = _make_recipe([_slot(1, 5.0)])

        with pytest.raises(TemplateMismatchError) as exc_info:
            match(recipe, [])

        assert exc_info.value.code == "TEMPLATE_CLIP_DURATION_MISMATCH"

    def test_duration_mismatch_raises_template_mismatch(self):
        """Clips too short for the slot should raise."""
        recipe = _make_recipe([_slot(1, 30.0, priority=5)])  # 30s slot
        clips = [
            _make_clip("clip_a", [_moment(0.0, 5.0)])  # only 5s moment — too short (±6s tolerance)
        ]

        with pytest.raises(TemplateMismatchError) as exc_info:
            match(recipe, clips)

        assert exc_info.value.code == "TEMPLATE_CLIP_DURATION_MISMATCH"
        assert "30.0s" in exc_info.value.message

    def test_duration_tolerance_allows_close_matches(self):
        """Moments within ±6s of target should be accepted."""
        recipe = _make_recipe([_slot(1, 10.0)])  # 10s target
        clips = [_make_clip("clip_a", [_moment(0.0, 15.0)])]  # 15s — within ±6s

        plan = match(recipe, clips)

        assert len(plan.steps) == 1

    def test_greedy_matches_energy_to_slot(self):
        """Highest priority slot gets the best energy-matched moment."""
        recipe = _make_recipe([
            _slot(1, 5.0, priority=1),
            _slot(2, 5.0, priority=10),  # highest priority, default energy=5.0
        ])
        clip = _make_clip("clip_a", [
            _moment(0.0, 5.0, energy=9.0),   # far from slot energy 5.0
            _moment(5.0, 10.0, energy=5.0),   # exact match to slot energy 5.0
        ])

        plan = match(recipe, [clip])

        # slot position 2 has priority=10, processed first → gets energy=5.0 (closest match)
        slot2_step = next(s for s in plan.steps if s.slot["position"] == 2)
        assert slot2_step.moment["energy"] == pytest.approx(5.0)

    def test_high_energy_slot_gets_high_energy_moment(self):
        """Slot with high energy rating gets matched to high-energy footage."""
        slots = [
            {
                "position": 1, "target_duration_s": 5.0,
                "priority": 5, "slot_type": "broll",
                "energy": 2.0,
            },
            {
                "position": 2, "target_duration_s": 5.0,
                "priority": 5, "slot_type": "broll",
                "energy": 9.0,
            },
        ]
        recipe = _make_recipe(slots)
        clip = _make_clip("clip_a", [
            _moment(0.0, 5.0, energy=8.5),   # high energy
            _moment(5.0, 10.0, energy=2.5),   # low energy
        ])

        plan = match(recipe, [clip])

        # slot 2 (energy=9.0) should get the high-energy moment (8.5)
        slot2_step = next(s for s in plan.steps if s.slot["position"] == 2)
        assert slot2_step.moment["energy"] == pytest.approx(8.5)
        # slot 1 (energy=2.0) should get the low-energy moment (2.5)
        slot1_step = next(s for s in plan.steps if s.slot["position"] == 1)
        assert slot1_step.moment["energy"] == pytest.approx(2.5)


# ── Adjacency dedup tests ────────────────────────────────────────────────────


def _step(
    clip_id: str,
    position: int,
    target_dur: float = 5.0,
    moment_dur: float = 5.0,
) -> AssemblyStep:
    """Helper to build an AssemblyStep for dedup tests."""
    return AssemblyStep(
        slot=_slot(position, target_dur),
        clip_id=clip_id,
        moment=_moment(0.0, moment_dur),
    )


class TestDedupAdjacent:
    def test_adjacent_same_clip_swapped(self):
        """[A, A, B] → [A, B, A] — adjacent duplicate resolved by swap."""
        steps = [_step("A", 1), _step("A", 2), _step("B", 3)]

        result = _dedup_adjacent(steps)

        clip_ids = [s.clip_id for s in result]
        assert clip_ids == ["A", "B", "A"]

    def test_no_adjacency_issue_unchanged(self):
        """[A, B, A] → [A, B, A] — already non-adjacent, no swap needed."""
        steps = [_step("A", 1), _step("B", 2), _step("A", 3)]

        result = _dedup_adjacent(steps)

        clip_ids = [s.clip_id for s in result]
        assert clip_ids == ["A", "B", "A"]

    def test_single_clip_all_slots_noop(self):
        """[A, A, A] → [A, A, A] — only one clip, no swap candidate exists."""
        steps = [_step("A", 1), _step("A", 2), _step("A", 3)]

        result = _dedup_adjacent(steps)

        clip_ids = [s.clip_id for s in result]
        assert clip_ids == ["A", "A", "A"]

    def test_duration_incompatible_swap_skipped(self):
        """Swap skipped when moments don't fit new slots within ±2s tolerance."""
        # A's moment is 5s, B's moment is 15s. Slots are 5s and 15s.
        # Swapping would put A's 5s moment in B's 15s slot (|5-15|=10 > 2s) — skip.
        steps = [
            _step("A", 1, target_dur=5.0, moment_dur=5.0),
            _step("A", 2, target_dur=5.0, moment_dur=5.0),
            _step("B", 3, target_dur=15.0, moment_dur=15.0),
        ]

        result = _dedup_adjacent(steps)

        # No swap possible — B's 15s moment doesn't fit 5s slot (±2s)
        clip_ids = [s.clip_id for s in result]
        assert clip_ids == ["A", "A", "B"]

    def test_cascading_adjacency_resolved(self):
        """[A, A, B, B] → [A, B, A, B] — cascading adjacency fixed left-to-right."""
        steps = [_step("A", 1), _step("A", 2), _step("B", 3), _step("B", 4)]

        result = _dedup_adjacent(steps)

        clip_ids = [s.clip_id for s in result]
        # Position 2 (was A, adjacent to A@1) swaps with position 3 (B)
        assert clip_ids[0] != clip_ids[1], "Positions 1-2 should differ"
        assert clip_ids[2] != clip_ids[3], "Positions 3-4 should differ"

    def test_two_element_plan_swapped(self):
        """[A, A] with a swap candidate — minimum size plan."""
        # Need two clips for a swap to be possible, but with only 2 steps
        # both using clip A, there's no candidate with a different clip_id.
        steps = [_step("A", 1), _step("A", 2)]

        result = _dedup_adjacent(steps)

        # No swap candidate (only clip A) — unchanged
        clip_ids = [s.clip_id for s in result]
        assert clip_ids == ["A", "A"]

    def test_match_integration_both_clips_used(self):
        """End-to-end: match() with 5 slots, 2 clips → both clips appear."""
        recipe = _make_recipe([
            _slot(1, 5.0, priority=5),
            _slot(2, 5.0, priority=4),
            _slot(3, 5.0, priority=3),
            _slot(4, 5.0, priority=2),
            _slot(5, 5.0, priority=1),
        ])
        clips = [
            _make_clip("clip_a", [_moment(0.0, 5.0, energy=8.0), _moment(5.0, 10.0, energy=7.0)]),
            _make_clip("clip_b", [_moment(0.0, 5.0, energy=7.5), _moment(5.0, 10.0, energy=6.5)]),
        ]

        plan = match(recipe, clips)

        clip_ids = [step.clip_id for step in plan.steps]
        # Coverage pass ensures both clips are used
        assert "clip_a" in clip_ids
        assert "clip_b" in clip_ids
        # Dedup is best-effort with 2 clips in 5 slots — some adjacency may remain
        assert len(plan.steps) == 5


# ── Minimum coverage pass tests ──────────────────────────────────────────────


class TestMinimumCoveragePass:
    def test_all_clips_used_when_slots_match(self):
        """N clips with N slots of compatible duration → all clips assigned."""
        slots = [_slot(i + 1, 5.0) for i in range(4)]
        clips = [
            _make_clip(f"clip_{i}", [_moment(0.0, 5.0, energy=7.0)])
            for i in range(4)
        ]

        pre = _minimum_coverage_pass(slots, clips)

        assert len(pre) == 4
        used_clip_ids = {meta.clip_id for meta, _ in pre.values()}
        assert used_clip_ids == {"clip_0", "clip_1", "clip_2", "clip_3"}

    def test_constrained_clip_gets_its_only_slot(self):
        """A clip with only 1 valid slot gets that slot, even if others want it."""
        slots = [
            _slot(1, 5.0, priority=5),
            _slot(2, 15.0, priority=5),  # only clip_c can fit here
        ]
        # clip_a: fits 5s only
        clip_a = _make_clip("clip_a", [_moment(0.0, 5.0)])
        # clip_b: fits 5s only
        clip_b = _make_clip("clip_b", [_moment(0.0, 5.0)])
        # clip_c: fits 15s only (most constrained — only 1 valid slot)
        clip_c = _make_clip("clip_c", [_moment(0.0, 15.0)])

        pre = _minimum_coverage_pass(slots, [clip_a, clip_b, clip_c])

        # clip_c should get slot 2 (the 15s slot — its only option)
        assert 2 in pre
        assert pre[2][0].clip_id == "clip_c"

    def test_more_clips_than_slots_partial_coverage(self):
        """8 clips, 4 slots → only 4 clips get assigned (graceful partial)."""
        slots = [_slot(i + 1, 5.0) for i in range(4)]
        clips = [
            _make_clip(f"clip_{i}", [_moment(0.0, 5.0)])
            for i in range(8)
        ]

        pre = _minimum_coverage_pass(slots, clips)

        assert len(pre) == 4
        used_clip_ids = {meta.clip_id for meta, _ in pre.values()}
        assert len(used_clip_ids) == 4

    def test_clip_no_valid_moments_skipped(self):
        """A clip with no duration-compatible moments is gracefully skipped."""
        slots = [_slot(1, 5.0), _slot(2, 5.0)]
        clip_good = _make_clip("clip_good", [_moment(0.0, 5.0)])
        clip_bad = _make_clip("clip_bad", [_moment(0.0, 30.0)])  # 30s — way outside ±6s

        pre = _minimum_coverage_pass(slots, [clip_good, clip_bad])

        # Only clip_good should be assigned
        used_clip_ids = {meta.clip_id for meta, _ in pre.values()}
        assert "clip_good" in used_clip_ids
        assert "clip_bad" not in used_clip_ids

    def test_integration_16_clips_8_slots_all_unique(self):
        """match() with 16 clips and 8 slots → all 8 slots use unique clips."""
        slots = [_slot(i + 1, 5.0, priority=10 - i) for i in range(8)]
        recipe = _make_recipe(slots)
        clips = [
            _make_clip(f"clip_{i}", [_moment(0.0, 5.0, energy=5.0 + i * 0.2)])
            for i in range(16)
        ]

        plan = match(recipe, clips)

        clip_ids = [step.clip_id for step in plan.steps]
        assert len(set(clip_ids)) == 8, (
            f"Expected 8 unique clips in 8 slots, got {len(set(clip_ids))}: {clip_ids}"
        )
