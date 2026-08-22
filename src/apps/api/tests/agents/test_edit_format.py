"""Edit-format vocabulary + content_plan emission (format-aware edit engine, Lane A).

No DB / no network. Locks the defensive contract: an unknown or missing edit_format
must coerce to montage (today's behavior) and never raise — one drifted LLM token
can't 422 a whole content plan.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.content_plan import ContentPlanOutput, PlanItemSpec
from app.agents._schemas.edit_format import (
    AUDIO_LED_EDIT_FORMATS,
    DEFAULT_EDIT_FORMAT,
    EDIT_FORMATS,
    GUIDED_EDIT_FORMATS,
    NARRATED_EDIT_FORMATS,
    coerce_edit_format,
    guided_edit_applicable,
    render_program_for_intent,
)


def test_vocabulary_and_default() -> None:
    assert DEFAULT_EDIT_FORMAT == "montage"
    assert set(EDIT_FORMATS) == {
        "montage",
        "talking_head",
        "day_vlog",
        "single_hero",
        "subtitled",
        "narrated",
        "narrated_planned",
        "narrated_ready",
    }


def test_subtitled_is_not_a_narrated_format() -> None:
    # `subtitled` captions the clip's OWN audio — it needs NO voiceover, so it must
    # stay OUT of the narrated grouping (which gates generation on a voiceover being
    # attached). If this ever flips, subtitled jobs would be blocked for lacking a
    # voiceover they never need. See edit_format.py lockstep note.
    assert "subtitled" in EDIT_FORMATS
    assert "subtitled" not in NARRATED_EDIT_FORMATS


@pytest.mark.parametrize(
    ("raw", "has_voiceover", "expected_program"),
    [
        (None, False, "guided"),
        ("", False, "guided"),
        ("montage", False, "guided"),
        ("DAY VLOG", False, "guided"),
        ("single-hero", False, "guided"),
        ("narrated_ready", False, "native"),
        ("subtitled", False, "native"),
        ("talking_head", False, "native"),
        ("unknown_future_format", False, "native"),
        (42, False, "native"),
        ("montage", True, "native"),
        ("narrated_ready", True, "native"),
    ],
)
def test_render_program_for_intent_is_exhaustive(
    raw: object,
    has_voiceover: bool,
    expected_program: str,
) -> None:
    assert render_program_for_intent(raw, has_voiceover=has_voiceover) == expected_program
    assert guided_edit_applicable(raw, has_voiceover=has_voiceover) is (
        expected_program == "guided"
    )


def test_guided_and_audio_led_vocabularies_are_disjoint_and_exhaustive() -> None:
    assert GUIDED_EDIT_FORMATS.isdisjoint(AUDIO_LED_EDIT_FORMATS)
    assert GUIDED_EDIT_FORMATS | AUDIO_LED_EDIT_FORMATS == set(EDIT_FORMATS)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("talking_head", "talking_head"),
        ("Talking-Head", "talking_head"),
        ("DAY VLOG", "day_vlog"),
        ("single-hero", "single_hero"),
        ("subtitled", "subtitled"),
        ("Subtitled", "subtitled"),
        ("montage", "montage"),
        ("", "montage"),
        ("nonsense", "montage"),
        (None, "montage"),
        (42, "montage"),
    ],
)
def test_coerce_edit_format(raw: object, expected: str) -> None:
    assert coerce_edit_format(raw) == expected


def test_plan_item_spec_defaults_to_montage() -> None:
    spec = PlanItemSpec(day_index=1, theme="t", idea="i")
    assert spec.edit_format == "montage"


def test_plan_item_spec_coerces_bad_value_instead_of_raising() -> None:
    # The LLM emitting a label we don't know must NOT drop the item.
    spec = PlanItemSpec(day_index=1, theme="t", idea="i", edit_format="vibey_montage")
    assert spec.edit_format == "montage"


def test_content_plan_output_round_trips_edit_format() -> None:
    out = ContentPlanOutput(
        items=[
            {"day_index": 1, "theme": "to camera", "idea": "a take", "edit_format": "talking_head"},
            {"day_index": 2, "theme": "a day out", "idea": "trip", "edit_format": "day_vlog"},
        ]
    )
    assert [i.edit_format for i in out.items] == ["talking_head", "day_vlog"]
    assert out.to_dict()["items"][0]["edit_format"] == "talking_head"
