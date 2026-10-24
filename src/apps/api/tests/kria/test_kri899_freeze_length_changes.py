"""KRI-899 / KRI-828: the creator changes how long a freeze holds, by asking Kria or in the editor.

Merope café M2 (KRI-775 fixture) is approved with "Freeze the frame for one second on Atakan's
bite" (r3). The creator then changes it. Each case replays the real gate, the real phone worker,
the strict guided compiler, the phone recipe compiler and the rollout validator, and reads the
worker's own receipts, so "met" is proven on the compiled hold, not on the reply.

Ways this can fail (written before the fix), each pinned below:
A. A later correction that names no moment ("make the freeze 2 seconds instead") is dropped:
   the plan still holds 1 s and the correction gets KRI-777's "Couldn't" after render.
B. The correction carries the model's freeze fact but no freeze word ("make it 2 seconds
   instead"): it is read as nothing (or as the video's length) instead of following the
   creator's earlier freeze.
C. "Make the freeze longer / shorter" (no number) changes nothing, or picks a length the
   receipt does not name.
D. A second freeze on another moment is not planned (only the first live freeze is read), or
   both land on the same cut.
E. Restating the same moment with a new length holds that frame twice, or keeps the old length.
F. The draft and the finished receipts disagree: the draft promises (defers) a freeze the plan
   does not carry, or says "Couldn't" for one it does.
G. A freeze longer than 3 s (allowed up to 10 s) fails plan or editor validation outright.
H. A model's freeze fact on a video-length ask ("make the video 20 seconds") hijacks an
   existing freeze (KRI-821's corroboration rule).
I. On an already-rendered edit the chat router hands a freeze change to the editor copilot,
   which has no freeze operation, so the change is guessed or lost instead of re-planned.
J. Editor: trimming or extending the END of the held slot does not change ``freeze_hold_s``
   (or moves the frozen frame), in the guided editor's runtime plan or the phone recipe.
"""

from __future__ import annotations

import pytest

from app.kria.brief import (
    BriefUpdate,
    CreativeBrief,
    apply_updates,
    plan_shape_from_editor_snapshot,
    route_requirements,
)
from app.kria.brief_checks import requirements_to_check_at_draft
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from tests.kria.test_kri775_merope_m2_replay import (
    RENDERS,
    TAKE,
    m2_brief_with_moment_ask,
    prod_flags,
)
from tests.kria.test_kri828_freeze_frame_m2 import (
    BITE_FRAME_S,
    _phone_recipe,
    _plan_m2,
    _receipt,
    _verify_freeze,
)
from tests.pipeline.user_song_helpers import bindings_for

# The take's analysed "waitress hands over dessert" moment (clip record, best_moments).
HANDOVER_FRAME_S = round((50.04 + 52.04) / 2, 3)
_FRAME = 1 / 30 + 0.001


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)
    _verify_freeze(monkeypatch)


def _with(*asks: tuple[str, dict]) -> CreativeBrief:
    """The approved M2 brief plus later chat turns, applied as the planner applies them."""
    brief = m2_brief_with_moment_ask(RENDERS[0])
    for description, facts in asks:
        brief = apply_updates(
            brief,
            (
                BriefUpdate(
                    operation="add",
                    kind="timing",
                    scope="global",
                    description=description,
                    facts=facts,
                ),
            ),
            source_turn_id=None,
            creator_text=description,
        )
    return brief


def _new_ids(brief: CreativeBrief) -> list[str]:
    base = {req.id for req in m2_brief_with_moment_ask(RENDERS[0]).requirements}
    return [req.id for req in brief.live() if req.id not in base]


async def _holds(monkeypatch: pytest.MonkeyPatch, brief: CreativeBrief):  # noqa: ANN202
    plan, outcome = await _plan_m2(monkeypatch, brief=brief)
    assert outcome.refusal is None, outcome.refusal
    execution, recipe = _phone_recipe(monkeypatch, plan)
    held = [m for m in execution.story_timeline if m.freeze_hold_s]
    clips = {c.id: c for t in recipe.tracks for c in t.clips}
    return held, clips, outcome


