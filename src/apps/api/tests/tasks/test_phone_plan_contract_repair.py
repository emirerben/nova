"""Approved phone edits are repaired toward their contract, never refused for it (KRI-129).

Prod, 2026-09-24 .. 2026-10-24: 30 of 133 phone jobs were refused before render with
``phone_plan_unsupported``. The shapes replayed here, each through the REAL worker task
(``_run_generative_job`` -> unified montage planner -> guided compiler -> pin-time
``verify_phone_recipe``), with only storage, the DB row and the model steps fixed:

  150d30b4 / be180d9f  "This edit is missing confirmed on-screen text." -- the confirmed
                       opening title ("... 😂") was replaced by the planner's own title.
  7bb664fb             a title asked for with no words (covered in
                       ``test_unified_montage_dispatch.py``).
  0701fcc9 / f88555bc  "I can't verify this ordering rule from the approved media." on a
                       ONE-take Talking edit whose clip row had no media_id.
  5df2e3ec             raw Pydantic "text layer exceeds the timeline".
  confirmed length     the footage could not fill the asked length: renders, and says so.

Ways this could fail, written before the code (each has a test below):
  1. the repair clobbers a title that is itself another confirmed literal;
  2. the repair duplicates words already on screen (exact, or as a word sequence);
  3. injected words the font has no glyph for (emoji) fail to compile;
  4. an injected window runs past the timeline and EditRecipeV2 rejects it;
  5. a copied look carries ``text_case``/sequence lineage, so the drawn words differ;
  6. a repair is silent: no note on the variant, no pipeline event;
  7. the one-clip contract fix relaxes a real multi-clip order;
  8. a length note shows when the pin-time check still refuses (or when the length was kept);
  9. disabling the repair does NOT reproduce prod (the test would prove nothing).
"""

from __future__ import annotations

import copy
from typing import get_args
from unittest.mock import Mock

import pytest

from app.agents._schemas.text_element import TextElement
from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.recipes import MediaCapability
from app.pipeline import phone_plan_repair
from app.pipeline.phone_plan_repair import compile_phone_guided_repaired, repair_plan_for_contract
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
    CreatorRenderContract,
    TextRequirement,
    build_render_contract,
    verify_phone_recipe,
)
from app.services.device_render import device_status
from app.services.length_policy_state import stamp_length_policy
from app.tasks import generative_build as gb
from tests.pipeline.test_phone_guided_plan import fixture
from tests.tasks.test_unified_montage_dispatch import _brief, harness  # noqa: F401

MARATHON = "When you have a marathon at 8 but jail at 9 😂"


def _stamp_contract(job, **changes) -> CreatorRenderContract:
    contract = CreatorRenderContract(generation_id="generation").rebind(**changes)
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    job.all_candidates[REQUIREMENT_VERSION_FIELD] = 1
    return contract


def _captured_events(monkeypatch) -> list[tuple[str, str, dict]]:
    import app.services.pipeline_trace as pipeline_trace

    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        pipeline_trace,
        "record_pipeline_event",
        lambda stage, event, data=None: events.append((stage, event, data or {})),
    )
    return events


# --- 150d30b4 / be180d9f: the confirmed title is put back, and the creator is told --------


def test_prod_150d30b4_the_confirmed_title_renders_instead_of_a_refusal(harness, monkeypatch):  # noqa: F811
    job, *_ = harness(brief=_brief())
    _stamp_contract(
        job, exact_texts=(TextRequirement(role="opening", text=MARATHON, duration_s=2.0),)
    )
    events = _captured_events(monkeypatch)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    failure.assert_not_called()
    assert job.status == "awaiting_device"
    recipe = device_status(job, "guided_story").request.recipe
    drawn = ["".join(run.text for run in layer.runs) for layer in recipe.text_layers]
    assert MARATHON.replace(" ", "") in [text.replace(" ", "") for text in drawn]
    variant = job.assembly_plan["variants"][0]
    note = f"Used your title “{MARATHON}” exactly as you wrote it"
    assert note in variant["phone_repair_notes"]
    # The editor recompiles from the persisted rows: they carry the creator's words too.
    assert MARATHON in [row["text"] for row in variant["text_elements"]]
    repaired = [e for e in events if e[1] == "phone_plan_repaired"]
    assert repaired and note in repaired[-1][2]["notes"]
    # What iOS reads: the thread's job projection `render_notes` ("What Kria changed").
    import app.routes.creation_threads as threads_routes
    import app.routes.generative_jobs as gj

    monkeypatch.setattr(gj.storage, "signed_get_url", lambda path, **_: f"https://s/{path}")
    job.current_phase = None  # a real Job row's column; the harness row lacks it
    assert note in threads_routes._job_projection(job)["render_notes"]


