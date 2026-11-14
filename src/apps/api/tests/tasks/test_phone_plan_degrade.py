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

Added with the verifier's findings, before the fix:
 10. a text the phone cannot animate (right-to-left words in a staggered slice) refuses the
     whole video, or loses an animation the creator chose;
 11. naming the text row changes the failure code or message of a text reject no degrade can
     fix (a script face that needs a newer app build);
 12. the note says less than the row lost (a crop, a speed or a card layout reported as a
     "look"), or calls a photo a clip;
 13. ``requirement_recovery`` fires for a compile the render throws away (the planning dry run,
     a refused candidate), or twice for one row;
 14. a look every clip carries refuses the whole video, or fills the approval card's six lines
     with one note per clip;
 15. a degrade that does not fix its row loops, or ends in anything but the typed refusal.

Added with the second verifier's findings, before the fix:
 16. a turned clip whose background picture is not bound refuses untyped, naming no reason or
     row; or a degrade drops that background although the creator pinned it (the guided
     worker binds only the visuals its timeline shows, so "not bound" is not "not pinned");
 17. the contract and phone repair notes ("Added your text", "Used your title", "Changed a
     text font") stay English in a Turkish chat next to the Turkish shrink note;
 18. the planning dry run still answers a degradable row with ``PhoneProposalRejected``, so the
     creator is offered the request-blind fallback instead of the model's draft and its note.

Added with the third verifier's findings, before the fix:
 19. a word-timed text on a Voiceover edit with no clear spot at its size still stops the whole
     video to ask the creator, although a smaller readable size fits below the face;
 20. that shrink touches a text that already fit, its words or its time, or goes below the
     readable floor;
 21. the shrink is silent: no note on the variant, a note in English in a Turkish chat, or no
     ``requirement_recovery`` event naming the row;
 22. the approval card shows a degrade note in English in a Turkish chat (the planning task
     binds no chat language);
 23. four different kinds of degradable reject still refuse the whole video; or, with no cap,
     a degrade that does not fix its row loops instead of ending in the typed refusal.

Added with the fourth verifier's findings, before the fix:
 24. two texts with the same words count as one row, so the note says fewer texts changed than
     did (prod 5df2e3ec has two "Türkiye" labels);
 25. a guided or Voiceover render the job no longer owns (a stale owner, a cancel) records
     ``requirement_recovery`` for a compile it never pins;
 26. a caption fault on a Voiceover edit refuses the whole video although captions are a
     preference, or the captions go silently, or their receipt still names a caption language;
 27. a caption fault drops the beat cards and sound effects too, then refuses anyway.
"""

from __future__ import annotations

import copy
import uuid
from typing import get_args
from unittest.mock import Mock

import pytest

from app.agents._schemas.text_element import TextElement
from app.config import settings
from app.kria.recipes import MediaCapability
from app.kria.reply_language import reply_language_for
from app.pipeline import phone_plan_degrade, phone_plan_repair
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.phone_plan_repair import (
    PhoneProposalRejected,
    compile_phone_guided_repaired,
    validate_proposal_phone_compiles,
)
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
from tests.pipeline.test_phone_plan_repair import _sequence, _snapshot_for, _stub_dry_run
from tests.tasks.test_phone_plan_contract_repair import _captured_events, _stamp_contract
from tests.tasks.test_unified_montage_dispatch import _brief, harness  # noqa: F401

BAD = 1  # the bad row sits between good ones
ARABIC = "مرحبا بالعالم"


def _set(index: int, **fields):
    def inject(plan: dict) -> int:
        timeline = plan["story_timeline"]
        position = index % len(timeline)
        timeline[position].update(fields)
        return position

    return inject


def _texts(plan: dict) -> int:
    """A text the phone draws next to right-to-left words it cannot slice letter by letter."""
    rows = [
        TextElement(id="good-text", text="Galata", start_s=0.0, end_s=2.0, effect="pop-in"),
        TextElement(id="rtl-text", text=ARABIC, start_s=0.5, end_s=2.5, effect="staggered-slice"),
    ]
    plan["text_elements"] = [
        *plan["text_elements"],
        *(row.model_dump(mode="json", exclude_none=False) for row in rows),
    ]
    return len(plan["text_elements"]) - 1


# gate -> (bad row, the plan field it sits in, what the degraded row must read, the note)
GATES = {
    "moment_treatment:look": (
        _set(BAD, look_preset="faded_analog"),
        "story_timeline",
        {"look_preset": "none"},
        "Showed clip 2 without its look: your iPhone can't draw that yet",
    ),
    "moment_treatment:zoom": (
        _set(BAD, image_motion="subtle_zoom_in"),
        "story_timeline",
        {"image_motion": None},
        "Showed clip 2 without its zoom: your iPhone can't draw that yet",
    ),
    "final_transition": (
        _set(-1, transition_after="crossfade"),
        "story_timeline",
        {"transition_after": None},
        "Ended your video on a cut instead of a transition",
    ),
    "text_effect": (
        _texts,
        "text_elements",
        {"effect": "fade-in"},
        f"Showed the text “{ARABIC}” with a simple fade-in: your iPhone can't play its "
        "animation yet",
    ),
}
LANES = {"story_timeline": ("moment", "moment_id"), "text_elements": ("text", "id")}


def _injecting(monkeypatch, job, inject) -> dict:
    """Wrap the harness's planner so the pinned plan carries ``inject``'s bad row."""
    planned = gb._guided_execution_plan
    seen: dict = {}

    def bad_plan(job_id, guided):
        plan, track = planned(job_id, guided)
        plan = copy.deepcopy(plan)
        seen["index"] = inject(plan)
        seen["plan"] = copy.deepcopy(plan)
        job.assembly_plan["guided_story_execution_plan"] = plan
        return plan, track

    monkeypatch.setattr(gb, "_guided_execution_plan", bad_plan)
    return seen


@pytest.mark.parametrize("gate", sorted(GATES))
def test_one_bad_row_renders_the_rest_and_says_so(gate, harness, monkeypatch):  # noqa: F811
    inject, field, degraded, note = GATES[gate]
    job, *_ = harness(brief=_brief())
    # The Arabic words need the script face; this build is past its protocol.
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "scriptFonts"],
    )
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
    lane, key = LANES[field]
    injected = seen["plan"][field]
    for persisted in (variant[field], variant[PHONE_EDITOR_PLAN_FIELD][field]):
        assert [row[key] for row in persisted] == [row[key] for row in injected]
        for index, (row, original) in enumerate(zip(persisted, injected, strict=True)):
            if index == bad:
                assert {name: row.get(name) for name in degraded} == degraded
                rest = {k: v for k, v in row.items() if k not in degraded and v is not None}
                assert rest == {
                    k: v for k, v in original.items() if k not in degraded and v is not None
                }
            elif field == "story_timeline":
                assert {k: v for k, v in row.items() if v is not None} == {
                    k: v for k, v in original.items() if v is not None
                }
    timeline = seen["plan"]["story_timeline"]
    clips = recipe.tracks[0].clips
    assert [(c.id, c.timeline_start) for c in clips] == [
        (m["moment_id"], m["output_start_s"]) for m in timeline
    ]
    assert all(clip.look is None for clip in clips)
    if field == "text_elements":
        drawn = {
            "".join(run.text for run in layer.runs).replace(" ", "") for layer in recipe.text_layers
        }
        assert {"Galata", ARABIC.replace(" ", "")} <= drawn
        assert variant[field][bad - 1]["effect"] == "pop-in"
    assert note in variant["phone_repair_notes"]
    reason = gate.split(":")[0]
    [recovery] = [data for _s, event, data in events if event == "requirement_recovery"]
    assert recovery == {
        "decision": "render_with_notes",
        "reason": reason,
        "lane": "transition" if reason == "final_transition" else lane,
        "item_id": injected[bad][key],
        "action": "dropped",
    }
    [repaired] = [data for _s, event, data in events if event == "phone_plan_repaired"]
    assert note in repaired["notes"]


@pytest.mark.parametrize("gate", sorted(GATES))
def test_without_the_degrade_the_bad_row_still_refuses(gate, harness, monkeypatch):  # noqa: F811
    """Failure mode 8: the injected row really is a refusal on the code before this change."""
    job, *_ = harness(brief=_brief())
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "scriptFonts"],
    )
    _stamp_contract(job)
    monkeypatch.setattr(phone_plan_degrade, "degrade_plan_for_phone", lambda plan, _exc: (plan, []))
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)
    _injecting(monkeypatch, job, GATES[gate][0])

    gb._run_generative_job(str(job.id))

    assert failure.call_args.kwargs["failure_reason"] == "phone_plan_unsupported"


def test_a_look_on_every_clip_renders_with_one_note(harness, monkeypatch):  # noqa: F811
    """Failure modes 13 and 14: the likeliest prod shape is one look the edit put everywhere."""
    job, *_ = harness(brief=_brief())
    _stamp_contract(job)
    events = _captured_events(monkeypatch)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    def every_clip(plan: dict) -> int:
        for row in plan["story_timeline"]:
            row["look_preset"] = "faded_analog"
        return 0

    seen = _injecting(monkeypatch, job, every_clip)

    gb._run_generative_job(str(job.id))

    failure.assert_not_called()
    assert job.status == "awaiting_device"
    timeline = seen["plan"]["story_timeline"]
    assert len(timeline) > 4
    variant = job.assembly_plan["variants"][0]
    assert {row["look_preset"] for row in variant["story_timeline"]} == {"none"}
    note = f"Showed {len(timeline)} clips without their look: your iPhone can't draw that yet"
    assert [n for n in variant["phone_repair_notes"] if "without" in n] == [note]
    recovered = [data["item_id"] for _s, event, data in events if event == "requirement_recovery"]
    assert sorted(recovered) == sorted(row["moment_id"] for row in timeline)


@pytest.mark.usefixtures("_verified")
def test_a_compile_records_nothing_until_the_worker_keeps_it(monkeypatch):
    """Failure mode 13: the planning dry run and a refused candidate compile alike; only the
    worker records the compile it keeps."""
    events = _captured_events(monkeypatch)
    plan, bindings = fixture()
    plan.story_timeline = [
        plan.story_timeline[0].model_copy(update={"look_preset": "faded_analog"})
    ]

    result = compile_phone_guided_repaired(plan, bindings)

    assert [r.item_id for r in result.recoveries] == ["moment"]
    assert events == []


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
    monkeypatch.setattr(phone_plan_degrade, "shrink_sizes", lambda size, **_kw: [size])

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

    with pytest.raises(CreatorRenderContractError) as refused:
        phone_plan_repair._place_added_contract_text(plan, element)
    assert refused.value.decline_reason == "needs_choice"


CROP = {"x": 0.1, "y": 0.2, "width": 0.6, "height": 0.5}
CARD = {"layout": "supporting_card"}


@pytest.mark.usefixtures("_verified")
@pytest.mark.parametrize(
    "bad,kept,lost",
    [
        ({"image_motion": "subtle_zoom_in"}, {}, "zoom"),
        ({"look_preset": "golden_hour"}, {}, "look"),
        ({"look_adjustments": {"brightness": 0.1}}, {}, "color adjustments"),
        ({"freeze_hold_s": 1.0}, {}, "freeze effect"),
        ({"playback_rate": 2.0}, {}, "speed change"),
        ({**CARD, "source_crop": CROP}, CARD, "crop"),
        ({**CARD, "source_crop": CROP, "image_motion": "subtle_zoom_in"}, CARD, "zoom and crop"),
    ],
)
def test_a_photo_the_phone_cannot_treat_renders_as_the_plain_still(bad, kept, lost):
    """Failure mode 12: the note names what the photo lost, and calls it a photo.

    The worker fixture has no Visuals photo, so the photo gates run at the compile call.
    """
    plan, bindings, visuals = photo_fixture(**bad)
    clean, _bindings, _visuals = photo_fixture(**kept)

    result = compile_phone_guided_repaired(plan, bindings, visuals=visuals)

    assert result.recipe == compile_phone_guided_plan(clean, bindings, visuals)
    assert result.notes == [f"Showed photo 2 without its {lost}: your iPhone can't draw that yet"]


# --- the degrade itself ----------------------------------------------------------------------


@pytest.mark.usefixtures("_verified")
def test_a_turkish_chat_gets_the_note_in_turkish():
    plan, bindings, visuals = photo_fixture(image_motion="subtle_zoom_in", playback_rate=2.0)
    plan.story_timeline[0] = plan.story_timeline[0].model_copy(
        update={"look_preset": "faded_analog"}
    )

    with reply_language_for("tr"):
        result = compile_phone_guided_repaired(plan, bindings, visuals=visuals)

    assert result.notes == [
        "1. klibi görünüm olmadan gösterdim: iPhone'un bunu henüz çizemiyor",
        "2. fotoğrafı yakınlaştırma ve hız değişikliği olmadan gösterdim: iPhone'un bunu henüz "
        "çizemiyor",
    ]


@pytest.mark.usefixtures("_verified")
def test_a_turkish_chat_gets_every_repair_note_in_turkish():
    """Failure mode 17: the contract and phone repair notes sit beside the Turkish shrink note."""
    plan, bindings, contract = _crowded()
    sequence, sequence_bindings = fixture()
    sequence.text_elements = _sequence("pop-in")

    with reply_language_for("tr"):
        placed = compile_phone_guided_repaired(plan, bindings, contract=contract)
        swapped = compile_phone_guided_repaired(sequence, sequence_bindings)

    assert placed.notes == [
        f"“{LONG}” yazını ekrana ekledim",
        f"“{LONG}” yazını diğer yazıların yanına net sığsın diye küçülttüm",
    ]
    assert swapped.notes == [
        "iPhone'unun henüz oynatamadığı bir yazı animasyonunu basit bir belirmeyle değiştirdim"
    ]


# --- the planning dry run ----------------------------------------------------------------------


@pytest.mark.usefixtures("_verified")
def test_the_planning_dry_run_offers_the_draft_with_the_note(monkeypatch):
    """Failure mode 18: a row the worker degrades never sends the proposal to the fallback."""
    plan, bindings = fixture()
    plan.story_timeline = [
        plan.story_timeline[0].model_copy(update={"look_preset": "faded_analog"})
    ]
    _stub_dry_run(monkeypatch, plan, bindings)

    notes = validate_proposal_phone_compiles(_snapshot_for(plan, bindings), [])

    assert notes == ["Showed clip 1 without its look: your iPhone can't draw that yet"]
    monkeypatch.setattr(phone_plan_degrade, "degrade_plan_for_phone", lambda plan, _exc: (plan, []))
    with pytest.raises(PhoneProposalRejected):
        validate_proposal_phone_compiles(_snapshot_for(plan, bindings), [])


# --- a background the worker did not bind ------------------------------------------------------


@pytest.mark.usefixtures("_verified")
def test_an_unbound_background_refuses_typed_and_is_never_dropped():
    """Failure mode 16: the reject names the clip, and the background the creator may have
    pinned is not silently traded for a plain one."""
    plan, bindings = fixture()
    tilt = {"rotation_deg": 10.0, "backdrop": {"kind": "media", "asset_id": "never-bound"}}
    plan.story_timeline = [plan.story_timeline[0].model_copy(update={"tilt": tilt})]

    with pytest.raises(UnsupportedPhonePlan) as refused:
        compile_phone_guided_repaired(plan, bindings)

    exc = refused.value
    assert (exc.reason, exc.lane, exc.item_id) == ("backdrop_unbound", "moment", "moment")
    assert str(exc) == "a media backdrop needs a photo or video pinned to this video"
    assert phone_plan_degrade.degrade_plan_for_phone(plan, exc) == (plan, [])
    assert gb._classify_phone_plan_failure(exc) == ("phone_plan_unsupported", "plan_defect")


# --- text the phone cannot animate ------------------------------------------------------------


def _rtl(**fields):
    plan, bindings = fixture()
    plan.text_elements = [
        TextElement(id="good-text", text="Galata", start_s=0.0, end_s=2.0, effect="pop-in"),
        TextElement(
            id="rtl-text", text=ARABIC, start_s=0.5, end_s=2.5, effect="staggered-slice", **fields
        ),
    ]
    return plan, bindings


@pytest.mark.usefixtures("_verified")
def test_an_animation_the_creator_chose_is_never_swapped():
    """Failure mode 10: the creator's own animation refuses, typed, naming the text."""
    plan, bindings = _rtl(source_params={"creator_style_fields": ["effect"]})

    with pytest.raises(UnsupportedPhonePlan) as refused:
        compile_phone_guided_repaired(plan, bindings)

    assert (refused.value.reason, refused.value.item_id) == ("text_effect", "rtl-text")
    assert str(refused.value) == "staggered slice cannot animate right-to-left text"


def test_a_text_no_degrade_can_fix_keeps_its_failure_code(monkeypatch):
    """Failure mode 11: a script face the build lacks still fails as before, now naming the row."""
    monkeypatch.setattr(settings, "phone_font_qualification_strict", False)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [c for c in get_args(MediaCapability) if c != "scriptFonts"],
    )
    plan, bindings = _rtl()

    with pytest.raises(UnsupportedPhonePlan) as refused:
        compile_phone_guided_repaired(plan, bindings)

    assert (refused.value.reason, refused.value.item_id) == ("text_treatment", "rtl-text")
    assert "requires a newer app build" in str(refused.value)
    assert gb._classify_phone_plan_failure(refused.value) == (
        "phone_plan_unsupported",
        "plan_defect",
    )