def _draft_checked(brief: CreativeBrief) -> set[str]:
    """The ids the draft judges itself (the rest are settled by the render's receipts)."""
    return {
        req.id
        for req in requirements_to_check_at_draft(
            brief.live(),
            creator_id="u",
            strategy={"edit_format": "montage", "audio_strategy": "original_audio"},
            item_edit_format="montage",
            clip_paths=["users/u/creation-threads/t/analysis-proxy-ios-11CF148F.mp4"],
        )
    }


# -- A, B, F: a correction with no moment changes the creator's one freeze ---------------------


@pytest.mark.parametrize(
    ("description", "facts"),
    [
        ("Make the freeze 2 seconds instead", {"freeze_frame": True}),
        ("Make the freeze 2 seconds instead", {"duration_s": 2}),
        ("make it 2 seconds instead", {"freeze_frame": True, "duration_s": 2}),
        ("Donmayı 2 saniye yap", {"freeze_frame": True}),
    ],
    ids=["freeze-word+fact", "freeze-word-only", "fact-only", "turkish"],
)
async def test_a_correction_changes_the_hold_on_the_same_frame(
    monkeypatch: pytest.MonkeyPatch, description: str, facts: dict
) -> None:
    brief = _with((description, facts))
    (correction,) = _new_ids(brief)
    held, clips, outcome = await _holds(monkeypatch, brief)
    assert len(held) == 1, [m.model_dump() for m in held]
    assert held[0].source_end_s == pytest.approx(BITE_FRAME_S, abs=0.02)
    assert held[0].freeze_hold_s == pytest.approx(2.0, abs=0.001)
    assert clips[held[0].moment_id].hold_duration == pytest.approx(2.0, abs=_FRAME)
    for rid in ("r3", correction):
        receipt = _receipt(outcome, rid)
        assert receipt["status"] == "met", receipt
        assert "2s" in receipt["reason"], receipt
    # F. the draft promised exactly what the render holds: both rows are the render's
    assert not {"r3", correction} & _draft_checked(brief)
    # the correction's seconds are the freeze's, never the video's length
    length = _receipt(outcome, "r7")
    assert length["status"] == "met", length


# -- C: longer / shorter with no number --------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "hold_s"),
    [("Make the freeze longer", 2.0), ("Make the freeze shorter", 0.5)],
)
async def test_longer_or_shorter_changes_the_hold_and_says_by_how_much(
    monkeypatch: pytest.MonkeyPatch, description: str, hold_s: float
) -> None:
    brief = _with((description, {"freeze_frame": True}))
    (correction,) = _new_ids(brief)
    held, clips, outcome = await _holds(monkeypatch, brief)
    assert [m.freeze_hold_s for m in held] == [pytest.approx(hold_s, abs=0.001)]
    assert held[0].source_end_s == pytest.approx(BITE_FRAME_S, abs=0.02)
    receipt = _receipt(outcome, correction)
    assert receipt["status"] == "met", receipt
    assert f"{hold_s:g}s" in receipt["reason"], receipt


# -- D, F: a second freeze on another moment ----------------------------------------------------


async def test_a_second_freeze_on_another_moment_holds_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brief = _with(("Also freeze the frame for half a second when Caro hands over the dessert", {}))
    (second,) = _new_ids(brief)
    held, clips, outcome = await _holds(monkeypatch, brief)
    frames = sorted((round(m.source_end_s, 2), m.freeze_hold_s) for m in held)
    assert len({m.moment_id for m in held}) == 2, frames
    assert frames[0][0] == pytest.approx(HANDOVER_FRAME_S, abs=0.02)
    assert frames[0][1] == pytest.approx(0.5, abs=0.001)
    assert frames[1][0] == pytest.approx(BITE_FRAME_S, abs=0.02)
    assert frames[1][1] == pytest.approx(1.0, abs=0.001)
    for moment in held:
        clip = clips[moment.moment_id]
        assert clip.rate == 1
        assert clip.hold_duration == pytest.approx(moment.freeze_hold_s, abs=_FRAME)
    for rid in ("r3", second):
        assert _receipt(outcome, rid)["status"] == "met", _receipt(outcome, rid)
    assert not {"r3", second} & _draft_checked(brief)