def test_without_the_repair_the_prod_refusal_reproduces(harness, monkeypatch):  # noqa: F811
    """Guard for failure mode 9: the fixture really is prod's refusal shape."""
    job, *_ = harness(brief=_brief())
    _stamp_contract(job, exact_texts=(TextRequirement(role="opening", text=MARATHON),))
    monkeypatch.setattr(
        phone_plan_repair, "repair_plan_for_contract", lambda plan, _contract, **_k: (plan, [])
    )
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    assert failure.call_args.kwargs["failure_reason"] == "phone_plan_unsupported"
    assert failure.call_args.args[1] == "This edit is missing confirmed on-screen text."


# --- the repair itself: no clobbering, no duplicates, renderable words ---------------------


def _plan_with(*elements: TextElement):
    plan, bindings = fixture()
    plan.text_elements = list(elements)
    return plan, bindings


def _title(text: str, **extra) -> TextElement:
    return TextElement(id="guided-title", text=text, start_s=0.0, end_s=2.0, **extra)


@pytest.fixture
def _verified(monkeypatch):
    monkeypatch.setattr(settings, "phone_font_qualification_strict", False)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(get_args(MediaCapability)))


def _verify(contract, recipe) -> None:
    verify_phone_recipe(contract, recipe, source_audio={"source": True})


@pytest.mark.usefixtures("_verified")
def test_a_title_that_is_another_confirmed_text_is_never_overwritten():
    plan, bindings = _plan_with(_title("Day one"))
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(
            TextRequirement(role="opening", text="Day one"),
            TextRequirement(role="any", text="Lisbon"),
        )
    )
    result = compile_phone_guided_repaired(plan, bindings, contract=contract)
    texts = [element.text for element in result.plan.text_elements]
    assert texts == ["Day one", "Lisbon"]
    assert result.notes == ["Added your text “Lisbon” on screen"]
    _verify(contract, result.recipe)


@pytest.mark.usefixtures("_verified")
def test_words_already_on_screen_as_a_sequence_are_left_alone():
    words = "come with me".split()
    plan, bindings = _plan_with(
        *(
            TextElement(
                id=f"guided-title::sequence-{index + 1}",
                text=word,
                start_s=index * 0.5,
                end_s=(index + 1) * 0.5,
                effect="fade-in",
                role="generative_sequence",
            )
            for index, word in enumerate(words)
        )
    )
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role="opening", text="come with me"),)
    )
    repaired, notes = repair_plan_for_contract(plan, contract)
    assert notes == [] and repaired is plan


@pytest.mark.usefixtures("_verified")
def test_a_copied_look_never_changes_the_drawn_words():
    plan, bindings = _plan_with(_title("MY OWN COPY", text_case="upper"))
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role="opening", text="Sabah kahvesi ☕", duration_s=2.5),)
    )
    result = compile_phone_guided_repaired(plan, bindings, contract=contract)
    [title] = result.plan.text_elements
    assert title.text == "Sabah kahvesi ☕" and title.text_case is None
    assert title.start_s == 0.0 and title.end_s == 2.5
    _verify(contract, result.recipe)


@pytest.mark.usefixtures("_verified")
def test_closing_and_clip_texts_land_inside_the_timeline():
    plan, bindings = _plan_with()
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(
            TextRequirement(role="closing", text="See you", duration_s=10.0),
            TextRequirement(role="clip", text="Belém", media_id="source"),
        )
    )
    result = compile_phone_guided_repaired(plan, bindings, contract=contract)
    total = plan.resolved_duration_s
    assert all(0.0 <= e.start_s < e.end_s <= total for e in result.plan.text_elements)
    assert result.notes == [
        "Added your text “See you” on screen",
        "Added your text “Belém” on its clip",
    ]
    assert len(result.recipe.text_layers) == 2


