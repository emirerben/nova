"""KRI-525: corner text for PART of the video ("from 5s to 10s", "during the first clip").

Failure modes this file is written against (before the code):

* the new fields are dropped between the strategy and the compiler (`_pinned_texts` used to
  rebuild each pin from `text` + `corner` only), so a ranged pin renders for the whole video;
* a pin WITHOUT a range changes shape (a `null` range key in a dump moves the record hash);
* a clip index with no window (past the last clip) or a window past the end is drawn anyway,
  or the plan claims a line it never draws;
* two pins in one corner at different times are stacked as if they overlapped (or overlapping
  ones are not stacked);
* labels / captions leave the bottom zone for the whole video instead of only while a bottom
  pin is on screen;
* the editor rebases a clip-scoped pin as a whole-video bar (it must follow its clip);
* a seconds range the creator never wrote is trusted.

Render side is the real `plan_unified_montage` -> `compile_execution_plan` ->
`compile_phone_guided_plan`, so "on screen exactly then" is checked on the recipe the phone
would draw. Editor parity lives in `tests/services/test_guided_label_rebase_vectors.py`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.strategy_policy import _pin_range_refusal
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.pinned_text import (
    PIN_LINE_STEP,
    clip_windows_from_moments,
    pinned_text_elements,
    resolve_pin_windows,
)
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.routes.creator_agent import _pin_range_is_grounded
from app.schemas.edit_proposal import PinnedText
from app.services.phone_sources import PhoneSourceBinding
from tests.pipeline.test_unified_montage import clip


def _binding_set(count: int) -> tuple[PhoneSourceBinding, ...]:
    return tuple(
        PhoneSourceBinding(
            media_id=f"c{i}",
            proxy_path=f"users/u/analysis-proxy-c{i}.mp4",
            generation="1",
            original=OriginalMediaDescriptor(
                sha256="a" * 64,
                byte_count=1000,
                duration_s=5,
                width=1920,
                height=1080,
                has_audio=True,
            ),
        )
        for i in range(count)
    )


def _plan(strategy: dict, count: int = 3, brief: CreativeBrief | None = None):  # noqa: ANN202
    return plan_unified_montage(
        [clip(i, minutes=i) for i in range(count)],
        brief_view(brief) if brief is not None else None,
        strategy=strategy,
    )


def _compiled(plan) -> dict:  # noqa: ANN001
    return compile_execution_plan(plan.guided_edit(), track=None)


def _pinned(compiled: dict) -> list[dict]:
    return [e for e in compiled["text_elements"] if e["id"].startswith("guided-pinned-")]


def _clip_windows(compiled: dict) -> list[tuple[float, float]]:
    return clip_windows_from_moments(compiled["story_timeline"], compiled["resolved_duration_s"])


# -- model ---------------------------------------------------------------------------------


def test_a_pin_without_a_range_serialises_exactly_as_before() -> None:
    assert PinnedText(text="Part 1", corner="top_left").model_dump(mode="json") == {
        "text": "Part 1",
        "corner": "top_left",
    }
    plan = _plan({"pinned_texts": [{"text": "Part 1", "corner": "top_left"}]})
    assert plan.record()["pinned_texts"] == [{"text": "Part 1", "corner": "top_left"}]


def test_a_range_is_seconds_or_clip_never_both_and_never_backwards() -> None:
    PinnedText(text="x", corner="top_left", start_s=1, end_s=2)
    PinnedText(text="x", corner="top_left", end_s=3)  # "for the first 3 seconds"
    PinnedText(text="x", corner="top_left", clip=2)
    for bad in (
        {"start_s": 1, "clip": 1},
        {"end_s": 2, "clip": 1},
        {"start_s": 5, "end_s": 5},
        {"start_s": 6, "end_s": 5},
        {"clip": 0},
        {"start_s": -1},
    ):
        with pytest.raises(ValidationError):
            PinnedText(text="x", corner="top_left", **bad)


def test_the_apply_strategy_tool_schema_still_does_not_grow() -> None:
    assert "pinned_texts" not in CreativeStrategy.model_json_schema()["properties"]


# -- resolving a window --------------------------------------------------------------------


def test_windows_clamp_to_the_video_and_drop_what_cannot_be_drawn() -> None:
    clips = [(0.0, 2.0), (2.0, 5.0)]
    pins = [
        PinnedText(text="whole", corner="top_left"),
        PinnedText(text="secs", corner="top_left", start_s=1, end_s=99),
        PinnedText(text="open-ended", corner="top_left", start_s=4),
        PinnedText(text="intro", corner="top_left", end_s=1.5),
        PinnedText(text="clip2", corner="top_left", clip=2),
        PinnedText(text="no such clip", corner="top_left", clip=3),
        PinnedText(text="past the end", corner="top_left", start_s=5),
        PinnedText(text="a blink", corner="top_left", start_s=4.9, end_s=5.0),
    ]
    windows = {w.pin.text: (w.start_s, w.end_s) for w in resolve_pin_windows(pins, 5.0, clips)}
    assert windows == {
        "whole": (0.0, 5.0),
        "secs": (1.0, 5.0),
        "open-ended": (4.0, 5.0),
        "intro": (0.0, 1.5),
        "clip2": (2.0, 5.0),
    }
    # A clip scope with no clip windows at all (a path that cannot say) is dropped, not guessed.
    assert resolve_pin_windows([pins[4]], 5.0, None) == []


def test_ids_stay_tied_to_the_strategy_position_when_a_pin_is_dropped() -> None:
    pins = [
        PinnedText(text="a", corner="top_left", clip=9),
        PinnedText(text="b", corner="top_left"),
    ]
    elements = pinned_text_elements(
        resolve_pin_windows(pins, 4.0, [(0.0, 4.0)]), font_family=None, text_color=None
    )
    assert [e["id"] for e in elements] == ["guided-pinned-1"]


def test_stacking_is_per_window_in_each_corner() -> None:
    def ys(pins: list[PinnedText]) -> list[float]:
        elements = pinned_text_elements(
            resolve_pin_windows(pins, 10.0, None), font_family=None, text_color=None
        )
        return [e["y_frac"] for e in elements]

    top = "top_left"
    # Disjoint in time: they share the corner's first line.
    a, b = (
        PinnedText(text=t, corner=top, start_s=s, end_s=e) for t, s, e in (("a", 0, 4), ("b", 5, 9))
    )
    assert ys([a, b]) == [0.12, 0.12]
    # Overlapping: they stack in list order.
    c = PinnedText(text="c", corner=top, start_s=3, end_s=8)
    assert ys([a, c]) == [0.12, round(0.12 + PIN_LINE_STEP, 4)]
    # A whole-video line forces a later ranged one down a slot.
    whole = PinnedText(text="w", corner=top)
    assert ys([whole, a])[1] > ys([whole, a])[0]
    # A chain a(0-4) b(3-10) c(5-10): b and c overlap, so they must not share a slot.
    chain = [
        PinnedText(text="a", corner=top, start_s=0, end_s=4),
        PinnedText(text="b", corner=top, start_s=3, end_s=10),
        PinnedText(text="c", corner=top, start_s=5, end_s=10),
    ]
    first, second, third = ys(chain)
    assert second != third and first != second
    # Bottom corners fill upward starting from the LAST listed line, like the whole-video case.
    bottom_pair = [
        PinnedText(text="x", corner="bottom_left"),
        PinnedText(text="y", corner="bottom_left"),
    ]
    upper, lower = ys(bottom_pair)
    assert lower == 0.86 and upper < lower


def test_two_corners_share_the_row_only_while_both_are_on_screen() -> None:
    def widths(pins: list[PinnedText]) -> list[float]:
        elements = pinned_text_elements(
            resolve_pin_windows(pins, 10.0, None), font_family=None, text_color=None
        )
        return [e["max_width_frac"] for e in elements]

    left_early = PinnedText(text="l", corner="top_left", start_s=0, end_s=4)
    right_late = PinnedText(text="r", corner="top_right", start_s=5, end_s=9)
    right_overlap = PinnedText(text="r", corner="top_right", start_s=2, end_s=9)
    assert widths([left_early, right_late]) == [pytest.approx(0.84)] * 2
    assert widths([left_early, right_overlap]) == [0.42, 0.42]


# -- end to end through the real compiler and the phone recipe ------------------------------


def test_a_seconds_pin_is_on_screen_exactly_then_on_the_phone_recipe() -> None:
    pin = {"text": "Part 1", "corner": "top_left", "start_s": 1.0, "end_s": 3.0}
    plan = _plan({"pinned_texts": [pin]})
    assert plan.snapshot.pinned_texts[0].start_s == 1.0  # not dropped on the way in
    compiled = _compiled(plan)
    [element] = _pinned(compiled)
    assert (element["start_s"], element["end_s"]) == (1.0, 3.0)
    assert element["y_frac"] == 0.12

    recipe = compile_phone_guided_plan(
        GuidedStoryExecutionPlan.model_validate(compiled), _binding_set(3)
    )
    [layer] = [layer for layer in recipe.text_layers if layer.id.startswith("text-")]
    assert (layer.start, layer.end) == (1.0, 3.0)


def test_a_clip_pin_is_on_screen_for_that_clip_only() -> None:
    plan = _plan({"pinned_texts": [{"text": "Day one", "corner": "bottom_right", "clip": 2}]})
    compiled = _compiled(plan)
    [element] = _pinned(compiled)
    start, end = _clip_windows(compiled)[1]
    assert (element["start_s"], element["end_s"]) == (pytest.approx(start), pytest.approx(end))
    assert 0 < element["start_s"] < element["end_s"] < compiled["resolved_duration_s"]


def test_a_pin_on_a_clip_that_does_not_exist_is_left_off_the_plan() -> None:
    """Never a plan that claims a line it cannot draw: the snapshot omits it, so the brief
    receipt reports the text as missing instead of "met"."""
    plan = _plan({"pinned_texts": [{"text": "Ghost", "corner": "top_left", "clip": 9}]})
    assert plan.snapshot.pinned_texts is None
    assert "pinned_texts" not in plan.record()
    assert _pinned(_compiled(plan)) == []

    late = _plan({"pinned_texts": [{"text": "Late", "corner": "top_left", "start_s": 900}]})
    assert late.snapshot.pinned_texts is None


def test_labels_leave_the_bottom_zone_only_while_a_bottom_pin_is_on_screen() -> None:
    labelled = [clip(i, minutes=i, place=f"Place {i}, Town") for i in range(3)]
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r0", kind="text", scope="per_clip", description="the location")
        ],
    )
    plan = plan_unified_montage(
        labelled,
        brief_view(brief),
        strategy={"pinned_texts": [{"text": "Day one", "corner": "bottom_left", "clip": 2}]},
    )
    compiled = _compiled(plan)
    ys = [e["y_frac"] for e in compiled["text_elements"] if e["id"].startswith("clip-label")]
    assert ys == [0.78, 0.70, 0.78]  # only the clip the pin sits on

    whole = plan_unified_montage(
        labelled,
        brief_view(brief),
        strategy={"pinned_texts": [{"text": "Day one", "corner": "bottom_left"}]},
    )
    assert {
        e["y_frac"] for e in _compiled(whole)["text_elements"] if e["id"].startswith("clip-label")
    } == {0.70}


# -- grounding and plan-time checks --------------------------------------------------------


def test_seconds_must_be_numbers_the_creator_wrote() -> None:
    pin = PinnedText(text="Part 1", corner="top_left", start_s=5, end_s=10)
    assert _pin_range_is_grounded(pin, "Put Part 1 top left from 5s to 10s")
    assert _pin_range_is_grounded(pin, "Part 1 at the top, from 5 to 10 seconds")
    assert _pin_range_is_grounded(pin, "Part 1 from five seconds to ten seconds")
    assert not _pin_range_is_grounded(pin, "Put Part 1 top left")
    assert not _pin_range_is_grounded(pin, "Part 1 from 6s to 10s")  # a time the model made up
    assert not _pin_range_is_grounded(pin, "Part 1 for 15 seconds")
    # No range, or a clip scope, has no seconds to check.
    assert _pin_range_is_grounded(PinnedText(text="x", corner="top_left"), "x")
    assert _pin_range_is_grounded(PinnedText(text="x", corner="top_left", clip=1), "x")


def test_a_clip_that_was_not_selected_becomes_a_question() -> None:
    strategy = CreativeStrategy(
        direction="fast_montage",
        edit_format="montage",
        audio_strategy="licensed_music",
        render_program="guided",
        rationale="x",
        selected_media_ids=["a", "b"],
        pinned_texts=[{"text": "Day three", "corner": "top_left", "clip": 3}],
    )
    refusal = _pin_range_refusal(strategy)
    assert refusal is not None and refusal.code == "pinned_text_clip_missing"
    assert "2 clips" in refusal.question and "Day three" in refusal.question

    ok = strategy.model_copy(
        update={"pinned_texts": [PinnedText(text="Day two", corner="top_left", clip=2)]}
    )
    assert _pin_range_refusal(ok) is None
    # "All clips" (empty selection) is not checkable here.
    assert _pin_range_refusal(strategy.model_copy(update={"selected_media_ids": []})) is None