# -- E: the same moment restated with a new length ----------------------------------------------


async def test_restating_the_same_moment_keeps_one_hold_at_the_new_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brief = _with(("Freeze the bite for 2 seconds", {"freeze_frame": True}))
    (restated,) = _new_ids(brief)
    held, _clips, outcome = await _holds(monkeypatch, brief)
    assert [(round(m.source_end_s, 2), m.freeze_hold_s) for m in held] == [
        (pytest.approx(BITE_FRAME_S, abs=0.02), pytest.approx(2.0, abs=0.001))
    ]
    for rid in ("r3", restated):
        receipt = _receipt(outcome, rid)
        assert receipt["status"] == "met" and "2s" in receipt["reason"], receipt


# -- G: a long freeze is a valid plan ------------------------------------------------------------


async def test_a_five_second_correction_compiles(monkeypatch: pytest.MonkeyPatch) -> None:
    brief = _with(("Make the freeze 5 seconds instead", {"freeze_frame": True}))
    # With "about 14 seconds" five held seconds overshoot it and the worker says so (an
    # honest length refusal, never a crash); with room for them the plan carries the hold.
    brief = brief.model_copy(
        update={
            "requirements": [
                req.model_copy(
                    update={"description": "About 18 seconds", "facts": {"duration_s": 18}}
                )
                if req.id == "r7"
                else req
                for req in brief.requirements
            ]
        }
    )
    (correction,) = _new_ids(brief)
    held, clips, outcome = await _holds(monkeypatch, brief)
    assert [m.freeze_hold_s for m in held] == [pytest.approx(5.0, abs=0.001)]
    assert clips[held[0].moment_id].hold_duration == pytest.approx(5.0, abs=_FRAME)
    assert _receipt(outcome, correction)["status"] == "met"


# -- H: a video-length ask never takes over a freeze ---------------------------------------------


async def test_a_video_length_ask_with_a_stray_freeze_fact_leaves_the_freeze_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brief = _with(("Make the video 20 seconds", {"freeze_frame": True, "duration_s": 20}))
    held, _clips, outcome = await _holds(monkeypatch, brief)
    assert [m.freeze_hold_s for m in held] == [pytest.approx(1.0, abs=0.001)]
    assert _receipt(outcome, "r3")["status"] == "met"


# -- I: on a rendered edit, a freeze change is re-planned, never sent to the copilot ------------


@pytest.mark.parametrize(
    "description",
    [
        "Make the freeze 2 seconds instead",
        "make the freeze longer",
        "Also freeze the frame for half a second when Caro hands over the dessert",
    ],
)
def test_a_freeze_change_on_a_rendered_edit_is_replanned(description: str) -> None:
    # The phone montage editor's snapshot: v2 ops with the clip family, so an ordinary
    # timing ask ("make clip 2 shorter") goes to the copilot.
    shape = plan_shape_from_editor_snapshot(
        {"allowed_op_families": ["clip", "text", "music"], "editor_ops_version": 2}
    )
    ordinary = BriefUpdate(
        operation="add", kind="timing", scope="global", description="make clip 2 shorter"
    )
    assert route_requirements([ordinary], shape, full_replan=False) == "editor_ops"
    brief = _with((description, {"freeze_frame": True}))
    fresh = [req for req in brief.live() if req.id in _new_ids(brief)]
    assert route_requirements(fresh, shape, message=description, full_replan=False) == "replan"


# -- J: the editor's end trim is the freeze length -------------------------------------------


