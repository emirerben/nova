"""KRI-847: the length plan treats a model-recorded "at most N seconds" as a cap.

"at_most" only removes the LOWER side of the existing +/- 10 % check, so the plan can relax
what exact already allowed and never tighten it.

Failure modes written first:

1. Footage that is shorter than the ceiling (7.5 s of clips under "at most 10 seconds") is an
   ``insufficient_capacity`` conflict, and the creator is asked about a plan that already
   satisfies the ask.
2. A ceiling is stricter than exact (a floor exact lifts or accepts inside +10 % is refused
   for a ceiling), or looser (a floor really above the tolerance is quietly accepted).
3. An exact length changes behaviour (every exact assertion below is a guard).
4. The frozen plan does not say it is a ceiling, so the render side cannot tell.
"""

from __future__ import annotations

import pytest

from app.services.length_planning import build_length_plan

MONTAGE = {"edit_format": "montage", "audio_strategy": "original_audio"}
HERO = {"edit_format": "single_hero", "audio_strategy": "original_audio"}


def _codes(plan: dict) -> list[str]:
    return [conflict["code"] for conflict in plan["conflicts"]]


def _plan(strategy, rows, target, bound=None):
    kwargs = {"creator_target_bound": bound} if bound else {}
    snapshot = {"clip_assignments": rows}
    return build_length_plan(strategy, snapshot, creator_target_s=target, **kwargs)


# --------------------------------------------------------- footage shorter than the ceiling


def test_footage_shorter_than_the_ceiling_is_not_a_conflict():
    plan = _plan(MONTAGE, [{"media_id": "a", "duration_s": 7.5}], 10.0, "at_most")

    assert plan["conflicts"] == []
    assert plan["windows"][0]["source_end_s"] == 7.5  # all of it, never stretched
    assert plan["target_duration_s"] == 10.0
    assert plan["target_duration_bound"] == "at_most"


def test_guard_the_same_footage_under_an_exact_length_is_still_a_conflict():
    plan = _plan(MONTAGE, [{"media_id": "a", "duration_s": 7.5}], 10.0)

    assert "insufficient_capacity" in _codes(plan)
    assert "target_duration_bound" not in plan  # the frozen plan of an exact ask is unchanged


def test_footage_longer_than_the_ceiling_is_cut_to_it():
    plan = _plan(MONTAGE, [{"media_id": "a", "duration_s": 20}], 10.0, "at_most")

    assert plan["conflicts"] == []
    assert plan["windows"][0]["source_end_s"] - plan["windows"][0]["source_start_s"] == 10.0


def test_a_ceiling_with_footage_too_short_to_be_a_video_still_conflicts():
    """A 2 s result is below the 3 s minimum whatever the ceiling says."""
    plan = _plan(MONTAGE, [{"media_id": "a", "duration_s": 2.0}], 10.0, "at_most")

    assert plan["conflicts"], plan


def test_single_hero_capacity_under_the_ceiling_is_not_a_conflict():
    rows = [
        {"media_id": "hero", "duration_s": 5, "hook_score": 9},
        {"media_id": "support", "duration_s": 20, "hook_score": 4},
    ]

    ceiling = _plan(HERO, rows, 20, "at_most")
    exact = _plan(HERO, rows, 20)

    assert ceiling["conflicts"] == []
    assert ceiling["capacity_raw_s"] == pytest.approx(8.333)
    assert "insufficient_capacity" in _codes(exact)  # guard


# ------------------------------------------- floors use the exact upper tolerance, both ways


def test_a_composition_floor_inside_the_exact_tolerance_is_accepted_for_a_ceiling_too():
    """One 3 s hero and a 0.1 s cutaway are 3.1 s: inside "3 s + 10 %" for exact and at_most."""
    rows = [
        {"media_id": "hero", "duration_s": 20, "analysis": {"hook_score": 9}},
        {"media_id": "support", "duration_s": 10, "analysis": {"hook_score": 4}},
    ]

    ceiling = _plan(HERO, rows, 3, "at_most")
    exact = _plan(HERO, rows, 3)

    assert ceiling["conflicts"] == []
    assert ceiling["capacity_raw_s"] == 3.1
    assert exact["conflicts"] == [] and exact["capacity_raw_s"] == 3.1  # guard: same plan


def test_a_composition_floor_above_the_tolerance_is_a_conflict_for_both():
    rows = [
        {"media_id": "hero", "duration_s": 20, "analysis": {"hook_score": 9}},
        {"media_id": "support", "duration_s": 10, "analysis": {"hook_score": 4}},
    ]

    ceiling = _plan(HERO, rows, 2.5, "at_most")
    exact = _plan(HERO, rows, 2.5)

    assert ceiling["conflicts"], ceiling
    assert exact["conflicts"], exact  # an exact 2.5 s cannot hold a 3.1 s composition either


def test_a_coverage_floor_inside_ten_percent_is_lifted_into_for_a_ceiling_as_for_exact():
    """40 clips need 0.1 s each (4.0 s): 3.8 s + 10 % reaches it, so both plans lift to 4.0 s.
    A ceiling is never stricter than exact here; only its shortfall side is relaxed."""
    rows = [{"media_id": f"c{i}", "duration_s": 5} for i in range(40)]

    exact = _plan(MONTAGE, rows, 3.8)
    ceiling = _plan(MONTAGE, rows, 3.8, "at_most")

    for plan in (exact, ceiling):
        assert plan["conflicts"] == []
        assert sum(w["source_end_s"] - w["source_start_s"] for w in plan["windows"]) == (
            pytest.approx(4.0)
        )


def test_a_coverage_floor_past_ten_percent_is_a_conflict_for_both():
    rows = [{"media_id": f"c{i}", "duration_s": 5} for i in range(40)]

    assert _plan(MONTAGE, rows, 3.5)["conflicts"]
    assert _plan(MONTAGE, rows, 3.5, "at_most")["conflicts"]


# ------------------------------- through the planner's own seam (brief -> frozen envelope)


def _brief_with_length(**facts):
    from app.kria.brief import BriefRequirement, CreativeBrief

    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="global",
                description="At most 10 seconds",
                facts={"duration_s": 10, **facts},
            )
        ],
    )


def _enriched(brief) -> dict:
    from app.kria.planner import _enrich_best_parts_snapshot

    snapshot = {
        "clip_assignments": [
            {"media_id": "a", "generation": "1", "gcs_path": "a.mp4", "duration_s": 7.5}
        ]
    }
    enriched = _enrich_best_parts_snapshot(MONTAGE, snapshot, brief)
    assert enriched is not None
    return enriched["length_plan"]


def test_the_planner_freezes_a_brief_ceiling_as_a_ceiling():
    plan = _enriched(_brief_with_length(duration_bound="at_most"))

    assert plan["conflicts"] == []
    assert plan["target_duration_s"] == 10
    assert plan["target_duration_bound"] == "at_most"


def test_guard_the_planner_freezes_the_same_brief_without_the_fact_as_a_shortfall():
    plan = _enriched(_brief_with_length())

    assert "insufficient_capacity" in _codes(plan)
    assert "target_duration_bound" not in plan
