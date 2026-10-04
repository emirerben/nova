"""KRI-286: planning-time repair of phone-inexpressible guided plans.

One case per prod failure that surfaced only AFTER approval as a terminal
`phone_plan_unsupported`:
  76db6913  "sequence effect needs composite-stream parity"
  31eb638e  "phone transition needs the full source window"
  b33e1c88  "This font instance awaits native parity and device qualification"
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import get_args

import pytest

from app.agents._schemas.text_element import TextElement
from app.config import settings
from app.kria.recipes import MediaCapability
from app.pipeline import phone_guided_plan, phone_plan_repair
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.phone_plan_repair import (
    COMPOSITE_SAFE_SEQUENCE_EFFECTS,
    PhoneProposalRejected,
    compile_phone_guided_repaired,
    compile_phone_guided_with_repairs,
    validate_proposal_phone_compiles,
)
from app.services.phone_rollout import (
    PhoneCapabilityUnavailable,
    PhoneFontUnqualified,
    validate_phone_pilot_recipe,
)
from tests.pipeline.test_phone_guided_plan import fixture, transition_fixture


@pytest.fixture(autouse=True)
def _all_capabilities_verified(monkeypatch):
    monkeypatch.setattr(settings, "phone_font_qualification_strict", False)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(get_args(MediaCapability)))


def _sequence(effect: str) -> list[TextElement]:
    return [
        TextElement(
            id=f"seq{index}",
            text=text,
            role="generative_sequence",
            effect=effect,
            start_s=index * 0.5,
            end_s=2.5,
        )
        for index, text in enumerate(("First", "Second"))
    ]


# --- 76db6913: sequence pop-in ------------------------------------------------


def test_sequence_pop_in_is_repaired_to_fade_in_with_a_note():
    plan, bindings = fixture()
    plan.text_elements = _sequence("pop-in")
    with pytest.raises(UnsupportedPhonePlan) as raised:
        compile_phone_guided_plan(plan, bindings)
    assert raised.value.reason == "sequence_effect"

    result = compile_phone_guided_repaired(plan, bindings)

    assert [layer.effect for layer in result.recipe.text_layers] == ["fade-in", "fade-in"]
    assert result.notes == [
        "Swapped a text animation your iPhone can't play yet for a simple fade-in"
    ]
    # The repaired plan is returned (the worker persists it); the input is untouched.
    assert result.plan is not plan
    assert {element.effect for element in result.plan.text_elements} == {"fade-in"}
    assert {element.effect for element in plan.text_elements} == {"pop-in"}


@pytest.mark.parametrize("effect", sorted(COMPOSITE_SAFE_SEQUENCE_EFFECTS))
def test_composite_safe_effects_compile_without_repair(effect):
    plan, bindings = fixture()
    plan.text_elements = _sequence(effect)
    compile_phone_guided_plan(plan, bindings)  # the allowlist mirrors the real fence
    assert compile_phone_guided_repaired(plan, bindings).notes == []


def test_only_sequence_lane_elements_are_repaired():
    plan, bindings = fixture()
    plan.text_elements = [
        *_sequence("pop-in"),
        TextElement(id="title", text="Title", start_s=0, end_s=2, effect="pop-in"),
    ]
    result = compile_phone_guided_repaired(plan, bindings)
    effects = {element.id: element.effect for element in result.plan.text_elements}
    assert effects == {"seq0": "fade-in", "seq1": "fade-in", "title": "pop-in"}


# --- b33e1c88: unqualified font -----------------------------------------------


def _unqualified_variable_font_plan(monkeypatch):
    """A variable face whose variation coordinates did not resolve (the gate's trigger)."""
    plan, bindings = fixture()
    plan.text_elements = [
        TextElement(
            id="title",
            text="This view",
            start_s=0.5,
            end_s=2.5,
            font_family="Outfit",
            effect="fade-in",
            size_px=64,
        )
    ]
    monkeypatch.setattr(
        "app.pipeline.portable_text_layout.resolved_font_variations", lambda _font: {}
    )
    return plan, bindings


def test_unqualified_font_is_swapped_for_a_qualified_default_with_a_note(monkeypatch):
    plan, bindings = _unqualified_variable_font_plan(monkeypatch)
    recipe = compile_phone_guided_plan(plan, bindings)
    with pytest.raises(PhoneFontUnqualified) as raised:
        validate_phone_pilot_recipe(recipe)
    assert raised.value.font_files == {"Outfit-VF.ttf"}

    result = compile_phone_guided_repaired(plan, bindings)

    assert result.notes == ["Changed a text font to Inter so it renders on your iPhone"]
    assert result.plan.text_elements[0].font_family == "Inter"
    assert plan.text_elements[0].font_family == "Outfit"
    font_ids = {run.font_asset_id for layer in result.recipe.text_layers for run in layer.runs}
    assert font_ids == {"font-Inter-Bold.ttf"}
    validate_phone_pilot_recipe(result.recipe)


def test_strict_font_qualification_is_not_repaired(monkeypatch):
    plan, bindings = fixture()
    plan.text_elements = [
        TextElement(id="t", text="This view", start_s=0.5, end_s=2.5, font_family="Outfit")
    ]
    monkeypatch.setattr(settings, "phone_font_qualification_strict", True)
    # Strict mode qualifies exact byte/coordinate instances only: no family can be assumed.
    with pytest.raises(PhoneFontUnqualified):
        compile_phone_guided_with_repairs(plan, bindings)


# --- 31eb638e: transition window ----------------------------------------------


def test_transition_hole_is_not_papered_over():
    plan, bindings = transition_fixture()
    bindings[0].original.duration_s = 2.5  # outgoing clip cannot reach the incoming one
    with pytest.raises(UnsupportedPhonePlan) as raised:
        compile_phone_guided_with_repairs(plan, bindings)
    assert raised.value.reason == "transition_window"


# --- shared behaviours ----------------------------------------------------------


def test_expressible_plan_is_a_no_op():
    plan, bindings = fixture()
    plan.text_elements = _sequence("fade-in")
    result = compile_phone_guided_repaired(plan, bindings)
    assert result.notes == []
    assert result.plan is plan
    recipe, notes = compile_phone_guided_with_repairs(plan, bindings)
    assert notes == []
    assert recipe.model_dump() == result.recipe.model_dump()


def test_capability_unavailable_is_never_repaired(monkeypatch):
    plan, bindings = fixture()
    plan.text_elements = _sequence("fade-in")
    monkeypatch.setattr(settings, "phone_render_verified_features", [])
    with pytest.raises(PhoneCapabilityUnavailable) as raised:
        compile_phone_guided_with_repairs(plan, bindings)
    assert "basicComposition" in raised.value.capability


def test_kwargs_are_forwarded_verbatim(monkeypatch):
    plan, bindings = fixture()
    seen: list[dict] = []
    real = phone_guided_plan.compile_phone_guided_plan

    def spy(*args, **kwargs):
        seen.append({"args": args, "kwargs": kwargs})
        return real(*args, **kwargs)

    monkeypatch.setattr(phone_guided_plan, "compile_phone_guided_plan", spy)
    compile_phone_guided_with_repairs(
        plan, bindings, visuals=(), narration=None, allow_editor_media=False
    )
    assert seen == [
        {
            "args": (plan, bindings),
            "kwargs": {"visuals": (), "narration": None, "allow_editor_media": False},
        }
    ]


def test_a_repair_that_does_not_help_stops_after_one_pass(monkeypatch):
    plan, bindings = fixture()
    plan.text_elements = _sequence("pop-in")
    calls = []

    def always_failing(plan_, bindings_, **kwargs):
        calls.append(plan_)
        raise UnsupportedPhonePlan(
            "sequence effect needs composite-stream parity", reason="sequence_effect"
        )

    monkeypatch.setattr(phone_guided_plan, "compile_phone_guided_plan", always_failing)
    with pytest.raises(UnsupportedPhonePlan):
        compile_phone_guided_with_repairs(plan, bindings)
    assert len(calls) == 2  # original + exactly one repaired retry


# --- planning-time dry run ------------------------------------------------------


def _snapshot_for(plan, bindings):
    return SimpleNamespace(
        media=[
            SimpleNamespace(
                lane="clip",
                gcs_path=binding.proxy_path,
                media_id=binding.media_id,
                analysis={},
                duration_s=10,
            )
            for binding in bindings
        ]
    )


def _stub_dry_run(monkeypatch, plan, bindings):
    monkeypatch.setattr(
        "app.pipeline.guided_story.compile_proposal_execution_plan",
        lambda _snapshot: plan.model_dump(mode="json"),
    )
    monkeypatch.setattr(
        "app.services.phone_sources.bind_phone_sources", lambda _assignments, _paths: bindings
    )


def test_dry_run_repairs_and_reports_notes(monkeypatch):
    plan, bindings = fixture()
    plan.text_elements = _sequence("pop-in")
    _stub_dry_run(monkeypatch, plan, bindings)
    notes = validate_proposal_phone_compiles(_snapshot_for(plan, bindings), [])
    assert notes == ["Swapped a text animation your iPhone can't play yet for a simple fade-in"]


def test_dry_run_is_silent_for_an_expressible_plan(monkeypatch):
    plan, bindings = fixture()
    plan.text_elements = _sequence("fade-in")
    _stub_dry_run(monkeypatch, plan, bindings)
    assert validate_proposal_phone_compiles(_snapshot_for(plan, bindings), []) == []


def test_dry_run_rejects_what_no_repair_fixes(monkeypatch):
    plan, bindings = transition_fixture()
    bindings[0].original.duration_s = 2.5
    _stub_dry_run(monkeypatch, plan, bindings)
    with pytest.raises(PhoneProposalRejected, match="full source window"):
        validate_proposal_phone_compiles(_snapshot_for(plan, bindings), [])


def test_dry_run_skips_when_receipts_cannot_be_bound(monkeypatch):
    plan, bindings = fixture()
    plan.text_elements = _sequence("pop-in")
    _stub_dry_run(monkeypatch, plan, bindings)

    def unbindable(_assignments, _paths):
        raise ValueError("phone source lacks its verified proxy receipt")

    monkeypatch.setattr("app.services.phone_sources.bind_phone_sources", unbindable)
    # The dispatch gate stays the authority for receipts; the dry run never blocks on them.
    assert validate_proposal_phone_compiles(_snapshot_for(plan, bindings), []) == []


def test_a_second_font_cause_is_repaired_in_a_second_pass(monkeypatch):
    """A layer can fail on its font AND its effect; each cause gets its own pass."""
    plan, bindings = fixture()
    plan.text_elements = [
        TextElement(id="t", text="Hello", start_s=0, end_s=2, font_family="Outfit", effect="pop-in")
    ]
    rejects = [
        PhoneFontUnqualified("font", font_files=frozenset({"Outfit-VF.ttf"})),
        PhoneFontUnqualified("font", effects=frozenset({"pop-in"})),
    ]
    seen = []

    def staged(plan_, bindings_, **kwargs):
        seen.append(plan_)
        if rejects:
            raise rejects.pop(0)
        return "recipe"

    monkeypatch.setattr(phone_guided_plan, "compile_phone_guided_plan", staged)
    monkeypatch.setattr(phone_plan_repair, "validate_phone_pilot_recipe", lambda *_a, **_k: None)
    result = compile_phone_guided_repaired(plan, bindings)
    element = result.plan.text_elements[0]
    assert (element.font_family, element.effect) == ("Inter", "fade-in")
    assert len(result.notes) == 2 and len(seen) == 3


def test_dry_run_treats_a_capability_gap_as_advisory_not_a_plan_defect(monkeypatch):
    plan, bindings = fixture()
    plan.text_elements = _sequence("fade-in")
    _stub_dry_run(monkeypatch, plan, bindings)
    monkeypatch.setattr(settings, "phone_render_verified_features", ["basicComposition"])
    # The dispatch gate and worker own capability gating: no fallback, no rejection.
    assert validate_proposal_phone_compiles(_snapshot_for(plan, bindings), []) == []


def test_dry_run_never_lets_an_unexpected_error_escape(monkeypatch):
    def boom(_snapshot):
        raise KeyError("planner shape changed")

    monkeypatch.setattr("app.pipeline.guided_story.compile_proposal_execution_plan", boom)
    assert validate_proposal_phone_compiles(SimpleNamespace(media=[]), []) == []


def test_dry_run_forwards_compile_kwargs_verbatim(monkeypatch):
    plan, bindings = fixture()
    plan.text_elements = _sequence("fade-in")
    _stub_dry_run(monkeypatch, plan, bindings)
    seen = []
    real = phone_guided_plan.compile_phone_guided_plan

    def spy(plan_, bindings_, **kwargs):
        seen.append(kwargs)
        return real(plan_, bindings_, **{k: v for k, v in kwargs.items() if k != "extra_kw"})

    monkeypatch.setattr(phone_guided_plan, "compile_phone_guided_plan", spy)
    validate_proposal_phone_compiles(_snapshot_for(plan, bindings), [], extra_kw="x")
    assert seen[0]["extra_kw"] == "x"


def test_dry_run_synthesizes_a_narration_bed(monkeypatch):
    from tests.pipeline.test_phone_guided_plan import narration_fixture

    plan, bindings, visuals, _bed = narration_fixture()
    _stub_dry_run(monkeypatch, plan, bindings)
    snapshot = SimpleNamespace(
        media=[
            *_snapshot_for(plan, bindings).media,
            *[
                SimpleNamespace(
                    lane="asset",
                    gcs_path=v.gcs_path,
                    media_id=v.media_id,
                    analysis={},
                    duration_s=None,
                )
                for v in visuals
            ],
        ]
    )
    # Voiceover + pool photos compile through the dry run (no I/O, dummy digests).
    assert validate_proposal_phone_compiles(snapshot, []) == []
    assert phone_plan_repair._dry_run_bindings(plan, snapshot, [])[2] is not None


def test_dry_run_skips_a_pool_video_it_cannot_describe(monkeypatch):
    plan, bindings = fixture()
    plan.story_timeline[0] = plan.story_timeline[0].model_copy(
        update={"lane": "asset", "kind": "video", "media_id": "pool"}
    )
    snapshot = SimpleNamespace(
        media=[
            SimpleNamespace(
                lane="asset",
                gcs_path="g",
                media_id="pool",
                analysis={},
                duration_s=3.0,
            )
        ]
    )
    assert phone_plan_repair._dry_run_bindings(plan, snapshot, []) is None


def test_landscape_fit_survives_the_repair_wrapper(monkeypatch):
    """Lane 1's `landscape_fit` kwarg reaches the compiler untouched, also across a repair."""
    plan, bindings = fixture()  # 1920x1080 source on the portrait canvas
    plan.text_elements = _sequence("pop-in")
    seen: list[str | None] = []
    real = phone_guided_plan.compile_phone_guided_plan

    def spy(plan_, bindings_, **kwargs):
        seen.append(kwargs.get("landscape_fit"))
        return real(plan_, bindings_, **kwargs)

    monkeypatch.setattr(phone_guided_plan, "compile_phone_guided_plan", spy)
    fit = compile_phone_guided_repaired(plan, bindings, landscape_fit="fit")
    fill = compile_phone_guided_repaired(plan, bindings, landscape_fit="fill")

    assert seen == ["fit", "fit", "fill", "fill"]  # reject + repaired retry, both forwarded
    assert fit.notes  # the sequence repair happened alongside the fit
    fit_transform = fit.recipe.tracks[0].clips[0].transform
    fill_transform = fill.recipe.tracks[0].clips[0].transform
    assert fit_transform != fill_transform  # letterboxed vs cropped: the kwarg took effect
