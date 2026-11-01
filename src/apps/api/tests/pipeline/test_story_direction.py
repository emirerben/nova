"""Failure-mode guards for the text-explainer unit (a planner direction built as a story).

Written from the ways building a text_explainer / guided_story on the phone montage route can
still hurt a creator, before the code: the build must never fail or block a render, never drop a
selected clip, never move a stated length, never lose an ask only the fast-cut layout can
express, never ask a phone for a capability it lacks, and the kill switch must give back exactly
the plan the route made before. The end-to-end proof is the Assos scorecard
(`scripts/kria/score_assos.py`, checks 2 and 4); these tests pin the failure modes the replay
does not reach.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.route_disclosure import UNIFIED_MONTAGE, card_disclosures
from app.pipeline.guided_story import compile_proposal_execution_plan
from app.pipeline.story_direction import (
    says_fallback,
    story_blocker,
    story_floor_for,
    story_media_blocker,
)
from app.pipeline.unified_montage import BriefView, UnifiedClip, brief_view, plan_unified_montage
from app.schemas.edit_proposal import EditProposalSnapshot
from app.services.creation_text_composition import (
    _digest,
    _drop_unused_story_seeds,
    _lanes,
    _plays_entrance,
)
from app.services.length_planning import build_length_plan
from app.services.length_policy import story_hold_floor_s
from app.tasks.generative_build import _composed_plan

OUTLINE = ["Arrive at the coast", "Walk through the ruins", "End on the sea"]
STRATEGY = {
    "edit_format": "montage",
    "direction": "text_explainer",
    "pacing": "balanced",
    "story_structure": OUTLINE,
}


@pytest.fixture(autouse=True)
def _phone_draws_crossfades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_render_verified_features", ["crossfade"])


# Every clip's strongest moment is its first 4 s of 6: a window never uses a clip whole, so the
# crossfade overlap each moment pays comes out of footage that is there (as in analysed prod clips).
ANALYSIS = {"best_moments": [{"start_s": 0.0, "end_s": 4.0, "energy": 5.0}]}


def _clips(count: int = 12, duration_s: float = 6.0) -> list[UnifiedClip]:
    return [
        UnifiedClip(
            media_id=f"c{i}",
            proxy_path=f"c{i}.mp4",
            generation="1",
            duration_s=duration_s,
            analysis=ANALYSIS,
        )
        for i in range(count)
    ]


def _media(count: int = 12, duration_s: float = 6.0) -> dict:
    return {
        "clip_assignments": [
            {
                "media_id": f"c{i}",
                "gcs_path": f"c{i}.mp4",
                "generation": "1",
                "duration_s": duration_s,
                "hook_score": 1.0,
                "analysis": ANALYSIS,
            }
            for i in range(count)
        ]
    }


def _approved(strategy: dict, count: int = 12, **kwargs) -> dict:
    """The length plan the draft pins, planned with the same floor the card plans with."""
    return build_length_plan(
        strategy, _media(count), story_floor_s=story_hold_floor_s(strategy), **kwargs
    )


def _plan(strategy: dict, *, count: int = 12, approved: dict | None = None, **kwargs):
    return plan_unified_montage(
        _clips(count),
        kwargs.pop("view", None),
        strategy=strategy,
        approved_length_plan=approved if approved is not None else _approved(strategy, count),
        **kwargs,
    )


def test_a_text_explainer_is_story_beats_over_every_clip_held_at_the_minimum() -> None:
    song = {
        "audio_strategy": "licensed_music",
        "selected_music_section": {"start_s": 0, "end_s": 9},
    }
    strategy = {**STRATEGY, **song}
    plan = _plan(strategy)
    assert plan.snapshot.direction == "text_explainer" and plan.direction_fallback is None
    assert not plan.snapshot.fast_cuts and not plan.snapshot.clip_labels
    compiled = compile_proposal_execution_plan(plan.snapshot)
    # Premise: nobody stated 9 s, so the song section the phone never plays yields to the holds;
    # no clip the creator selected is dropped.
    assert [m["media_id"] for m in compiled["story_timeline"]] == [f"c{i}" for i in range(12)]
    assert min(m["duration_s"] for m in compiled["story_timeline"]) >= 1.8 - 0.01
    assert plan.record()["story"] == {"direction": "text_explainer", "beats": 3}
    assert len(plan.snapshot.story_beats) == 3 and all(
        len(beat.media_ids) <= 4 for beat in plan.snapshot.story_beats
    )


def test_a_stated_length_the_holds_cannot_fit_keeps_the_length_and_the_fast_montage() -> None:
    approved = _approved(STRATEGY, creator_target_s=10.0)
    plan = _plan(STRATEGY, approved=approved, creator_duration_proven=True)
    assert plan.snapshot.direction == "fast_montage"
    assert plan.direction_fallback == "holds_do_not_fit"
    assert plan.snapshot.duration_s == pytest.approx(10.0, abs=0.1)
    assert says_fallback(plan.direction_fallback, "text_explainer")
    assert not says_fallback(plan.direction_fallback, "guided_story")


@pytest.mark.parametrize(
    ("change", "view", "reason"),
    [
        ({"shot_labels": ["DAY 1", "DAY 2"]}, None, "per_clip_text"),
        ({}, BriefView(wants_per_clip_text=True), "per_clip_text"),
        ({"video_reuse_policy": "allow_repeat"}, None, "photo_timing"),
        ({"mixed_media_timing": {"image_hold": "very_fast"}}, None, "photo_timing"),
        ({}, BriefView(curved_title=True), "curved_text"),
    ],
)
def test_an_ask_only_the_fast_cut_layout_expresses_keeps_the_fast_montage(
    change: dict, view: BriefView | None, reason: str
) -> None:
    strategy = {**STRATEGY, **change}
    assert story_blocker(strategy, view or BriefView()) == reason
    plan = _plan(strategy, view=view)
    assert plan.snapshot.direction == "fast_montage"
    assert plan.direction_fallback == reason


def test_a_creator_song_is_never_rebuilt_as_a_story() -> None:
    plan = plan_unified_montage(
        _clips(),
        strategy=STRATEGY,
        song_duration_s=40.0,
        song_plan_item_id="item",
        song_generation=1,
    )
    assert plan.snapshot.direction == "fast_montage" and plan.story_beats is None
    assert plan.snapshot.user_song is not None


def test_a_phone_without_crossfade_gets_the_fast_montage(monkeypatch: pytest.MonkeyPatch) -> None:
    assert story_floor_for(STRATEGY, BriefView()) == 1.8
    monkeypatch.setattr(settings, "phone_render_verified_features", [])
    # Planning does not hold clips longer for a layout this phone cannot be asked for.
    assert story_floor_for(STRATEGY, BriefView()) is None
    plan = _plan(STRATEGY)
    assert plan.snapshot.direction == "fast_montage"
    assert plan.direction_fallback == "crossfade_unavailable"


def test_the_kill_switch_gives_back_the_plan_the_route_made_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "story_directions_on_montage_enabled", False)
    off = _plan({**STRATEGY, "direction": "text_explainer"}, approved=_approved(STRATEGY))
    fast = _plan({**STRATEGY, "direction": "fast_montage"}, approved=_approved(STRATEGY))
    assert story_hold_floor_s(STRATEGY) is None
    assert off.story_beats is None and off.direction_fallback is None
    assert off.snapshot.model_dump(mode="json") == fast.snapshot.model_dump(mode="json")
    record = {k: v for k, v in off.record().items() if k != "requested_direction"}
    assert record == fast.record()
    # And approval-time planning is the plain plan: no floor, no `story` marker.
    assert "story" not in _approved(STRATEGY)


def test_when_the_story_text_cannot_be_placed_the_worker_composes_the_fast_montage() -> None:
    plan = _plan(STRATEGY)
    assert plan.story_fallback is not None
    composed: list[str] = []

    def compose(candidate):  # noqa: ANN001, ANN202
        composed.append(candidate.snapshot.direction)
        if candidate.snapshot.direction != "fast_montage":
            raise ValueError("The explanation overlaps protected text; the edit is unchanged")
        return candidate.snapshot

    result = _composed_plan(plan, [], {}, compose)
    assert result is not None and result.snapshot.direction == "fast_montage"
    assert result.direction_fallback == "story_text_failed"
    assert composed == ["text_explainer", "fast_montage"]

    # A provider timeout is not the story's doing: the fast montage would hit it too.
    from app.services.creation_text_composition import CreationTextTimeoutError

    def timeout(_candidate):  # noqa: ANN001, ANN202
        raise CreationTextTimeoutError("timed out", slot_count=12)

    with pytest.raises(CreationTextTimeoutError):
        _composed_plan(plan, [], {}, timeout)


def test_the_soft_time_limit_in_the_story_text_pass_is_not_swallowed_as_a_fallback() -> None:
    from billiard.exceptions import SoftTimeLimitExceeded

    plan = _plan(STRATEGY)
    composed: list[str] = []

    def out_of_time(candidate):  # noqa: ANN001, ANN202
        composed.append(candidate.snapshot.direction)
        raise SoftTimeLimitExceeded()

    # No second text pass runs with no time left; the orchestrator's own handling gets it.
    with pytest.raises(SoftTimeLimitExceeded):
        _composed_plan(plan, [], {}, out_of_time)
    assert composed == ["text_explainer"]


def test_a_composed_story_is_composed_once_and_a_fast_montage_as_always() -> None:
    for strategy, direction in (
        (STRATEGY, "text_explainer"),
        ({**STRATEGY, "direction": "fast_montage"}, "fast_montage"),
    ):
        plan = _plan(strategy)
        calls: list[str] = []

        def compose(candidate):  # noqa: ANN001, ANN202
            calls.append(candidate.snapshot.direction)
            return candidate.snapshot

        result = _composed_plan(plan, [], {}, compose)
        assert result is plan and result.direction_fallback is None
        assert calls == [direction]


def _story_snapshot() -> EditProposalSnapshot:
    return _plan(STRATEGY).snapshot


def test_the_creators_named_entrance_goes_on_a_placeholder_caption() -> None:
    # The direction's own fade-in is nobody's choice; anything else already decided its entrance.
    placeholder = {"id": "guided-thought-story-beat-1", "effect": "fade-in"}
    assert not _plays_entrance(placeholder)
    assert _plays_entrance({**placeholder, "id": "guided-thought-beat-1"})
    assert _plays_entrance({**placeholder, "animation_phases": {"entrance": "fade"}})
    assert not _plays_entrance({"id": "guided-title", "effect": "static"})


def test_placeholders_the_text_step_did_not_replace_are_dropped_once_it_wrote_text() -> None:
    from app.schemas.text_composition import TextCompositionProgram

    snapshot = _story_snapshot()
    base = compile_proposal_execution_plan(snapshot)
    texts, slots = _lanes(base)
    seed_ids = [row["id"] for row in texts]
    assert len(seed_ids) == 3 and all(i.startswith("guided-thought-story-beat-") for i in seed_ids)

    def with_ops(ops: list[dict]) -> EditProposalSnapshot:
        program = TextCompositionProgram(base_digest=_digest(texts, slots), operations=ops)
        return snapshot.model_copy(update={"text_composition": program})

    window = (texts[1]["start_s"], texts[1]["end_s"])
    wrote = with_ops(
        [
            {"op": "remove_text", "bar_index": 1},
            {
                "op": "add_text",
                "text": "Assos: Home to a temple",
                "start_s": window[0],
                "end_s": window[1],
            },
        ]
    )
    cleaned = _drop_unused_story_seeds(wrote)
    final = compile_proposal_execution_plan(cleaned)["text_elements"]
    assert [row["text"] for row in final] == ["Assos: Home to a temple"]
    # Nothing written (the step only took a placeholder away): the chapter text stays.
    untouched = with_ops([{"op": "remove_text", "bar_index": 1}])
    assert _drop_unused_story_seeds(untouched) is untouched
    assert len(compile_proposal_execution_plan(untouched)["text_elements"]) == 2


def test_a_plain_photo_in_the_pinned_plan_does_not_make_the_card_promise_a_fast_montage() -> None:
    media = _media(5)
    media["clip_assignments"].append(
        {"media_id": "p0", "gcs_path": "p0.jpg", "generation": "1", "kind": "image"}
    )
    approved = build_length_plan(STRATEGY, media, story_floor_s=story_hold_floor_s(STRATEGY))
    photo = next(w for w in approved["windows"] if w["media_id"] == "p0")
    # Premise: a photo keeps its own ~1.2 s hold, shorter than the story floor by design.
    assert photo["source_end_s"] - photo["source_start_s"] < story_hold_floor_s(STRATEGY)
    assert story_media_blocker(STRATEGY, {"length_plan": approved}) is None
    # A video window held below the floor still names the fallback.
    short = {**approved, "windows": [dict(w) for w in approved["windows"]]}
    video = next(w for w in short["windows"] if w["media_id"] == "c0")
    video["source_end_s"] = video["source_start_s"] + 1.0
    assert story_media_blocker(STRATEGY, {"length_plan": short}) == "holds_do_not_fit"


def test_the_card_names_a_fast_montage_fallback_before_approval() -> None:
    asks = [
        BriefRequirement(id="r1", kind="text", scope="per_clip", description="a label per clip")
    ]
    built = card_disclosures([], STRATEGY, route=UNIFIED_MONTAGE)
    kept = card_disclosures(asks, STRATEGY, route=UNIFIED_MONTAGE)
    built_fields = {d.subject for d in built}
    # Built as a text explainer: direction and pacing are read, the outline only sets chapters.
    assert "direction" not in built_fields and "pacing" not in built_fields
    assert any(d.subject == "story_structure" and "won't reorder" in d.text for d in built)
    line = next(d.text for d in kept if d.subject == "direction")
    assert "fast montage instead of a text explainer" in line and "individual clips" in line
    assert brief_view(CreativeBrief(version=1, requirements=asks)).wants_per_clip_text


def test_approval_time_planning_holds_clips_longer_only_where_a_story_will_be_built() -> None:
    from app.kria.planner import _enrich_best_parts_snapshot

    song = {
        "audio_strategy": "licensed_music",
        "selected_music_section": {"start_s": 0, "end_s": 9},
    }
    strategy = {**STRATEGY, **song}
    ask = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="per_clip", description="a label per clip")
        ],
    )
    story = _enrich_best_parts_snapshot(strategy, _media(), None)["length_plan"]
    kept = _enrich_best_parts_snapshot(strategy, _media(), ask)["length_plan"]
    assert story["story"]["floor_s"] == 1.8 and story["story"]["total_s"] > 9.0
    assert "story" not in kept
    assert sum(w["source_end_s"] - w["source_start_s"] for w in kept["windows"]) == pytest.approx(
        9.0, abs=0.2
    )