@pytest.mark.usefixtures("_verified")
def test_a_row_degraded_twice_gets_one_note_naming_both():
    """Failure mode 12: a zoom and a golden hour on a landscape clip are two rejects, one row."""
    plan, bindings = fixture()
    plan.story_timeline = [
        plan.story_timeline[0].model_copy(
            update={"image_motion": "subtle_zoom_in", "look_preset": "golden_hour"}
        )
    ]

    result = compile_phone_guided_repaired(plan, bindings)

    assert [r.reason for r in result.recoveries] == ["moment_treatment", "look_source"]
    assert result.notes == [
        "Showed clip 1 without its zoom and look: your iPhone can't draw that yet"
    ]


# --- many rejects, and a degrade that does not help ---------------------------------------------


@pytest.mark.usefixtures("_verified")
def test_four_kinds_of_reject_all_render_with_their_notes():
    """Failure mode 23: every degradable row degrades, however many kinds of reject there are.

    The video is landscape, so its golden hour is a ``look_source`` reject; the photo's zoom
    a ``photo_treatment``; the photo's transition out a ``final_transition``; the right-to-left
    staggered text a ``text_effect``.
    """
    plan, bindings, visuals = photo_fixture(
        image_motion="subtle_zoom_in", transition_after="crossfade"
    )
    plan.story_timeline[0] = plan.story_timeline[0].model_copy(
        update={"look_preset": "golden_hour"}
    )
    rtl, _bindings = _rtl()
    plan.text_elements = rtl.text_elements

    result = compile_phone_guided_repaired(plan, bindings, visuals=visuals)

    assert sorted(r.reason for r in result.recoveries) == [
        "final_transition",
        "look_source",
        "photo_treatment",
        "text_effect",
    ]
    assert sorted(result.notes) == [
        "Ended your video on a cut instead of a transition",
        "Showed clip 1 without its look: your iPhone can't draw that yet",
        "Showed photo 2 without its zoom: your iPhone can't draw that yet",
        f"Showed the text “{ARABIC}” with a simple fade-in: your iPhone can't play its "
        "animation yet",
    ]


