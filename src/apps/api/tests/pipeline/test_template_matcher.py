"""Unit tests for pipeline/template_matcher.py — pure functions, no DB/Gemini."""

import pytest

from app.pipeline.agents.gemini_analyzer import AssemblyPlan, ClipMeta, TemplateRecipe
from app.pipeline.template_matcher import TemplateMismatchError, match

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

    def test_greedy_assigns_highest_energy_to_highest_priority(self):
        """Highest priority slot should get the highest energy moment."""
        recipe = _make_recipe([
            _slot(1, 5.0, priority=1),
            _slot(2, 5.0, priority=10),  # highest priority
        ])
        clip = _make_clip("clip_a", [
            _moment(0.0, 5.0, energy=9.0),   # high energy
            _moment(5.0, 10.0, energy=3.0),  # low energy
        ])

        plan = match(recipe, [clip])

        # slot position 2 has priority=10, should get the high-energy moment
        slot2_step = next(s for s in plan.steps if s.slot["position"] == 2)
        assert slot2_step.moment["energy"] == pytest.approx(9.0)
