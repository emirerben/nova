"""Unit tests for pipeline/template_matcher.py — pure functions, no DB/Gemini."""

import pytest

from app.pipeline.agents.gemini_analyzer import AssemblyPlan, AssemblyStep, ClipMeta, TemplateRecipe
from app.pipeline.template_matcher import (
    LOCKED_TEMPLATE_CLIP_ID,
    MAX_MERGED_DURATION_S,
    TemplateMismatchError,
    _dedup_adjacent,
    _minimum_coverage_pass,
    consolidate_slots,
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

    def test_duration_mismatch_falls_back_when_moments_too_short(self):
        """Moments shorter than the slot's tolerance window should still match
        via the last-resort fallback. The downstream assembler uses
        moment.start_s and trims to slot.target_duration_s, so a too-short
        moment is fine when the underlying clip is long enough."""
        recipe = _make_recipe([_slot(1, 30.0, priority=5)])  # 30s slot
        clips = [
            # Only a 5s moment — far below the 30±6s tolerance window.
            # Old behavior: TemplateMismatchError. New behavior: matches via fallback.
            _make_clip("clip_a", [_moment(0.0, 5.0)])
        ]

        plan = match(recipe, clips)

        assert len(plan.steps) == 1
        assert plan.steps[0].clip_id == "clip_a"

    def test_duration_mismatch_falls_back_when_moments_too_long(self):
        """Reproduces the prod failure shape: 12s clip uploaded for a template
        whose slots want ~5s, Gemini extracts one full-clip moment of duration
        12s, |12-5|=7 > 6 tolerance. Old behavior: TemplateMismatchError. New
        behavior: matches via fallback — the assembler trims to 5s anyway."""
        recipe = _make_recipe([
            _slot(1, 5.0, priority=10),
            _slot(2, 5.0, priority=5),
        ])
        clips = [
            # Single 12s clip with one full-clip moment — exactly the prod failure.
            _make_clip("clip_a", [_moment(0.0, 12.0)])
        ]

        plan = match(recipe, clips)

        # Both slots fill from the single clip; assembler will pick distinct
        # windows via clip_cursor. The matcher's job is just clip selection.
        assert len(plan.steps) == 2
        assert all(s.clip_id == "clip_a" for s in plan.steps)

    def test_empty_best_moments_still_raises_template_mismatch(self):
        """When a clip has no best_moments at all (Gemini analysis empty),
        the matcher has nothing to fall back to and must raise. This is the
        only legitimate path to TemplateMismatchError now."""
        recipe = _make_recipe([_slot(1, 5.0, priority=5)])
        clips = [
            ClipMeta(
                clip_id="clip_a",
                transcript="",
                hook_text="",
                hook_score=5.0,
                best_moments=[],  # empty — nothing to assemble
                analysis_degraded=False,
                clip_path="/tmp/x.mp4",
            )
        ]

        with pytest.raises(TemplateMismatchError) as exc_info:
            match(recipe, clips)

        assert exc_info.value.code == "TEMPLATE_CLIP_DURATION_MISMATCH"
        # New copy: no longer mentions ">=0s" or talks about "uploading longer clips"
        assert "≥0s" not in exc_info.value.message
        assert "no usable moments" in exc_info.value.message.lower()

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


# ── Pinned-clip reuse regression (1-clip → multi-slot templates) ─────────────


class TestPinnedClipReuse:
    """Regression suite for TEMPLATE_CLIP_DURATION_MISMATCH on 1-clip uploads
    to multi-slot templates with a "hook" first slot.

    The Just Fine template (2 unlocked slots: hook + outro) auto-pins clip 0 to
    slot 1 in template_orchestrate.py. Before the fix, that left greedy_metas
    empty for slot 2 and the matcher raised. The greedy phase now lets a pinned
    clip back in when there are no other clips, while still excluding it when
    alternatives exist.
    """

    def test_single_clip_multi_slot_with_hook_pin(self):
        """1 clip, 2 unlocked slots, slot 1 pinned (hook). Both slots filled,
        same clip_id, different moments via variety dedup. This is the exact
        Just Fine — Sunset Reassurance failure path."""
        recipe = _make_recipe([
            _slot(1, 3.468, priority=9, slot_type="hook"),
            _slot(2, 5.002, priority=9, slot_type="outro"),
        ])
        # 12s clip with three moments — enough variety for slot 1 + slot 2
        clip = _make_clip("clip_0", [
            _moment(0.0, 3.5, energy=6.0),
            _moment(3.5, 8.5, energy=8.0),
            _moment(7.0, 12.0, energy=7.0),
        ])

        plan = match(recipe, [clip], pinned_assignments={1: "clip_0"})

        assert len(plan.steps) == 2
        slot1 = next(s for s in plan.steps if s.slot["position"] == 1)
        slot2 = next(s for s in plan.steps if s.slot["position"] == 2)
        assert slot1.clip_id == "clip_0"
        assert slot2.clip_id == "clip_0"
        # Variety dedup: slot 2 must pick a different moment than slot 1
        slot1_key = (slot1.moment["start_s"], slot1.moment["end_s"])
        slot2_key = (slot2.moment["start_s"], slot2.moment["end_s"])
        assert slot1_key != slot2_key, (
            "Variety dedup failed — both slots got the same moment range"
        )

    def test_pinned_reuse_only_when_no_alternatives(self):
        """2 clips, slot 1 pinned to clip_0. Slot 2 must go to clip_1, NOT
        reuse the pinned clip. The contract still holds when alternatives
        exist."""
        recipe = _make_recipe([
            _slot(1, 5.0, priority=9, slot_type="hook"),
            _slot(2, 5.0, priority=5, slot_type="broll"),
        ])
        clip_0 = _make_clip("clip_0", [_moment(0.0, 5.0, energy=8.0)])
        clip_1 = _make_clip("clip_1", [_moment(0.0, 5.0, energy=7.0)])

        plan = match(
            recipe, [clip_0, clip_1], pinned_assignments={1: "clip_0"}
        )

        slot2 = next(s for s in plan.steps if s.slot["position"] == 2)
        assert slot2.clip_id == "clip_1", (
            "Pinned clip leaked into a non-pinned slot when an alternative "
            "was available"
        )

    def test_three_slots_one_clip_pinned(self):
        """1 clip, 3 unlocked slots, slot 1 pinned. All three filled, all
        from the same clip, with max_uses=ceil(3/1)=3 honored."""
        recipe = _make_recipe([
            _slot(1, 4.0, priority=9, slot_type="hook"),
            _slot(2, 5.0, priority=7, slot_type="broll"),
            _slot(3, 6.0, priority=5, slot_type="outro"),
        ])
        clip = _make_clip("clip_0", [
            _moment(0.0, 4.0, energy=8.0),
            _moment(4.0, 9.0, energy=7.0),
            _moment(9.0, 15.0, energy=6.0),
        ])

        plan = match(recipe, [clip], pinned_assignments={1: "clip_0"})

        assert len(plan.steps) == 3
        assert all(s.clip_id == "clip_0" for s in plan.steps)


# ── No-reuse-when-supply-sufficient regression ────────────────────────────────


class TestNoReuseWhenSupplySufficient:
    """Regression for the matcher reusing one clip across multiple slots while
    other uploaded clips sat unused.

    Trigger: a clip whose moment fits the slot's duration tolerance gets used,
    then for the next slot the only `loose_candidates` entry that fits ±6s of
    the new slot's target_duration_s is THAT same clip. Other unused clips have
    moments that fall outside the ±6s tolerance window. Pre-fix the matcher
    fell back from the use-count-capped pool straight to the duration-loose
    pool — silently reusing the used clip even though the assembler trims
    durations downstream and would happily accept an unused clip with a
    "wrong-duration" moment. Post-fix the matcher inserts a duration-relaxed
    pass over unused clips before reaching the reuse fallback.
    """

    def test_no_reuse_when_durations_mismatch_and_unused_clips_exist(self):
        """The user's exact bug. One clip has a slot-fitting moment; the
        other four uploaded clips have moments well outside the duration
        tolerance. Pre-fix slot 2 reused the fitting clip; post-fix it picks
        an unused clip via the duration-relaxed branch."""
        recipe = _make_recipe([
            _slot(1, 4.0, priority=10, slot_type="hook"),
            _slot(2, 4.0, priority=9, slot_type="broll"),
            _slot(3, 4.0, priority=8, slot_type="broll"),
            _slot(4, 4.0, priority=7, slot_type="broll"),
            _slot(5, 4.0, priority=5, slot_type="outro"),
        ])
        # clip_a has a fitting 4s moment; b/c/d/e only have 15s moments
        # (|15 - 4| = 11s > DURATION_TOLERANCE_FALLBACK_S=6s).
        clips = [
            _make_clip("clip_a", [_moment(0.0, 4.0, energy=8.0)]),
            _make_clip("clip_b", [_moment(0.0, 15.0, energy=7.0)]),
            _make_clip("clip_c", [_moment(0.0, 15.0, energy=7.0)]),
            _make_clip("clip_d", [_moment(0.0, 15.0, energy=7.0)]),
            _make_clip("clip_e", [_moment(0.0, 15.0, energy=7.0)]),
        ]

        plan = match(recipe, clips)

        assert len(plan.steps) == 5
        clip_ids_used = [s.clip_id for s in plan.steps]
        assert sorted(clip_ids_used) == ["clip_a", "clip_b", "clip_c", "clip_d", "clip_e"], (
            f"Expected each uploaded clip used exactly once when supply "
            f"equals demand, got {clip_ids_used}"
        )

    def test_no_reuse_when_supply_exceeds_demand(self):
        """7 clips, 5 slots. Even when only one clip duration-fits, the
        remaining 4 slots must pull from the other 6 unused clips. Two clips
        legitimately stay unused."""
        recipe = _make_recipe([_slot(p, 4.0, priority=10 - p) for p in range(1, 6)])
        clips = [
            _make_clip("clip_a", [_moment(0.0, 4.0, energy=8.0)]),
        ] + [
            _make_clip(f"clip_{c}", [_moment(0.0, 15.0, energy=7.0)])
            for c in ("b", "c", "d", "e", "f", "g")
        ]

        plan = match(recipe, clips)

        clip_ids_used = [s.clip_id for s in plan.steps]
        assert len(set(clip_ids_used)) == 5, (
            f"Expected 5 distinct clips when supply (7) exceeds demand (5), "
            f"got {len(set(clip_ids_used))} distinct: {clip_ids_used}"
        )

    def test_reuse_allowed_when_supply_less_than_demand(self):
        """2 clips, 5 slots. max_uses = ceil(5/2) = 3. Each clip used 2 or 3
        times; total = 5. Guards against over-correcting the fix."""
        recipe = _make_recipe([_slot(p, 4.0, priority=10 - p) for p in range(1, 6)])
        # Each clip has several moments so the matcher has variety to choose
        # from per-slot without depleting one clip's moments before reuse.
        clips = [
            _make_clip("clip_a", [
                _moment(0.0, 4.0, energy=8.0),
                _moment(4.0, 8.0, energy=7.0),
                _moment(8.0, 12.0, energy=6.0),
            ]),
            _make_clip("clip_b", [
                _moment(0.0, 4.0, energy=7.5),
                _moment(4.0, 8.0, energy=6.5),
                _moment(8.0, 12.0, energy=5.5),
            ]),
        ]

        plan = match(recipe, clips)

        from collections import Counter

        counts = Counter(s.clip_id for s in plan.steps)
        assert sum(counts.values()) == 5
        assert set(counts.keys()) == {"clip_a", "clip_b"}
        # max_uses=3, each clip used 2 or 3 times
        for clip_id, n in counts.items():
            assert 2 <= n <= 3, (
                f"clip {clip_id} used {n} times — expected 2 or 3 with "
                f"supply=2 demand=5 max_uses=3"
            )

    def test_variety_relaxation_emits_log_when_fired(self, monkeypatch):
        """Operators need to see when the matcher reaches for the
        duration-relaxed unused-clip pool. Frequent firing suggests Gemini
        moment-extraction is producing weird durations and warrants a
        separate look. Monkeypatches the module logger because the
        AsyncBoundLogger created at import time bypasses caplog/capture_logs
        in the default pytest configuration."""
        from app.pipeline import template_matcher

        recorded: list[tuple[str, dict]] = []

        class _Recorder:
            def info(self, event, **kwargs):
                recorded.append((event, kwargs))

            def debug(self, *args, **kwargs):
                pass

            def warning(self, *args, **kwargs):
                pass

        monkeypatch.setattr(template_matcher, "log", _Recorder())

        recipe = _make_recipe([
            _slot(1, 4.0, priority=10, slot_type="hook"),
            _slot(2, 4.0, priority=9, slot_type="broll"),
        ])
        # Setup forces the variety-relaxed branch on slot 2:
        # clip_a fits perfectly, clip_b's moment is far outside tolerance.
        clips = [
            _make_clip("clip_a", [_moment(0.0, 4.0, energy=8.0)]),
            _make_clip("clip_b", [_moment(0.0, 15.0, energy=7.0)]),
        ]

        plan = match(recipe, clips)

        relaxed_events = [
            (event, kwargs)
            for event, kwargs in recorded
            if event == "matcher_relaxed_duration_for_variety"
        ]
        assert relaxed_events, (
            "Expected matcher_relaxed_duration_for_variety log when "
            "capped_candidates is empty but unused clips exist. Recorded: "
            f"{[e[0] for e in recorded]}"
        )
        # Sanity-check the structured kwargs carry the slot context
        _, kwargs = relaxed_events[0]
        assert "slot_position" in kwargs
        assert "n_unused" in kwargs and kwargs["n_unused"] >= 1

        # And the plan still came out correct — both clips used exactly once
        clip_ids_used = sorted(s.clip_id for s in plan.steps)
        assert clip_ids_used == ["clip_a", "clip_b"]

    def test_pinned_clip_with_mismatched_unpinned_durations(self):
        """Production-realistic combo: hook-pinned slot + supply >= demand +
        duration mismatch on the unpinned clips. Templates like Just Fine
        auto-pin clip 0 to slot 1 in template_orchestrate.py:678. If the
        other uploaded clips have moments outside the duration tolerance,
        the relaxed_unused branch must STILL exclude the pinned clip (via
        greedy_metas filter at template_matcher.py:667) and fill the
        remaining slots from the four unpinned-but-mismatched clips
        without reusing the pinned one.

        Pre-fix this scenario reused clip_0 (the pinned hook) across
        slots 2-5 because it was the only clip whose moment fit ±6s,
        and the unpinned clips' moments got rejected by the duration
        filter before they could be considered.
        """
        recipe = _make_recipe([
            _slot(1, 4.0, priority=10, slot_type="hook"),
            _slot(2, 5.0, priority=8, slot_type="broll"),
            _slot(3, 5.0, priority=7, slot_type="broll"),
            _slot(4, 5.0, priority=6, slot_type="broll"),
            _slot(5, 5.0, priority=5, slot_type="outro"),
        ])
        # clip_0 (pinned to slot 1): has a 4s moment that fits slot 1
        # and would also fit slots 2-5 if unpinned. The other four clips
        # have 15s moments — outside the ±6s tolerance for the 5s slots.
        clips = [
            _make_clip("clip_0", [_moment(0.0, 4.0, energy=9.0)]),
            _make_clip("clip_a", [_moment(0.0, 15.0, energy=7.0)]),
            _make_clip("clip_b", [_moment(0.0, 15.0, energy=7.0)]),
            _make_clip("clip_c", [_moment(0.0, 15.0, energy=7.0)]),
            _make_clip("clip_d", [_moment(0.0, 15.0, energy=7.0)]),
        ]

        plan = match(recipe, clips, pinned_assignments={1: "clip_0"})

        # Pinned clip stayed in slot 1
        slot_1 = next(s for s in plan.steps if s.slot["position"] == 1)
        assert slot_1.clip_id == "clip_0"

        # Slots 2-5 each used a different unpinned clip, no reuse
        other_slot_clip_ids = sorted(
            s.clip_id for s in plan.steps if s.slot["position"] != 1
        )
        assert other_slot_clip_ids == ["clip_a", "clip_b", "clip_c", "clip_d"], (
            f"Pinned clip_0 leaked into a non-pinned slot OR the unpinned "
            f"clips were not all used. Got: {other_slot_clip_ids}"
        )

        # And clip_0 only appears once total
        all_clip_ids = [s.clip_id for s in plan.steps]
        assert all_clip_ids.count("clip_0") == 1, (
            f"Pinned clip_0 used more than once: {all_clip_ids}"
        )


# ── Locked hook slot regression (Waka Waka / Morocco) ────────────────────────


class TestLockedHookSlot:
    """Regression for TEMPLATE_PIN_INVALID_SLOT on Waka Waka (formerly Morocco).

    The template has slots 1+2 locked (slot_type="hook"); the orchestrator
    used to blindly pin clip 0 to slot 1 because the type matched, and the
    matcher then rejected the pin since locked slots are excluded from
    unlocked_slots. The orchestrator now skips pinning for locked slots, so
    match() must accept this shape (no pin, locked step emitted directly).
    """

    def test_locked_hook_slot_1_emits_locked_step_without_pin(self):
        """Slot 1 locked (hook), slot 2 unlocked. With no pin (orchestrator's
        new behavior), match emits LOCKED_TEMPLATE_CLIP_ID for slot 1 and
        fills slot 2 from a user clip."""
        locked_slot = _slot(1, 1.3, priority=8, slot_type="hook")
        locked_slot["locked"] = True
        locked_slot["source_start_s"] = 0.0
        locked_slot["source_end_s"] = 1.3
        recipe = _make_recipe([
            locked_slot,
            _slot(2, 5.0, priority=5, slot_type="broll"),
        ])
        clip = _make_clip("clip_0", [_moment(0.0, 5.0, energy=7.0)])

        plan = match(recipe, [clip])

        assert len(plan.steps) == 2
        slot1 = next(s for s in plan.steps if s.slot["position"] == 1)
        slot2 = next(s for s in plan.steps if s.slot["position"] == 2)
        assert slot1.clip_id == LOCKED_TEMPLATE_CLIP_ID
        assert slot1.moment["start_s"] == 0.0
        assert slot1.moment["end_s"] == 1.3
        assert slot2.clip_id == "clip_0"

    def test_pin_to_locked_slot_still_raises(self):
        """Matcher contract stays loud: if the orchestrator (or any caller)
        does try to pin to a locked slot, the matcher must refuse so the
        programmer error surfaces instead of silently dropping the pin."""
        locked_slot = _slot(1, 1.3, priority=8, slot_type="hook")
        locked_slot["locked"] = True
        locked_slot["source_start_s"] = 0.0
        locked_slot["source_end_s"] = 1.3
        recipe = _make_recipe([
            locked_slot,
            _slot(2, 5.0, priority=5, slot_type="broll"),
        ])
        clip = _make_clip("clip_0", [_moment(0.0, 5.0, energy=7.0)])

        with pytest.raises(TemplateMismatchError) as exc_info:
            match(recipe, [clip], pinned_assignments={1: "clip_0"})

        assert exc_info.value.code == "TEMPLATE_PIN_INVALID_SLOT"


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


# ── Consolidate slots tests ─────────────────────────────────────────────────


def _slot_with_overlays(
    position: int,
    target_dur: float,
    priority: int = 5,
    slot_type: str = "broll",
    energy: float = 5.0,
    text_overlays: list[dict] | None = None,
) -> dict:
    """Slot helper that also accepts energy and text_overlays."""
    s = _slot(position, target_dur, priority, slot_type)
    s["energy"] = energy
    if text_overlays is not None:
        s["text_overlays"] = text_overlays
    return s


class TestConsolidateSlots:
    """Tests for consolidate_slots() — adaptive slot merging."""

    def test_t1_noop_when_clips_gte_slots(self):
        """T1: n_clips >= n_slots → recipe returned unchanged."""
        recipe = _make_recipe([
            _slot(1, 5.0, slot_type="hook"),
            _slot(2, 5.0),
            _slot(3, 5.0),
        ])
        clips = [
            _make_clip("c1", [_moment(0, 5)]),
            _make_clip("c2", [_moment(0, 5)]),
            _make_clip("c3", [_moment(0, 5)]),
        ]

        result = consolidate_slots(recipe, clips)

        assert result is recipe  # identity — no copy made

    def test_t2_basic_merge_2_clips_4_broll(self):
        """T2: 2 clips, 4 broll slots → merged down to 2 slots."""
        recipe = _make_recipe([
            _slot(1, 5.0, priority=5),
            _slot(2, 5.0, priority=4),
            _slot(3, 5.0, priority=3),
            _slot(4, 5.0, priority=2),
        ])
        clips = [
            _make_clip("c1", [_moment(0, 10, energy=7.0)]),
            _make_clip("c2", [_moment(0, 10, energy=6.0)]),
        ]

        result = consolidate_slots(recipe, clips)

        assert len(result.slots) == 2
        assert result.shot_count == 2
        # Total duration preserved
        assert result.total_duration_s == pytest.approx(20.0)
        total_dur = sum(float(s["target_duration_s"]) for s in result.slots)
        assert total_dur == pytest.approx(20.0)

    def test_t3_hook_never_merged(self):
        """T3: Hook at position 1 is never merged with adjacent slot."""
        recipe = _make_recipe([
            _slot(1, 3.0, priority=10, slot_type="hook"),
            _slot(2, 5.0, priority=5),
            _slot(3, 5.0, priority=4),
            _slot(4, 5.0, priority=3),
        ])
        clips = [
            _make_clip("c1", [_moment(0, 5)]),
            _make_clip("c2", [_moment(0, 10)]),
        ]

        result = consolidate_slots(recipe, clips)

        # Hook must survive as its own slot
        hook_slots = [s for s in result.slots if s.get("slot_type") == "hook"]
        assert len(hook_slots) == 1
        assert float(hook_slots[0]["target_duration_s"]) == pytest.approx(3.0)

    def test_t3b_multi_hook_slots_merge_together(self):
        """T3b: Multiple hook slots merge with each other but not with broll."""
        recipe = _make_recipe([
            _slot(1, 0.7, priority=8, slot_type="hook"),
            _slot(2, 0.7, priority=8, slot_type="hook"),
            _slot(3, 0.9, priority=7, slot_type="hook"),
            _slot(4, 0.9, priority=7, slot_type="hook"),
            _slot(5, 0.9, priority=7, slot_type="hook"),
            _slot(6, 0.8, priority=8, slot_type="broll"),
            _slot(7, 0.9, priority=8, slot_type="broll"),
            _slot(8, 1.7, priority=9, slot_type="broll"),
            _slot(9, 1.3, priority=9, slot_type="broll"),
            _slot(10, 2.5, priority=9, slot_type="broll"),
            _slot(11, 2.5, priority=9, slot_type="broll"),
        ])
        clips = [
            _make_clip("c1", [_moment(0, 2, energy=9.0)]),
            _make_clip("c2", [_moment(0, 5, energy=6.0)]),
            _make_clip("c3", [_moment(0, 5, energy=7.0)]),
            _make_clip("c4", [_moment(0, 4, energy=7.5)]),
            _make_clip("c5", [_moment(0, 5, energy=6.5)]),
        ]

        result = consolidate_slots(recipe, clips)

        # 5 clips → target 5 slots. Hook-hook merges allowed, hook-broll blocked.
        assert len(result.slots) <= 5
        # Hooks merged together, brolls merged together
        hook_slots = [s for s in result.slots if s["slot_type"] == "hook"]
        # Fewer hooks than original 5 (hooks merged together)
        assert len(hook_slots) < 5
        # Total hook duration preserved
        original_hook_dur = 0.7 + 0.7 + 0.9 + 0.9 + 0.9  # 4.1s
        merged_hook_dur = sum(float(h["target_duration_s"]) for h in hook_slots)
        assert merged_hook_dur == pytest.approx(original_hook_dur)

    def test_t4_min_floor_distinct_types(self):
        """T4: Distinct slot types preserved — hook+broll+outro → min 3 even with 1 clip."""
        recipe = _make_recipe([
            _slot(1, 3.0, slot_type="hook"),
            _slot(2, 5.0, slot_type="broll"),
            _slot(3, 5.0, slot_type="broll"),
            _slot(4, 5.0, slot_type="broll"),
            _slot(5, 4.0, slot_type="outro"),
        ])
        clips = [
            _make_clip("c1", [_moment(0, 15, energy=6.0)]),
        ]

        result = consolidate_slots(recipe, clips)

        # Floor = max(1 clip, 3 types, 2 min) = 3
        assert len(result.slots) == 3

    def test_t5_single_type_floor_is_n_clips(self):
        """T5: All broll → floor = max(n_clips, 1 type, 2) = n_clips."""
        recipe = _make_recipe([
            _slot(1, 5.0), _slot(2, 5.0), _slot(3, 5.0),
            _slot(4, 5.0), _slot(5, 5.0), _slot(6, 5.0),
        ])
        clips = [
            _make_clip("c1", [_moment(0, 10)]),
            _make_clip("c2", [_moment(0, 10)]),
            _make_clip("c3", [_moment(0, 10)]),
        ]

        result = consolidate_slots(recipe, clips)

        assert len(result.slots) == 3

    def test_t6_max_duration_cap_skips_merge(self):
        """T6: Two 15s slots → combined 30s > MAX_MERGED_DURATION_S → skip."""
        recipe = _make_recipe([
            _slot(1, 15.0, priority=5),
            _slot(2, 15.0, priority=4),
            _slot(3, 5.0, priority=3),
        ])
        clips = [
            _make_clip("c1", [_moment(0, 15)]),
        ]

        result = consolidate_slots(recipe, clips)

        # No slot should exceed the cap
        durations = [float(s["target_duration_s"]) for s in result.slots]
        for d in durations:
            assert d < MAX_MERGED_DURATION_S

    def test_t7_clip_aware_scoring_prefers_matching_duration(self):
        """T7: Prefers merge whose combined duration matches a clip moment."""
        recipe = _make_recipe([
            _slot(1, 3.0, priority=5),   # pair 1-2: 3+3=6s
            _slot(2, 3.0, priority=4),
            _slot(3, 5.0, priority=3),   # pair 3-4: 5+5=10s
            _slot(4, 5.0, priority=2),
        ])
        # Only 10s clip moments — pair (3,4)=10s gets exact fit score 3.0,
        # pair (1,2)=6s gets partial fit score ~1.0 (|10-6|=4, 3*(1-4/6))
        clips = [
            _make_clip("c1", [_moment(0, 10, energy=7.0)]),
            _make_clip("c2", [_moment(0, 10, energy=6.0)]),
            _make_clip("c3", [_moment(0, 3, energy=5.0)]),
        ]

        result = consolidate_slots(recipe, clips)

        assert len(result.slots) == 3
        # Pair (3,4) merged → one 10s slot exists
        durations = sorted(float(s["target_duration_s"]) for s in result.slots)
        assert 10.0 in durations

    def test_t8_interstitial_penalty(self):
        """T8: Prefers merging non-interstitial boundary over one with interstitial."""
        recipe = TemplateRecipe(
            shot_count=4,
            total_duration_s=20.0,
            hook_duration_s=3.0,
            slots=[
                _slot(1, 5.0, priority=5),
                _slot(2, 5.0, priority=4),
                _slot(3, 5.0, priority=3),
                _slot(4, 5.0, priority=2),
            ],
            copy_tone="casual",
            caption_style="bold",
            interstitials=[{"type": "fade", "after_slot": 1}],
        )
        clips = [
            _make_clip("c1", [_moment(0, 10)]),
            _make_clip("c2", [_moment(0, 10)]),
            _make_clip("c3", [_moment(0, 5)]),
        ]

        result = consolidate_slots(recipe, clips)

        assert len(result.slots) == 3
        # The interstitial should survive (penalty prevents that merge)
        assert len(result.interstitials) >= 1

    def test_t9_interior_interstitial_dropped(self):
        """T9: Interstitial between merged slots is dropped."""
        recipe = TemplateRecipe(
            shot_count=4,
            total_duration_s=20.0,
            hook_duration_s=3.0,
            slots=[
                _slot(1, 5.0, priority=5),
                _slot(2, 5.0, priority=4),
                _slot(3, 5.0, priority=3),
                _slot(4, 5.0, priority=2),
            ],
            copy_tone="casual",
            caption_style="bold",
            interstitials=[{"type": "fade", "after_slot": 2}],
        )
        clips = [
            _make_clip("c1", [_moment(0, 10)]),
            _make_clip("c2", [_moment(0, 10)]),
        ]

        result = consolidate_slots(recipe, clips)

        assert len(result.slots) == 2
        # All remaining interstitials have valid positions
        for inter in result.interstitials:
            after = inter.get("after_slot", 0)
            assert after <= len(result.slots)

    def test_t10_tail_interstitial_remapped(self):
        """T10: Interstitial after second slot of merged pair remaps correctly."""
        recipe = TemplateRecipe(
            shot_count=4,
            total_duration_s=20.0,
            hook_duration_s=3.0,
            slots=[
                _slot(1, 5.0, priority=5),
                _slot(2, 5.0, priority=4),
                _slot(3, 5.0, priority=3),
                _slot(4, 5.0, priority=2),
            ],
            copy_tone="casual",
            caption_style="bold",
            interstitials=[{"type": "fade", "after_slot": 3}],
        )
        clips = [
            _make_clip("c1", [_moment(0, 10)]),
            _make_clip("c2", [_moment(0, 5)]),
            _make_clip("c3", [_moment(0, 5)]),
        ]

        result = consolidate_slots(recipe, clips)

        assert len(result.slots) == 3
        assert len(result.interstitials) >= 1
        for inter in result.interstitials:
            assert 1 <= inter["after_slot"] <= len(result.slots)

    def test_t11_positions_renumbered_sequentially(self):
        """T11: After merges, positions are sequential 1..N."""
        recipe = _make_recipe([
            _slot(1, 5.0), _slot(2, 5.0), _slot(3, 5.0), _slot(4, 5.0),
        ])
        clips = [
            _make_clip("c1", [_moment(0, 10)]),
            _make_clip("c2", [_moment(0, 10)]),
        ]

        result = consolidate_slots(recipe, clips)

        positions = [s["position"] for s in result.slots]
        assert positions == list(range(1, len(result.slots) + 1))

    def test_t12_text_overlay_time_shifting(self):
        """T12: Slot B overlays get dur_A offset when merged."""
        recipe = _make_recipe([
            _slot_with_overlays(1, 5.0, text_overlays=[
                {"text": "hello", "position": "center", "start_s": 0.0, "end_s": 3.0},
            ]),
            _slot_with_overlays(2, 5.0, text_overlays=[
                {"text": "world", "position": "bottom", "start_s": 1.0, "end_s": 4.0},
            ]),
            _slot_with_overlays(3, 5.0),
        ])
        clips = [
            _make_clip("c1", [_moment(0, 10)]),
            _make_clip("c2", [_moment(0, 5)]),
        ]

        result = consolidate_slots(recipe, clips)

        # Find the merged slot (should have ~10s duration)
        merged = next(
            s for s in result.slots
            if float(s["target_duration_s"]) == pytest.approx(10.0)
        )
        overlays = merged.get("text_overlays", [])
        assert len(overlays) == 2

        # First overlay (from slot A) unchanged
        hello_ov = next(o for o in overlays if o["text"] == "hello")
        assert hello_ov["start_s"] == pytest.approx(0.0)
        assert hello_ov["end_s"] == pytest.approx(3.0)

        # Second overlay (from slot B) shifted by dur_A=5.0
        world_ov = next(o for o in overlays if o["text"] == "world")
        assert world_ov["start_s"] == pytest.approx(6.0)  # 1.0 + 5.0
        assert world_ov["end_s"] == pytest.approx(9.0)    # 4.0 + 5.0

    def test_t13_integration_consolidate_then_match_no_repetition(self):
        """T13: consolidate → match produces plan where all clips are used."""
        # Use shorter broll slots (3s each) so merges stay under the 20s cap.
        recipe = _make_recipe([
            _slot(1, 3.0, priority=10, slot_type="hook"),
            _slot(2, 3.0, priority=5),
            _slot(3, 3.0, priority=4),
            _slot(4, 3.0, priority=3),
            _slot(5, 3.0, priority=2),
            _slot(6, 3.0, priority=1),
            _slot(7, 3.0, priority=1),
            _slot(8, 3.0, priority=1),
        ])
        # Each clip provides moments spanning a wide duration range.
        clips = [
            _make_clip("c1", [
                _moment(0, 3, energy=9.0), _moment(0, 12, energy=7.0),
            ]),
            _make_clip("c2", [
                _moment(0, 12, energy=6.0), _moment(0, 9, energy=5.5),
            ]),
            _make_clip("c3", [
                _moment(0, 12, energy=7.5), _moment(0, 9, energy=6.0),
            ]),
        ]

        consolidated = consolidate_slots(recipe, clips)
        plan = match(consolidated, clips)

        clip_ids = [step.clip_id for step in plan.steps]
        assert len(clip_ids) == len(consolidated.slots)
        # All 3 clips should be used (no clip left out)
        assert len(set(clip_ids)) == 3, (
            f"Expected all 3 clips used, got: {clip_ids}"
        )

    def test_t14_regression_3clips_11slots_no_excessive_repetition(self):
        """T14: Regression — 3 clips + 11 slots (passport vlog scenario).

        Without consolidation, max_uses = ceil(11/3) = 4, so each clip repeats
        3-4 times. With consolidation, slots merge down toward 3, so each clip
        appears at most twice (limited by MAX_MERGED_DURATION_S).
        """
        recipe = _make_recipe([
            # 5 hook slots + 6 broll slots, mimicking the passport vlog template
            _slot(1, 3.0, priority=10, slot_type="hook"),
            _slot(2, 3.0, priority=9, slot_type="hook"),
            _slot(3, 3.0, priority=8, slot_type="hook"),
            _slot(4, 3.0, priority=7, slot_type="hook"),
            _slot(5, 3.0, priority=6, slot_type="hook"),
            _slot(6, 3.0, priority=5),
            _slot(7, 3.0, priority=4),
            _slot(8, 3.0, priority=3),
            _slot(9, 3.0, priority=2),
            _slot(10, 3.0, priority=1),
            _slot(11, 3.0, priority=1),
        ])
        clips = [
            _make_clip("c1", [
                _moment(0, 3, energy=9.0), _moment(0, 15, energy=7.0),
            ]),
            _make_clip("c2", [
                _moment(0, 15, energy=6.0), _moment(0, 10, energy=5.5),
            ]),
            _make_clip("c3", [
                _moment(0, 15, energy=7.5), _moment(0, 10, energy=6.0),
            ]),
        ]

        consolidated = consolidate_slots(recipe, clips)
        plan = match(consolidated, clips)

        clip_ids = [step.clip_id for step in plan.steps]
        # All 3 clips must be used
        assert set(clip_ids) == {"c1", "c2", "c3"}, (
            f"Expected all 3 clips used, got unique: {set(clip_ids)}"
        )
        # No clip should appear more than twice (ideally once after full consolidation)
        from collections import Counter
        counts = Counter(clip_ids)
        for clip_id, count in counts.items():
            assert count <= 2, (
                f"Clip {clip_id} appears {count} times — consolidation should prevent this"
            )