@pytest.mark.usefixtures("_verified")
def test_a_degrade_that_does_not_fix_its_row_ends_in_the_typed_refusal(monkeypatch):
    """Failure mode 15: the same reject on the same row twice is raised, never looped on."""
    plan, bindings = fixture()
    plan.story_timeline = [
        plan.story_timeline[0].model_copy(update={"look_preset": "faded_analog"})
    ]
    attempts = []

    def no_help(plan, exc):  # noqa: ANN001
        attempts.append(exc.item_id)
        recovery = phone_plan_degrade.Recovery(
            reason=exc.reason, lane=exc.lane, item_id=exc.item_id, action="dropped"
        )
        return plan, [recovery]

    monkeypatch.setattr(phone_plan_degrade, "degrade_plan_for_phone", no_help)

    with pytest.raises(UnsupportedPhonePlan) as refused:
        compile_phone_guided_repaired(plan, bindings)

    assert (refused.value.reason, refused.value.item_id) == ("moment_treatment", "moment")
    assert attempts == ["moment"]


def test_a_reject_that_names_no_known_row_is_left_to_fail():
    plan, _bindings = fixture()
    for exc in (
        UnsupportedPhonePlan("x", reason="moment_treatment", lane="moment", item_id="missing"),
        UnsupportedPhonePlan("x", reason="transition_window", lane="transition", item_id="moment"),
        UnsupportedPhonePlan("x"),
        ValueError("phone timeline duration differs from approved plan"),
    ):
        assert phone_plan_degrade.degrade_plan_for_phone(plan, exc) == (plan, [])