@pytest.mark.usefixtures("_verified")
def test_prod_5df2e3ec_text_past_the_timeline_is_clamped_not_a_raw_error():
    plan, bindings = _plan_with(
        TextElement(id="late-edge", text="Galata", start_s=1.0, end_s=3.4),
        TextElement(id="wholly-late", text="Karaköy", start_s=3.5, end_s=4.0),
    )
    with pytest.raises(ValueError, match="text layer exceeds the timeline"):
        phone_plan_repair.phone_guided_plan.compile_phone_guided_plan(plan, bindings)

    result = compile_phone_guided_repaired(plan, bindings)

    assert [(e.id, e.end_s) for e in result.plan.text_elements] == [("late-edge", 3.0)]
    assert [(layer.start, layer.end) for layer in result.recipe.text_layers] == [(1.0, 3.0)]
    assert result.notes == [
        "Ended a text that ran past the end of your video with the video",
        "Left out a text that started after your video ends",
    ]


# --- 0701fcc9 / f88555bc: a one-take Talking edit has no order to refuse -------------------


def _talking_order_contract(*, clips: int, keyed: bool):
    ids = [f"analysis-proxy-ios-{index}.mp4" for index in range(clips)]
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r2",
                kind="order",
                scope="global",
                description="in the order the clips were added",
                facts={
                    "key": "attachment",
                    "start": "Hi, I'm Karolina, I'm from Ecuador",
                    "end": "I'm gonna be here all week",
                },
            )
        ],
    )
    return build_render_contract(
        {"edit_format": "subtitled", "render_program": "native", "selected_media_ids": ids},
        generation_id="g",
        brief=brief,
        media_snapshot={
            "clip_assignments": [
                {"kind": "video", **({"media_id": media_id} if keyed else {})} for media_id in ids
            ]
        },
    )


def test_prod_0701fcc9_a_single_unkeyed_take_pins_no_unverifiable_order():
    contract = _talking_order_contract(clips=1, keyed=False)
    assert contract is not None
    assert contract.unresolved == ()
    assert contract.order_required is False


def test_a_real_multi_clip_order_is_still_asked_about():
    contract = _talking_order_contract(clips=2, keyed=False)
    assert contract is not None and contract.unresolved


# --- a confirmed length the footage cannot fill renders, and says so -----------------------


def _stamp_length(job, contract, mode: str) -> None:
    job.assembly_plan.update(
        stamp_length_policy(
            job.assembly_plan, job.all_candidates["creator_strategy"], contract, mode=mode
        )
    )


def test_a_length_the_footage_cannot_fill_renders_with_a_note(harness, monkeypatch):  # noqa: F811
    job, *_ = harness(brief=_brief())
    contract = _stamp_contract(job, duration_s=600.0)
    _stamp_length(job, contract, "log_only")
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    failure.assert_not_called()
    assert job.status == "awaiting_device"
    duration = device_status(job, "guided_story").request.recipe.duration
    [note] = [n for n in job.assembly_plan["variants"][0]["phone_repair_notes"] if "seconds" in n]
    assert note == (
        f"Your clips fill about {duration:.0f} seconds, so the video is that long instead of "
        "the 600 seconds you asked for"
    )


def test_an_enforced_length_still_refuses_and_carries_no_note(harness, monkeypatch):  # noqa: F811
    job, *_ = harness(brief=_brief())
    contract = _stamp_contract(job, duration_s=600.0)
    _stamp_length(job, contract, "enforce")
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    assert failure.call_args.kwargs["failure_reason"] == "phone_plan_unsupported"
    assert "600 seconds" in failure.call_args.args[1]
    # (The harness session never rolls back, so the uncommitted row is still visible.)
    for variant in job.assembly_plan.get("variants") or []:
        assert not [n for n in variant.get("phone_repair_notes") or [] if "seconds" in n]


def test_a_kept_length_has_no_note(harness, monkeypatch):  # noqa: F811
    job, *_ = harness(brief=_brief())
    gb._run_generative_job(str(job.id))
    natural = copy.deepcopy(device_status(job, "guided_story").request.recipe.duration)

    job2, *_ = harness(brief=_brief())
    contract = _stamp_contract(job2, duration_s=round(natural, 1))
    _stamp_length(job2, contract, "log_only")
    gb._run_generative_job(str(job2.id))

    assert job2.status == "awaiting_device"
    notes = job2.assembly_plan["variants"][0].get("phone_repair_notes") or []
    assert not [n for n in notes if "seconds" in n]
