"""KRI-828 review round: the creator's freeze survives their own numbers, editor trims,
transitions and a stated video length.

Ways this can fail (written before the fix), each pinned below:
A. Decimal holds: "for 1.5 seconds" / "0.5 seconds" / "1,5 saniye" split into "1" "5", so the
   creator's length is lost, the 1 s default is held and the receipt claims they never said
   how long.
B. A stated hold outside what a freeze can carry is silently changed to a different length
   while the receipt still says "met" for that other length.
C. Trimming the END of the held slot in the editor drops the freeze (new footage plays past
   the chosen frame) or moves the frozen frame; Save and the phone recipe disagree on the
   frame.
D. A crossfade next to the held slot makes the whole editor Save fail (422) instead of
   rendering both the transition and the hold.
E. The hold is added on top of cuts already sized to the creator's length, so a stated
   length overshoots by the hold and the length receipt fails.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement
from app.kria.freeze_ask import MAX_FREEZE_HOLD_S, freeze_spec
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


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)


def _req(description: str, facts: dict | None = None) -> BriefRequirement:
    return BriefRequirement(
        id="rf", kind="timing", scope="global", description=description, facts=facts or {}
    )


# -- A: decimal seconds are the creator's hold ------------------------------------------------


@pytest.mark.parametrize(
    ("description", "hold_s", "words"),
    [
        ("Freeze the frame for 1.5 seconds on Atakan's bite", 1.5, ("atakan", "bite")),
        ("Freeze for 0.5 seconds on the jump", 0.5, ("jump",)),
        ("freeze 2.5s on the toast", 2.5, ("toast",)),
        ("Isırırken kareyi 1,5 saniye dondur", 1.5, ()),
        ("Freeze the frame at 14.5 seconds for 2 seconds", 2.0, ()),
    ],
)
def test_decimal_seconds_set_the_hold(description: str, hold_s: float, words: tuple) -> None:
    spec = freeze_spec(_req(description, {"freeze_frame": True}))
    assert spec is not None
    assert spec.hold_s == pytest.approx(hold_s)
    assert spec.hold_stated
    assert set(words) <= set(spec.target_words), spec


# -- B: a stated hold is honoured, or the receipt says it was not -----------------------------


def test_a_five_second_freeze_holds_five_seconds() -> None:
    spec = freeze_spec(_req("Freeze the frame for 5 seconds on the bite", {"freeze_frame": True}))
    assert spec is not None
    assert (spec.hold_s, spec.hold_stated, spec.hold_requested_s) == (5.0, True, 5.0)


def test_a_hold_beyond_the_limit_is_never_reported_as_met() -> None:
    from app.kria.brief_checks import _check_freeze  # noqa: PLC0415

    req = _req("Freeze the frame for 20 seconds on the bite", {"freeze_frame": True})
    spec = freeze_spec(req)
    assert spec is not None
    assert spec.hold_s == MAX_FREEZE_HOLD_S
    assert spec.hold_requested_s == 20.0
    receipt = _check_freeze(
        req,
        {
            "status": "held",
            "hold_s": spec.hold_s,
            "hold_stated": spec.hold_stated,
            "hold_requested_s": spec.hold_requested_s,
            "frame_source_s": 58.44,
        },
    )
    assert receipt.status == "partial"
    assert "20" in receipt.reason and f"{MAX_FREEZE_HOLD_S:g}" in receipt.reason


# -- C, D: editor trims and transitions keep the chosen frame ---------------------------------


async def _editor_fixture(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN202
    from app.pipeline.guided_story import compile_guided_runtime_plan  # noqa: PLC0415
    from app.routes.generative_jobs import _guided_v2_slot_rows  # noqa: PLC0415
    from app.schemas.guided_edit_revision import (  # noqa: PLC0415
        guided_editor_revision_from_approval,
    )

    _verify_freeze(monkeypatch)
    plan, _outcome = await _plan_m2(monkeypatch)
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

    def recipe_for(saved: dict):  # noqa: ANN202
        runtime = GuidedStoryExecutionPlan.model_validate(
            compile_guided_runtime_plan(canonical, guided, saved)
        )
        footage, visuals = bindings_for(plan, original_duration={TAKE: 68.37})
        return runtime, compile_phone_guided_plan(runtime, footage, visuals)

    return revision, slots, recipe_for


def _save(revision: dict, slots: list[dict], *, transitions: bool = False) -> dict:
    from app.routes.generative_jobs import TimelineSlotEdit  # noqa: PLC0415
    from app.services.guided_timeline import build_guided_segments  # noqa: PLC0415

    segments = build_guided_segments(
        revision,
        [TimelineSlotEdit.model_validate(row) for row in slots],
        transitions_enabled=transitions,
        max_slots=40,
        max_total_s=180,
    )
    return {**revision, "segments": segments}


_EDITABLE = ("slot_id", "clip_index", "in_s", "duration_s")


async def test_trimming_the_held_slots_end_shortens_the_hold_on_the_same_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision, slots, recipe_for = await _editor_fixture(monkeypatch)
    held = next(row for row in slots if row.get("freeze_hold_s"))
    # The editor (and its preview) learns the chosen frame, which trims never rewrite.
    assert held["freeze_frame_s"] == pytest.approx(BITE_FRAME_S, abs=0.02)
    editable = [{key: row[key] for key in _EDITABLE} for row in slots]

    def trimmed_by(seconds: float) -> list[dict]:
        return [
            {**row, "duration_s": round(row["duration_s"] - seconds, 3)}
            if row["slot_id"] == held["slot_id"]
            else row
            for row in editable
        ]

    saved = _save(revision, trimmed_by(0.4))
    segment = next(s for s in saved["segments"] if s["segment_id"] == held["slot_id"])
    assert segment["source_end_s"] == pytest.approx(held["freeze_frame_s"], abs=0.001)
    assert segment["freeze_hold_s"] == pytest.approx(0.6, abs=1 / 30 + 0.001)
    clip = next(c for t in recipe_for(saved)[1].tracks for c in t.clips if c.id == held["slot_id"])
    assert clip.rate == 1
    assert clip.source_start + clip.source_duration == pytest.approx(BITE_FRAME_S, abs=0.02)
    assert clip.hold_duration == pytest.approx(0.6, abs=1 / 30 + 0.001)

    # A trim that eats the whole hold leaves a plain clip ending before the frame.
    saved = _save(revision, trimmed_by(1.2))
    segment = next(s for s in saved["segments"] if s["segment_id"] == held["slot_id"])
    assert not segment.get("freeze_hold_s")
    assert segment["source_end_s"] <= held["freeze_frame_s"] + 0.001


async def test_a_crossfade_next_to_the_held_slot_keeps_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision, slots, recipe_for = await _editor_fixture(monkeypatch)
    # The creator also moves the held moment to second place, so it has a neighbour on each
    # side and the photo plays AFTER the hold, and puts a crossfade on both of its edges.
    held_row = next(row for row in slots if row.get("freeze_hold_s"))
    rest = [row for row in slots if row is not held_row]
    slots = [rest[0], held_row, *rest[1:]]
    order = [row["slot_id"] for row in slots]
    held_at = 1
    edited = []
    for index, row in enumerate(slots):
        base = {key: row[key] for key in _EDITABLE}
        if index in (held_at - 1, held_at):
            base["transition_after"] = "crossfade"
            base["transition_duration_s"] = 0.3
        edited.append(base)
    runtime, recipe = recipe_for(_save(revision, edited, transitions=True))
    clips = {c.id: c for t in recipe.tracks for c in t.clips}
    held = clips[order[held_at]]
    assert held.hold_duration == pytest.approx(1.0, abs=1 / 30 + 0.001)
    assert held.source_start + held.source_duration == pytest.approx(BITE_FRAME_S, abs=0.02)
    assert held.transition is not None and held.transition.kind == "crossfade"
    after = clips[order[held_at + 1]]
    assert after.transition is not None and after.transition.kind == "crossfade"
    # The photo now plays after the hold, on the clock the hold moved it to.
    photo = next(m for m in runtime.story_timeline if m.kind == "image")
    hold_end = next(m for m in runtime.story_timeline if m.freeze_hold_s).output_end_s
    assert photo.output_start_s >= hold_end - 0.5  # inside the outgoing crossfade


# -- E: a stated length reserves room for the hold -------------------------------------------


async def test_a_long_freeze_comes_out_of_the_creators_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verify_freeze(monkeypatch)
    brief = m2_brief_with_moment_ask(RENDERS[0])
    freeze_req = next(req for req in brief.live() if req.id == "r3")
    brief = brief.model_copy(
        update={
            "requirements": [
                req.model_copy(
                    update={"description": "Freeze the frame for 3 seconds on Atakan's bite"}
                )
                if req.id == freeze_req.id
                else req
                for req in brief.requirements
            ]
        }
    )
    plan, outcome = await _plan_m2(monkeypatch, brief=brief)
    execution, recipe = _phone_recipe(monkeypatch, plan)
    held = [m for m in execution.story_timeline if m.freeze_hold_s]
    assert len(held) == 1
    assert held[0].freeze_hold_s == pytest.approx(3.0, abs=0.001)
    assert held[0].source_end_s == pytest.approx(BITE_FRAME_S, abs=0.02)
    assert recipe.duration <= 14 * 1.1, recipe.duration  # "About 14 seconds"
    freeze = _receipt(outcome, freeze_req.id)
    assert freeze["status"] == "met", freeze
    assert "3s" in freeze["reason"]
    length = _receipt(outcome, "r7")
    assert length["status"] == "met", length