# --- a word-timed text on a Voiceover edit ------------------------------------------------------

# A two-line text at its fitted size runs into the captions below the face; one line fits.
SPOKEN = "Twenty minutes of total darkness on the water"
SPOKEN_WORDS = "yirmi dakika boyunca tam karanlık ve sessizlik içinde bekledik".split()


def _spoken_job(monkeypatch, *, literal: str = SPOKEN, language: str | None = None):
    from app.kria.brief import BriefRequirement, CreativeBrief
    from app.kria.brief_binding import BriefBinding
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_narrated, _words
    from tests.tasks.test_phone_voiceover_requested_edits import _enable_requested_word_text

    words = _words(*((word, 1.0 + 0.3 * i, 1.3 + 0.3 * i) for i, word in enumerate(SPOKEN_WORDS)))
    job, snapshot, _session, _bindings = _setup_narrated(
        monkeypatch,
        spoken_words=words,
        extra_candidates={"creator_strategy": {"opening_title": "Night boat rules"}},
    )
    _enable_requested_word_text(monkeypatch)
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r6",
                kind="text",
                scope="global",
                literal=literal,
                facts={"triggers": ["yirmi dakika"]},
            )
        ],
    )
    binding = BriefBinding.create(uuid.uuid4(), brief).model_dump(mode="json")
    snapshot["creator_brief_binding"] = binding
    job.assembly_plan["creator_brief_binding"] = binding
    if language is not None:
        snapshot["creator_reply_language"] = language
    return job, snapshot


