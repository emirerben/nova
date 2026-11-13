"""One bad row never costs the creator the whole video (KRI-1376, rule KRI-129).

Every late phone gate runs repair -> degrade -> fail. A degrade drops or shrinks the one item
the typed reject names, says so, and the rest of the approved edit renders. Each gate in
``GATES`` gets one bad row next to good ones and goes through the REAL worker task
(``_run_generative_job`` -> unified montage planner -> guided compiler -> pin-time contract
check), with only storage, the DB row and the model steps fixed.

Ways this could fail, written before the code (each has a check below):
  1. the degrade drops a different row than the one the reject names, or more than one;
  2. the degrade moves a clip's timing (the rest of the edit must compile byte for byte);
  3. the degrade is silent: no note on the variant, no ``requirement_recovery`` event;
  4. the editor's Save recompiles the undegraded row (the persisted plan still has it);
  5. a stubborn plan loops: more bad rows than passes must end in the typed refusal, never
     an ``AssertionError`` that reads as our bug;
  6. a degrade masks a confirmed item: the contract check must still refuse it, typed;
  7. a confirmed text shrinks although the creator chose its size, or below a readable size;
  8. with the degrade off the row does not reproduce the refusal (the test proves nothing);
  9. a Turkish chat gets the note in English.
"""

from __future__ import annotations

import copy
from typing import get_args
from unittest.mock import Mock

import pytest

from app.agents._schemas.text_element import TextElement
from app.config import settings
from app.kria.recipes import MediaCapability
from app.kria.reply_language import reply_language_for
from app.pipeline import phone_plan_degrade, phone_plan_repair
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.phone_plan_repair import compile_phone_guided_repaired
from app.services.creator_render_contract import (
    CreatorRenderContract,
    CreatorRenderContractError,
    TextRequirement,
    verify_phone_recipe,
)
from app.services.device_render import device_status
from app.services.phone_editor import PHONE_EDITOR_PLAN_FIELD
from app.tasks import generative_build as gb
from tests.pipeline.test_phone_guided_plan import fixture, photo_fixture
from tests.tasks.test_phone_plan_contract_repair import _captured_events, _stamp_contract
from tests.tasks.test_unified_montage_dispatch import _brief, harness  # noqa: F401

BAD = 1  # the bad row sits between good ones


def _set(index: int, **fields):
    def inject(timeline: list[dict]) -> int:
        position = index % len(timeline)
        timeline[position].update(fields)
        return position

    return inject


# gate reason -> (bad row, what the degraded row must read, the note)
GATES = {
    "moment_treatment:look": (
        _set(BAD, look_preset="faded_analog"),
        {"look_preset": "none"},
        "Showed clip 2 without a look your iPhone can't draw yet",
    ),
    "moment_treatment:zoom": (
        _set(BAD, image_motion="subtle_zoom_in"),
        {"image_motion": None},
        "Showed clip 2 without a look your iPhone can't draw yet",
    ),
    "final_transition": (
        _set(-1, transition_after="crossfade"),
        {"transition_after": None},
        "Ended your video on a cut instead of a transition",
    ),
}


def _injecting(monkeypatch, job, inject) -> dict:
    """Wrap the harness's planner so the pinned plan carries ``inject``'s bad row."""
    planned = gb._guided_execution_plan
    seen: dict = {}

    def bad_plan(job_id, guided):
        plan, track = planned(job_id, guided)
        plan = copy.deepcopy(plan)
        seen["index"] = inject(plan["story_timeline"])
        seen["plan"] = copy.deepcopy(plan)
        job.assembly_plan["guided_story_execution_plan"] = plan
        return plan, track

    monkeypatch.setattr(gb, "_guided_execution_plan", bad_plan)
    return seen