async def _editor(monkeypatch: pytest.MonkeyPatch, brief: CreativeBrief | None = None):  # noqa: ANN202
    from app.pipeline.guided_story import compile_guided_runtime_plan  # noqa: PLC0415
    from app.routes.generative_jobs import TimelineSlotEdit, _guided_v2_slot_rows  # noqa: PLC0415
    from app.schemas.guided_edit_revision import (  # noqa: PLC0415
        guided_editor_revision_from_approval,
        normalize_guided_editor_revision,
    )
    from app.services.guided_timeline import build_guided_segments  # noqa: PLC0415

    plan, _outcome = await _plan_m2(monkeypatch, brief=brief)
    guided = plan.guided_edit()
    canonical = compile_execution_plan(guided, track=None)
    revision = guided_editor_revision_from_approval(
        proposal_version=guided["proposal_version"],
        media_digest=guided["media_digest"],
        snapshot=guided["approved_proposal"],
        execution_plan=canonical,
    )
    revision["state_hash"] = ""
    slots = _guided_v2_slot_rows(revision, lambda segment: "fullscreen", include_source_path=False)
    held = next(row for row in slots if row.get("freeze_hold_s"))

    def save(delta_s: float):  # noqa: ANN202
        """The editor's Save after dragging the held slot's end by ``delta_s``: the same
        build + normalize the timeline route runs, then the guided (cloud) runtime plan and
        the phone recipe the phone editor's Save compiles from it."""
        edited = [
            {
                key: (
                    round(row[key] + delta_s, 3)
                    if key == "duration_s" and row["slot_id"] == held["slot_id"]
                    else row[key]
                )
                for key in ("slot_id", "clip_index", "in_s", "duration_s")
            }
            for row in slots
        ]
        segments = build_guided_segments(
            revision,
            [TimelineSlotEdit.model_validate(row) for row in edited],
            transitions_enabled=False,
            max_slots=40,
            max_total_s=180,
        )
        saved = normalize_guided_editor_revision(
            {**revision, "revision_number": revision["revision_number"] + 1, "segments": segments},
            expected_approval_version=int(revision["approval_proposal_version"]),
            expected_media_digest=str(revision["approval_media_digest"]),
        )
        runtime = GuidedStoryExecutionPlan.model_validate(
            compile_guided_runtime_plan(canonical, guided, saved)
        )
        footage, visuals = bindings_for(plan, original_duration={TAKE: 68.37})
        recipe = compile_phone_guided_plan(runtime, footage, visuals)
        segment = next(s for s in saved["segments"] if s["segment_id"] == held["slot_id"])
        moment = next(m for m in runtime.story_timeline if m.moment_id == held["slot_id"])
        clip = next(c for t in recipe.tracks for c in t.clips if c.id == held["slot_id"])
        return segment, moment, clip

    return held, save


@pytest.mark.parametrize(("delta_s", "hold_s"), [(-0.4, 0.6), (1.0, 2.0), (3.0, 4.0)])
async def test_dragging_the_held_slots_end_sets_the_freeze_length(
    monkeypatch: pytest.MonkeyPatch, delta_s: float, hold_s: float
) -> None:
    held, save = await _editor(monkeypatch)
    assert held["freeze_hold_s"] == pytest.approx(1.0)
    segment, moment, clip = save(delta_s)
    # the saved revision (guided editor)
    assert segment["freeze_hold_s"] == pytest.approx(hold_s, abs=_FRAME)
    assert segment["source_end_s"] == pytest.approx(held["freeze_frame_s"], abs=0.001)
    # the cloud runtime plan the guided re-render draws
    assert moment.freeze_hold_s == pytest.approx(hold_s, abs=_FRAME)
    assert moment.source_end_s == pytest.approx(BITE_FRAME_S, abs=0.02)
    # the phone recipe the phone editor's Save pins
    assert clip.rate == 1
    assert clip.hold_duration == pytest.approx(hold_s, abs=_FRAME)
    assert clip.source_start + clip.source_duration == pytest.approx(BITE_FRAME_S, abs=0.02)