def test_a_spoken_text_with_no_room_shrinks_instead_of_asking(monkeypatch):
    """Failure modes 19-21, through the real Voiceover worker task."""
    job, snapshot = _spoken_job(monkeypatch)
    events = _captured_events(monkeypatch)

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert job.status == "awaiting_device"
    assert "request_recovery" not in job.assembly_plan
    variant = job.assembly_plan["variants"][0]
    [receipt] = variant["spoken_text_placement"]["rows"]
    assert receipt["status"] == "placed"
    [row] = [r for r in variant["narrated_title_text_elements"] if r["id"] == "spoken-text-r6"]
    assert (row["text"], row["start_s"]) == (SPOKEN, pytest.approx(1.0, abs=1 / 30))
    assert 40 <= row["size_px"] < receipt["placement"]["shrunk_from_px"]
    assert variant["phone_repair_notes"] == [
        f"Made your text “{SPOKEN}” smaller so it fits clearly on screen"
    ]
    recovered = [data for stage, event, data in events if stage == "phone_plan"]
    assert recovered == [
        {
            "decision": "render_with_notes",
            "reason": "spoken_text_fit",
            "lane": "spoken_text",
            "item_id": "spoken-text-r6",
            "action": "shrunk",
        }
    ]
    recipe = device_status(job, "narrated").request.recipe
    assert len([layer for layer in recipe.text_layers if layer.id.startswith("title-")]) == 2