@pytest.mark.parametrize("gate", sorted(GATES))
def test_one_bad_row_renders_the_rest_and_says_so(gate, harness, monkeypatch):  # noqa: F811
    inject, degraded, note = GATES[gate]
    job, *_ = harness(brief=_brief())
    contract = _stamp_contract(job)
    events = _captured_events(monkeypatch)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)
    seen = _injecting(monkeypatch, job, inject)

    gb._run_generative_job(str(job.id))

    failure.assert_not_called()
    assert job.status == "awaiting_device"
    recipe = device_status(job, "guided_story").request.recipe
    verify_phone_recipe(contract, recipe, source_audio=None)
    variant = job.assembly_plan["variants"][0]
    bad = seen["index"]
    injected = seen["plan"]["story_timeline"]
    for persisted in (
        variant["story_timeline"],
        variant[PHONE_EDITOR_PLAN_FIELD]["story_timeline"],
    ):
        assert len(persisted) == len(injected)
        for index, (row, original) in enumerate(zip(persisted, injected, strict=True)):
            if index == bad:
                assert {key: row.get(key) for key in degraded} == degraded
                rest = {k: v for k, v in row.items() if k not in degraded and v is not None}
                assert rest == {
                    k: v for k, v in original.items() if k not in degraded and v is not None
                }
            else:
                assert {k: v for k, v in row.items() if v is not None} == {
                    k: v for k, v in original.items() if v is not None
                }
    clips = recipe.tracks[0].clips
    assert [(c.id, c.timeline_start) for c in clips] == [
        (m["moment_id"], m["output_start_s"]) for m in injected
    ]
    assert all(clip.look is None for clip in clips)
    assert note in variant["phone_repair_notes"]
    reason = gate.split(":")[0]
    [recovery] = [data for _s, event, data in events if event == "requirement_recovery"]
    assert recovery == {
        "decision": "render_with_notes",
        "reason": reason,
        "lane": "transition" if reason == "final_transition" else "moment",
        "item_id": injected[bad]["moment_id"],
        "action": "dropped",
    }
    [repaired] = [data for _s, event, data in events if event == "phone_plan_repaired"]
    assert note in repaired["notes"]


def test_without_the_degrade_the_bad_row_still_refuses(harness, monkeypatch):  # noqa: F811
    """Failure mode 8: the injected row really is a refusal on the code before this change."""
    job, *_ = harness(brief=_brief())
    _stamp_contract(job)
    monkeypatch.setattr(phone_plan_degrade, "degrade_plan_for_phone", lambda plan, _exc: (plan, []))
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)
    _injecting(monkeypatch, job, GATES["moment_treatment:look"][0])

    gb._run_generative_job(str(job.id))

    assert failure.call_args.kwargs["failure_reason"] == "phone_plan_unsupported"


def test_more_bad_rows_than_passes_ends_in_the_typed_refusal(harness, monkeypatch):  # noqa: F811
    job, *_ = harness(brief=_brief())
    _stamp_contract(job)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    def every_clip(timeline: list[dict]) -> int:
        for row in timeline:
            row["look_preset"] = "faded_analog"
        return 0

    _injecting(monkeypatch, job, every_clip)

    gb._run_generative_job(str(job.id))

    assert failure.call_args.kwargs["failure_reason"] == "phone_plan_unsupported"
    assert "unsupported phone moment treatment" in failure.call_args.args[1]


# --- contract text placement: shrink before asking --------------------------------------------


@pytest.fixture
def _verified(monkeypatch):
    monkeypatch.setattr(settings, "phone_font_qualification_strict", False)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(get_args(MediaCapability)))


# Four one-line texts with gaps a full-size confirmed text cannot fit in, a smaller one can.
CROWD = [
    TextElement(
        id=f"crowd-{index}",
        text="Galata Kulesi",
        start_s=0.0,
        end_s=3.0,
        position="custom",
        y_frac=y,
        size_px=96,
        max_width_frac=0.9,
    )
    for index, y in enumerate((0.12, 0.362, 0.604, 0.846))
]
LONG = "Sabah kahvesi ve simit, sonra vapurla karşıya"


def _crowded(**source_params):
    plan, bindings = fixture()
    plan.text_elements = [*CROWD]
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role="any", text=LONG),)
    )
    return plan, bindings, contract


