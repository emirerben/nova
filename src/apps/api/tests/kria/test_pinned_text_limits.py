"""KRI-526: pins next to an opening title, and pin limits that ask instead of failing.

Failure modes (written before the code):

* the opening title (centred at y 0.16) overlaps a top pin (y 0.12, ~0.04 tall) for the whole
  title hold, and face placement can move the title back up into the pin band;
* the title is moved when there is NO pin on screen with it (bottom pin, or a ranged pin that
  starts after the title hold) - a pin-free or bottom-pin snapshot must stay byte-identical;
* more than 4 lines or a line over 120 characters fails the strategy's schema (retry, then a
  generic planning failure) instead of asking about the pins;
* an over-limit strategy that slips past the check crashes the strict snapshot.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.strategy_policy import _refusal_question
from app.pipeline.guided_story import compile_execution_plan
from app.pipeline.pinned_text import (
    PIN_LINE_STEP,
    PIN_TOP_Y,
    title_y_clear_of_pins,
    top_pin_edge_from_rows,
)
from app.pipeline.unified_montage import plan_unified_montage
from app.schemas.edit_proposal import EditProposalSnapshot
from app.services.creator_capabilities import CreatorStrategyError
from tests.pipeline.test_unified_montage import clip

TOP = {"text": "Part 1", "corner": "top_left"}
BOTTOM = {"text": "Day one", "corner": "bottom_left"}


def _title_y(**strategy: object) -> float:
    plan = plan_unified_montage(
        [clip(i, minutes=i) for i in range(3)],
        None,
        strategy={"opening_title": "Lisbon", "opening_title_duration_s": 2, **strategy},
    )
    compiled = compile_execution_plan(plan.guided_edit(), track=None)
    [title] = [e for e in compiled["text_elements"] if e["id"] == "guided-title"]
    return title["y_frac"]


def test_the_title_steps_below_a_top_pin_that_shares_its_hold() -> None:
    plain = _title_y()
    assert plain == 0.16
    with_pin = _title_y(pinned_texts=[TOP])
    # The title block's top edge clears the pin's bottom edge.
    assert with_pin > plain
    two = _title_y(pinned_texts=[TOP, {"text": "Line 2", "corner": "top_left"}])
    assert two > with_pin


def test_the_title_stays_put_when_no_top_pin_is_on_screen_with_it() -> None:
    assert _title_y(pinned_texts=[BOTTOM]) == 0.16
    # A ranged pin that only starts after the 2 s hold never meets the title.
    assert _title_y(pinned_texts=[{**TOP, "start_s": 2.5, "end_s": 5.0}]) == 0.16
    assert _title_y(pinned_texts=[{**TOP, "start_s": 0.5, "end_s": 5.0}]) > 0.16


def test_a_longer_title_is_moved_further_than_a_short_one() -> None:
    edge = PIN_TOP_Y + PIN_LINE_STEP / 2
    short = title_y_clear_of_pins(0.16, edge, text="Lisbon", size_px=104, max_width_frac=0.8)
    long = title_y_clear_of_pins(
        0.16, edge, text="Free to do in Lisbon " * 3, size_px=104, max_width_frac=0.8
    )
    assert 0.16 < short < long <= 0.40
    assert title_y_clear_of_pins(0.16, None, text="x", size_px=104, max_width_frac=0.8) == 0.16


def test_face_placement_floor_reads_the_compiled_pin_rows() -> None:
    rows = [
        {"id": "guided-pinned-0", "y_frac": 0.12, "start_s": 0.0, "end_s": 10.0},
        {"id": "guided-pinned-1", "y_frac": 0.86, "start_s": 0.0, "end_s": 10.0},  # bottom
        {"id": "guided-pinned-2", "y_frac": 0.158, "start_s": 6.0, "end_s": 9.0},  # later
        {"id": "guided-title", "y_frac": 0.16, "start_s": 0.0, "end_s": 3.0},
    ]
    assert top_pin_edge_from_rows(rows, 0.0, 3.0) == pytest.approx(0.12 + PIN_LINE_STEP / 2)
    assert top_pin_edge_from_rows(rows, 7.0, 8.0) == pytest.approx(0.158 + PIN_LINE_STEP / 2)
    assert top_pin_edge_from_rows(rows[1:2], 0.0, 3.0) is None


# -- limits ---------------------------------------------------------------------------------


def _strategy(pins: list[dict]) -> CreativeStrategy:
    return CreativeStrategy(
        direction="fast_montage",
        edit_format="montage",
        audio_strategy="licensed_music",
        render_program="guided",
        rationale="x",
        pinned_texts=pins,
    )


def test_over_limit_pins_parse_so_they_can_be_asked_about() -> None:
    assert len(_strategy([TOP] * 5).pinned_texts) == 5
    assert (
        _strategy([{"text": "x" * 130, "corner": "top_left"}]).pinned_texts[0].fits_a_corner
        is False
    )
    with pytest.raises(ValidationError):
        _strategy([TOP] * 13)  # a sanity cap, not a creator-facing limit
    with pytest.raises(ValidationError):
        _strategy([{"text": "x" * 401, "corner": "top_left"}])


def test_the_renderer_side_stays_strict() -> None:
    with pytest.raises(ValidationError):
        EditProposalSnapshot.model_validate(
            _snapshot_with(pinned_texts=[{"text": "x", "corner": "top_left"}] * 5)
        )
    with pytest.raises(ValidationError):
        EditProposalSnapshot.model_validate(
            _snapshot_with(pinned_texts=[{"text": "x" * 130, "corner": "top_left"}])
        )


def _snapshot_with(**updates: object) -> dict:
    plan = plan_unified_montage([clip(i, minutes=i) for i in range(2)], None, strategy={})
    return {**plan.snapshot.model_dump(mode="json"), **updates}


def test_a_stored_over_limit_strategy_is_trimmed_not_crashed_by_the_planner() -> None:
    pins = [{"text": f"Line {i}", "corner": "top_left"} for i in range(6)]
    pins.append({"text": "y" * 130, "corner": "top_left"})
    plan = plan_unified_montage(
        [clip(i, minutes=i) for i in range(2)], None, strategy={"pinned_texts": pins}
    )
    assert [p.text for p in plan.snapshot.pinned_texts] == [f"Line {i}" for i in range(4)]


def test_the_real_check_asks_about_over_limit_pins_instead_of_failing() -> None:
    from app.kria.strategy_policy import RefusedStrategy, check_strategy_for_runtime_v2
    from tests.agents.test_main_creator_agent import _manifest

    manifest = _manifest()
    five = check_strategy_for_runtime_v2(manifest, _strategy([TOP] * 5))
    assert isinstance(five, RefusedStrategy) and five.code == "pinned_text_too_many"

    wide = check_strategy_for_runtime_v2(
        manifest, _strategy([{"text": "word " * 30, "corner": "top_left"}])
    )
    assert isinstance(wide, RefusedStrategy) and wide.code == "pinned_text_too_long"

    ok = check_strategy_for_runtime_v2(manifest, _strategy([TOP] * 4))
    assert not isinstance(ok, RefusedStrategy)
    assert len(ok.strategy.pinned_texts) == 4


def test_over_limit_pins_become_specific_questions() -> None:
    too_many = CreatorStrategyError(
        "pinned_texts_too_many: more corner lines than fit", code="unsupported_treatment"
    )
    refusal = _refusal_question(too_many, _strategy([TOP] * 5))
    assert refusal.code == "pinned_text_too_many"
    assert "4 lines" in refusal.question and refusal.question.endswith("?")

    long_line = "Free to do in Lisbon and everything else you could possibly want " * 3
    too_long = CreatorStrategyError(
        "pinned_texts_too_long: a corner line is longer than one line",
        code="unsupported_treatment",
    )
    refusal = _refusal_question(too_long, _strategy([{"text": long_line, "corner": "top_left"}]))
    assert refusal.code == "pinned_text_too_long"
    assert "Free to do in Lisbon" in refusal.question and "shorten" in refusal.question

    # The generic copy no longer claims the whole video.
    generic = _refusal_question(
        CreatorStrategyError("pinned_texts is not supported by the subtitled renderer"),
        _strategy([TOP]),
    )
    assert generic.code == "pinned_text_unavailable" and "whole video" not in generic.question


# -- the phone writers now draw pins (KRI-527) ----------------------------------------------


def test_pins_are_allowed_on_a_phone_voiceover_montage() -> None:
    """The recorded-voiceover montage writer draws pins on a phone, so a voiceover manifest (whose
    program is "native") is no longer refused them. The voice-behind-footage and spoken-excerpt
    routes are guided montages and were only ever blocked by the `voice_mode` clause removed
    here; `edit_format == "subtitled"` still asks (that condition is unchanged)."""
    from app.kria.strategy_policy import RefusedStrategy, check_strategy_for_runtime_v2
    from app.services.creator_capabilities import (
        CAPABILITY_PHONE_SOURCE_AUDIO,
        CapabilityAvailability,
    )
    from tests.agents.test_main_creator_agent import _manifest

    manifest = _manifest()
    phone = manifest.model_copy(
        update={
            "has_voiceover": True,
            "capabilities": {
                **manifest.capabilities,
                CAPABILITY_PHONE_SOURCE_AUDIO: CapabilityAvailability(available=True),
            },
        }
    )
    own_clip = phone.media[0].media_id
    voiceover = check_strategy_for_runtime_v2(
        phone, _strategy([TOP]).model_copy(update={"selected_media_ids": [own_clip]})
    )
    assert not isinstance(voiceover, RefusedStrategy)
    assert [pin.text for pin in voiceover.strategy.pinned_texts] == ["Part 1"]