def test_without_the_shrink_the_spoken_text_still_asks(monkeypatch):
    """Failure mode 8 for this gate: at its own size the text really has no clear spot."""
    job, snapshot = _spoken_job(monkeypatch)
    monkeypatch.setattr(phone_plan_degrade, "shrink_sizes", lambda size, **_kw: [size])

    with pytest.raises(UnsupportedPhonePlan) as refused:
        gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert (refused.value.reason, refused.value.lane) == ("spoken_text_unplaced", "spoken_text")
    assert "couldn't safely place" in job.assembly_plan["request_recovery"]["message"]


def test_a_turkish_voiceover_chat_gets_the_shrink_note_in_turkish(monkeypatch):
    job, snapshot = _spoken_job(monkeypatch, language="tr")

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert job.assembly_plan["variants"][0]["phone_repair_notes"] == [
        f"“{SPOKEN}” yazını ekrana net sığsın diye küçülttüm"
    ]


def test_a_spoken_text_that_fits_keeps_its_size_and_says_nothing(monkeypatch):
    """Failure mode 20: only a text with no clear spot at its own size changes."""
    job, snapshot = _spoken_job(monkeypatch, literal="20 dk")
    events = _captured_events(monkeypatch)

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    variant = job.assembly_plan["variants"][0]
    [receipt] = variant["spoken_text_placement"]["rows"]
    assert "shrunk_from_px" not in receipt["placement"]
    [row] = [r for r in variant["narrated_title_text_elements"] if r["id"] == "spoken-text-r6"]
    assert row["size_px"] == 120
    assert "phone_repair_notes" not in variant
    assert not [data for stage, _event, data in events if stage == "phone_plan"]