@pytest.mark.usefixtures("_verified")
def test_a_confirmed_text_with_no_room_shrinks_instead_of_asking():
    plan, bindings, contract = _crowded()

    result = compile_phone_guided_repaired(plan, bindings, contract=contract)

    [added] = [e for e in result.plan.text_elements if e.text == LONG]
    assert 40 <= added.size_px < 64
    assert result.notes == [
        f"Added your text “{LONG}” on screen",
        f"Made your text “{LONG}” smaller so it fits clearly next to the other text",
    ]
    verify_phone_recipe(contract, result.recipe, source_audio={"source": True})


@pytest.mark.usefixtures("_verified")
def test_without_the_shrink_the_crowded_text_asks_for_a_choice(monkeypatch):
    """Failure mode 8 for the placement gate: the crowd really leaves no room at full size."""
    plan, bindings, contract = _crowded()
    monkeypatch.setattr(phone_plan_repair, "_shrink_sizes", lambda element: [element.size_px])

    with pytest.raises(CreatorRenderContractError) as refused:
        compile_phone_guided_repaired(plan, bindings, contract=contract)

    assert refused.value.decline_reason == "needs_choice"


@pytest.mark.usefixtures("_verified")
def test_a_size_the_creator_chose_never_shrinks():
    """Failure mode 7: the confirmed item keeps its size and the refusal stays typed."""
    stray = TextElement(
        id="stray",
        text=LONG,
        start_s=0.0,
        end_s=3.0,
        size_px=64,
        source_params={"creator_style_fields": ["size_px"]},
    )
    plan, bindings = fixture()
    plan.text_elements = [*CROWD, stray]
    element = stray.model_copy(update={"position": "custom", "y_frac": 0.5})

    assert phone_plan_repair._shrink_sizes(element) == [64]
    with pytest.raises(CreatorRenderContractError) as refused:
        phone_plan_repair._place_added_contract_text(plan, element)
    assert refused.value.decline_reason == "needs_choice"


@pytest.mark.usefixtures("_verified")
@pytest.mark.parametrize(
    "bad,note",
    [
        (
            {"image_motion": "subtle_zoom_in"},
            "Showed clip 2 without a look your iPhone can't draw yet",
        ),
        ({"look_preset": "golden_hour"}, "Showed clip 2 without a look your iPhone can't draw yet"),
        (
            {"look_adjustments": {"brightness": 0.1}},
            "Showed clip 2 without a look your iPhone can't draw yet",
        ),
        ({"freeze_hold_s": 1.0}, "Kept photo 2 on screen without a freeze effect"),
    ],
)
def test_a_photo_the_phone_cannot_treat_renders_as_the_plain_still(bad, note):
    """The worker fixture has no Visuals photo, so the photo gates run at the compile call."""
    plan, bindings, visuals = photo_fixture(**bad)
    clean, _bindings, _visuals = photo_fixture()

    result = compile_phone_guided_repaired(plan, bindings, visuals=visuals)

    assert result.recipe == compile_phone_guided_plan(clean, bindings, visuals)
    assert result.notes == [note]


# --- the degrade itself ----------------------------------------------------------------------


def test_a_turkish_chat_gets_the_note_in_turkish():
    plan, _bindings = fixture()
    moment = plan.story_timeline[0].model_copy(update={"look_preset": "faded_analog"})
    plan = plan.model_copy(update={"story_timeline": [moment]})
    exc = UnsupportedPhonePlan(
        "unsupported phone moment treatment",
        reason="moment_treatment",
        lane="moment",
        item_id=moment.moment_id,
    )
    with reply_language_for("tr"):
        degraded, notes = phone_plan_degrade.degrade_plan_for_phone(plan, exc)
    assert degraded.story_timeline[0].look_preset == "none"
    assert notes == ["1. klibi iPhone'unun henüz çizemediği bir görünüm olmadan gösterdim"]


def test_a_reject_that_names_no_known_row_is_left_to_fail():
    plan, _bindings = fixture()
    for exc in (
        UnsupportedPhonePlan("x", reason="moment_treatment", lane="moment", item_id="missing"),
        UnsupportedPhonePlan("x", reason="transition_window", lane="transition", item_id="moment"),
        UnsupportedPhonePlan("x"),
        ValueError("phone timeline duration differs from approved plan"),
    ):
        assert phone_plan_degrade.degrade_plan_for_phone(plan, exc) == (plan, [])