# --- the fourth verifier's findings -----------------------------------------------------------

OTHER_ARABIC = "شكرا جزيلا"


@pytest.mark.usefixtures("_verified")
@pytest.mark.parametrize(
    ("words", "note"),
    [
        (
            (ARABIC, ARABIC, OTHER_ARABIC),
            "Showed 3 texts with a simple fade-in: your iPhone can't play their animations yet",
        ),
        (
            (ARABIC, ARABIC),
            "Showed 2 texts with a simple fade-in: your iPhone can't play their animations yet",
        ),
    ],
)
def test_two_texts_with_the_same_words_are_two_rows_in_the_note(words, note):
    """Failure mode 24."""
    plan, bindings = fixture()
    plan.text_elements = [
        TextElement(
            id=f"rtl-{index}",
            text=text,
            start_s=0.4 * index,
            end_s=0.4 * index + 1.0,
            effect="staggered-slice",
        )
        for index, text in enumerate(words)
    ]

    result = compile_phone_guided_repaired(plan, bindings)

    assert sorted(r.item_id for r in result.recoveries) == [f"rtl-{i}" for i in range(len(words))]
    assert result.notes == [note]


def test_a_guided_render_the_job_no_longer_owns_records_no_recovery(harness, monkeypatch):  # noqa: F811
    """Failure mode 25: the job is cancelled while its plan compiles."""
    job, *_ = harness(brief=_brief())
    _stamp_contract(job)
    events = _captured_events(monkeypatch)
    monkeypatch.setattr(gb, "_fail_job", Mock(return_value=True))
    _injecting(monkeypatch, job, _set(BAD, look_preset="faded_analog"))
    degrade = phone_plan_degrade.degrade_plan_for_phone
    degraded: list[str] = []

    def cancelled_meanwhile(plan, exc):  # noqa: ANN001
        job.status = gb._CANCELLED_JOB_STATUS
        result = degrade(plan, exc)
        degraded.extend(r.item_id for r in result[1])
        return result

    monkeypatch.setattr(phone_plan_degrade, "degrade_plan_for_phone", cancelled_meanwhile)

    gb._run_generative_job(str(job.id))

    assert degraded, "the bad row degraded"
    assert job.status == gb._CANCELLED_JOB_STATUS
    assert not job.assembly_plan.get("variants")
    assert [event for stage, event, _data in events if stage == "phone_plan"] == []


def test_a_voiceover_render_the_job_no_longer_owns_records_no_recovery(monkeypatch):
    """Failure mode 25, on the Voiceover worker's word-timed text shrink."""
    job, snapshot = _spoken_job(monkeypatch)
    events = _captured_events(monkeypatch)
    sizes = phone_plan_degrade.shrink_sizes

    def cancelled_meanwhile(size, **kwargs):  # noqa: ANN001
        job.status = gb._CANCELLED_JOB_STATUS
        return sizes(size, **kwargs)

    monkeypatch.setattr(phone_plan_degrade, "shrink_sizes", cancelled_meanwhile)

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert job.status == gb._CANCELLED_JOB_STATUS
    assert not job.assembly_plan.get("variants")
    assert not [data for stage, _event, data in events if stage == "phone_plan"]


@pytest.mark.parametrize(
    ("language", "note"),
    [
        ("en", "Left out the captions: your iPhone can't draw them yet"),
        ("tr", "Altyazıları çıkardım: iPhone'un onları henüz çizemiyor"),
    ],
)
def test_a_caption_fault_drops_the_captions_not_the_video_or_its_beats(language, note, monkeypatch):
    """Failure modes 26 and 27, through the real Voiceover worker task with two beat cards."""
    from app.kria.brief_checks import NARRATED_ALIGNMENT_FIELD
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_voiceover_beats

    job, snapshot, _bind_calls, _transcribe = _setup_voiceover_beats(monkeypatch)
    snapshot["creator_reply_language"] = language
    events = _captured_events(monkeypatch)

    def unreadable(cues, **_kwargs):  # noqa: ANN001
        raise ValueError("no face draws this cue")

    monkeypatch.setattr("app.pipeline.phone_captions.compile_caption_layers", unreadable)

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert job.status == "awaiting_device"
    [variant] = job.assembly_plan["variants"]
    assert variant["caption_cues"] is None
    receipt = variant["phone_beat_receipt"]
    assert [entry["beat_id"] for entry in receipt["placed"]] == ["medal", "finish-time"]
    assert receipt["unplaced"] == []
    assert note in variant["phone_repair_notes"]
    recipe = device_status(job, "narrated").request.recipe
    assert not [layer for layer in recipe.text_layers if layer.id.startswith("caption")]
    assert "subtitled-overlays" in {track.id for track in recipe.tracks}
    assert job.assembly_plan[NARRATED_ALIGNMENT_FIELD]["caption_language"] is None
    recovered = [data for stage, event, data in events if stage == "phone_plan"]
    assert recovered == [
        {
            "decision": "render_with_notes",
            "reason": "captions_compile",
            "lane": "captions",
            "item_id": None,
            "action": "dropped",
        }
    ]
