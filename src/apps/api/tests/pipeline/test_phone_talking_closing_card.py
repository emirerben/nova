"""KRI-1346: the closing colour card on a phone Talking edit.

Each test names the failure mode it guards (C1..C13 of the feature's risk list).
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.recipes import Canvas
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes, SubtitledEndingClip
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.pipeline.phone_talking_closing_card import (
    CLOSING_CARD_DEFAULT_S,
    CLOSING_CARD_FILL_ID,
    CLOSING_CARD_TEXT_ID,
    ClosingCardSpec,
    align_rows_to_recipe,
    asks_closing_card,
    closing_card_from_recipe,
    closing_card_from_requirements,
    closing_card_receipt,
    closing_card_text_row,
    contrast_ratio,
    retime_card_rows,
)
from app.services.phone_rollout import validate_phone_pilot_recipe
from tests.pipeline.test_phone_subtitled_plan import (
    _CUES,
    VIDEO_ID,
    VIDEO_PATH,
    _binding,
    _pool_video_visual,
)

_CANVAS = Canvas(width=1080, height=1920)


class _Req:
    def __init__(self, kind="text", facts=None, literal=None, id="r5"):
        self.kind = kind
        self.facts = facts
        self.literal = literal
        self.id = id


def _ask(**facts):
    return _Req(facts={"closing": "follow for more", **facts})


def _spec(**changes) -> ClosingCardSpec:
    base = {"text": "follow for more", "background_color": "#FF0000", "text_color": "#FFFF00"}
    return ClosingCardSpec(**(base | changes))


def _card_row(**params) -> dict:
    row = closing_card_text_row(_spec(), start_s=10.0, end_s=13.0, canvas=_CANVAS)
    assert row is not None
    return row


# --- spec parsing (C1, C4, C6) -------------------------------------------------------------


def test_c1_a_colour_and_closing_words_is_a_card_but_either_alone_is_not():
    assert asks_closing_card(_ask(background_color="red"))
    assert not asks_closing_card(_ask())  # words only: KRI-514's closing text
    assert not asks_closing_card(_Req(facts={"background_color": "red"}))  # speaker background
    assert not asks_closing_card(_Req(kind="style", facts={**_ask(background_color="red").facts}))


@pytest.mark.parametrize(
    ("name", "hex_value"),
    [("red", "#FF0000"), ("#00ff00", "#00FF00"), ("kırmızı", "#FF0000")],
    ids=["en-name", "hex", "tr-name"],
)
def test_c4_named_and_hex_colours_resolve(name, hex_value):
    spec = closing_card_from_requirements([_ask(background_color=name, text_color="black")])

    assert spec is not None
    assert spec.background_color.upper() == hex_value
    assert spec.text_color == "#000000"
    assert spec.text_color_asked and not spec.text_color_swapped


def test_c4_an_unnamed_colour_makes_no_card():
    assert closing_card_from_requirements([_ask(background_color="a nice calm shade")]) is None


def test_c4_unreadable_text_colour_is_swapped_and_flagged():
    spec = closing_card_from_requirements([_ask(background_color="#FFFF00", text_color="#FFFF00")])

    assert spec is not None
    assert spec.text_color_swapped
    assert contrast_ratio(spec.text_color, spec.background_color) >= 2.0


def test_c4_unasked_text_colour_is_picked_for_contrast_not_swapped():
    spec = closing_card_from_requirements([_ask(background_color="#FFFF00")])

    assert spec is not None
    assert not spec.text_color_asked and not spec.text_color_swapped
    assert spec.text_color == "#000000"


def test_c4_contrast_ratio_extremes():
    assert contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert contrast_ratio("#123456", "#123456") == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("asked", "expected"),
    [(None, CLOSING_CARD_DEFAULT_S), (0.2, 1.0), (60, 10.0), (4, 4.0), ("long", 3.0), (True, 3.0)],
)
def test_c6_duration_defaults_to_three_and_clamps(asked, expected):
    facts = {"background_color": "red"}
    if asked is not None:
        facts["duration_s"] = asked
    spec = closing_card_from_requirements([_ask(**facts)])

    assert spec is not None and spec.duration_s == expected


def test_blank_closing_words_are_not_a_card_even_with_a_literal():
    req = _Req(facts={"closing": " ", "background_color": "red"}, literal="takip et")

    assert closing_card_from_requirements([req], closing_title="x") is None


def test_spec_round_trips_through_its_dict():
    spec = _spec(duration_s=4.5, requirement_id="r5", text_color_asked=True)
    assert ClosingCardSpec.from_dict(spec.as_dict()) == spec
    assert ClosingCardSpec.from_dict({"text": "x"}) is None


# --- the text row (C2) ---------------------------------------------------------------------


def test_c2_text_row_is_centred_on_the_card_window_and_tagged():
    row = _card_row()

    assert row["id"] == CLOSING_CARD_TEXT_ID
    assert (row["start_s"], row["end_s"]) == (10.0, 13.0)
    assert (row["x_frac"], row["y_frac"]) == (0.5, 0.5)
    assert row["color"] == "#FFFF00"
    assert row["source_params"]["source"] == "closing_card"


def test_c2_retime_moves_only_card_rows():
    other = {"id": "t", "start_s": 0.0, "end_s": 2.0, "source_params": {"source": "opening_title"}}
    out = retime_card_rows([other, _card_row()], start_s=20.0, end_s=23.0)

    assert out[0] == other
    assert (out[1]["start_s"], out[1]["end_s"]) == (20.0, 23.0)


# --- the compiler (C2, C3, C7, C9/C12) -----------------------------------------------------


def _compile(**kwargs):
    kwargs.setdefault("caption_cues", _CUES)
    return compile_phone_subtitled_plan((_binding(duration_s=10.0),), **kwargs)


def _main(recipe):
    return next(t for t in recipe.tracks if t.id == "subtitled").clips


def test_c3_card_only_holds_the_last_clip_and_never_cuts_speech():
    plain = _compile()
    carded = _compile(closing_card={"duration_s": 3.0, "background_color": "#FF0000"})

    before, after = _main(plain)[-1], _main(carded)[-1]
    assert after.source_duration == before.source_duration
    assert after.source_start == before.source_start
    assert (after.hold_duration or 0) == pytest.approx(3.0)
    assert carded.duration == pytest.approx(plain.duration + 3.0)
    assert {"freezeFrame", "visualBlocks"} <= carded.required_capabilities


def test_c2_fill_starts_after_the_last_word_and_text_sits_on_it():
    row = _card_row()
    recipe = _compile(
        closing_card={"duration_s": 3.0, "background_color": "#FF0000"}, text_elements=[row]
    )

    (fill,) = recipe.visual_fills
    assert fill.id == CLOSING_CARD_FILL_ID
    assert fill.start == pytest.approx(10.0) and fill.end == pytest.approx(13.0)
    assert fill.color.red == pytest.approx(1.0) and fill.color.green == 0
    text_layer = next(layer for layer in recipe.text_layers if layer.id == "text-0")
    assert text_layer.start == pytest.approx(fill.start)
    assert text_layer.end == pytest.approx(fill.end)


def test_c7_card_follows_an_ending_clip():
    ending = SubtitledEndingClip(media_id=VIDEO_ID, gcs_path=VIDEO_PATH, generation="88")
    kwargs = {
        "visuals": (_pool_video_visual(duration_s=6.0),),
        "lanes": PhoneSubtitledLanes(ending_clip=ending),
    }
    plain = _compile(**kwargs)
    carded = _compile(closing_card={"duration_s": 2.0, "background_color": "#0000FF"}, **kwargs)

    (fill,) = carded.visual_fills
    assert fill.start == pytest.approx(plain.duration)
    assert carded.duration == pytest.approx(plain.duration + 2.0)
    # The hold lands on the ending clip, never on the talk.
    speaker, ending_clip = _main(carded)
    assert (speaker.hold_duration or 0) == 0
    assert ending_clip.hold_duration == pytest.approx(2.0)


def test_c8_whole_video_rows_stretch_to_the_new_end_and_ranged_rows_stay():
    from app.pipeline.phone_talking_corner_text import corner_text_rows
    from app.schemas.edit_proposal import PinnedText

    pins = [
        PinnedText(text="Yorumla", corner="top_left"),
        PinnedText(text="Part 8", corner="top_right", start_s=0.0, end_s=4.0),
    ]
    rows = corner_text_rows(pins, talk_end_s=10.0, canvas=_CANVAS)
    assert [r["source_params"]["whole_video"] for r in rows] == [True, False]

    carded = _compile(
        closing_card={"duration_s": 3.0, "background_color": "#FF0000"}, text_elements=rows
    )
    ends = [layer.end for layer in carded.text_layers if layer.id.startswith("text-")]
    assert ends == [pytest.approx(13.0), pytest.approx(4.0)]
    # Without a card the whole-video row still runs to the (unchanged) end.
    plain = [
        layer.end
        for layer in _compile(text_elements=rows).text_layers
        if layer.id.startswith("text-")
    ]
    assert plain == [pytest.approx(10.0), pytest.approx(4.0)]


def test_c12_no_card_is_byte_identical():
    rows = [{**_card_row(), "source_params": {"source": "opening_title"}, "end_s": 2.0}]
    kwargs = {"text_elements": [{**rows[0], "start_s": 0.0}]}
    assert (
        _compile(**kwargs).model_dump_json()
        == _compile(closing_card=None, **kwargs).model_dump_json()
    )
    assert _compile().visual_fills == []


# --- the gates (C9) ------------------------------------------------------------------------


def _carded():
    return _compile(
        closing_card={"duration_s": 3.0, "background_color": "#FF0000"}, text_elements=[_card_row()]
    )


def test_c9_flag_off_still_rejects_the_fill(monkeypatch):
    recipe = _carded()
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    monkeypatch.setattr(settings, "phone_talking_closing_card_enabled", False)

    with pytest.raises(ValueError, match="Visual blocks"):
        validate_phone_pilot_recipe(recipe)


def test_c9_flag_on_but_closing_card_token_unverified_still_rejects(monkeypatch):
    recipe = _carded()
    verified = [f for f in recipe.required_capabilities if f != "visualBlocks"]
    monkeypatch.setattr(settings, "phone_render_verified_features", verified + ["freezeFrame"])
    monkeypatch.setattr(settings, "phone_talking_closing_card_enabled", True)
    monkeypatch.setattr(settings, "phone_subtitled_title_enabled", True)

    with pytest.raises(ValueError):
        validate_phone_pilot_recipe(recipe)


def test_c9_fully_enabled_accepts_the_card(monkeypatch):
    recipe = _carded()
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*recipe.required_capabilities, "closingCard", "positionedText", "animatedText"],
    )
    monkeypatch.setattr(settings, "phone_talking_closing_card_enabled", True)
    monkeypatch.setattr(settings, "phone_subtitled_title_enabled", True)

    validate_phone_pilot_recipe(recipe)


def test_c9_a_foreign_fill_is_never_accepted(monkeypatch):
    recipe = _carded()
    foreign = recipe.visual_fills[0].model_copy(update={"id": "other-fill"})
    recipe = recipe.model_copy(update={"visual_fills": [foreign]})
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*recipe.required_capabilities, "closingCard", "positionedText", "animatedText"],
    )
    monkeypatch.setattr(settings, "phone_talking_closing_card_enabled", True)
    monkeypatch.setattr(settings, "phone_subtitled_title_enabled", True)

    with pytest.raises(ValueError, match="Visual blocks"):
        validate_phone_pilot_recipe(recipe)


# --- round trip and receipt (C11, C13) -----------------------------------------------------


def test_c11_colour_and_duration_survive_the_recipe_round_trip():
    recipe = _compile(closing_card={"duration_s": 4.5, "background_color": "#12AB9F"})

    assert closing_card_from_recipe(recipe) == {"duration_s": 4.5, "background_color": "#12AB9F"}
    assert closing_card_from_recipe(_compile()) is None


def test_c11_recompiling_from_the_round_trip_is_identical():
    first = _compile(closing_card={"duration_s": 4.5, "background_color": "#12AB9F"})
    again = _compile(closing_card=closing_card_from_recipe(first))

    assert again.model_dump_json() == first.model_dump_json()


def test_c13_receipt_is_applied_only_with_fill_and_card_row():
    spec = _spec()
    recipe = _compile(closing_card={"duration_s": 3.0, "background_color": "#FF0000"})
    plain = _compile()
    row = _card_row()

    applied = closing_card_receipt(spec, recipe, [row])
    assert applied["status"] == "applied"
    assert (applied["start_s"], applied["end_s"], applied["duration_s"]) == (10.0, 13.0, 3.0)
    assert closing_card_receipt(spec, recipe, [])["reason"] == "no_text"
    dropped = closing_card_receipt(spec, plain, [row])
    assert (dropped["status"], dropped["reason"]) == ("dropped", "not_compiled")


def test_align_rows_puts_card_on_its_window_and_whole_video_at_the_end():
    recipe = _compile(closing_card={"duration_s": 3.0, "background_color": "#FF0000"})
    corner = {"id": "c", "start_s": 0.0, "end_s": 10.0, "source_params": {"whole_video": True}}
    ranged = {"id": "r", "start_s": 1.0, "end_s": 2.0, "source_params": {}}
    stale_card = {**_card_row(), "start_s": 0.0, "end_s": 1.0}

    out = {r["id"]: r for r in align_rows_to_recipe([corner, ranged, stale_card], recipe)}

    assert out["c"]["end_s"] == pytest.approx(13.0)
    assert (out["r"]["start_s"], out["r"]["end_s"]) == (1.0, 2.0)
    assert (out[CLOSING_CARD_TEXT_ID]["start_s"], out[CLOSING_CARD_TEXT_ID]["end_s"]) == (
        10.0,
        13.0,
    )


# --- review of #2256: never shorten the creator's words ---------------------------------------


def test_review_long_words_are_kept_whole_in_the_spec():
    words = "follow for more " * 12  # 192 characters: past the old 160 cut
    spec = closing_card_from_requirements(
        [
            BriefRequirement(
                id="r5",
                kind="text",
                scope="global",
                description="end card",
                facts={"closing": words, "background_color": "red"},
            )
        ]
    )
    assert spec is not None and spec.text == " ".join(words.split())


def test_review_words_over_a_rows_limit_are_refused_not_cut():
    from app.pipeline.phone_subtitled_plan import _STORY_CANVAS
    from app.pipeline.phone_talking_closing_card import (
        TOO_LONG,
        ClosingCardSpec,
        closing_card_text_row_or_reason,
    )

    spec = ClosingCardSpec(text="x" * 501, background_color="#FF0000", text_color="#FFFF00")
    row, reason = closing_card_text_row_or_reason(
        spec, start_s=10.0, end_s=13.0, canvas=_STORY_CANVAS
    )
    assert row is None and reason == TOO_LONG


def test_review_a_drawn_card_carries_every_word():
    from app.pipeline.phone_subtitled_plan import _STORY_CANVAS
    from app.pipeline.phone_talking_closing_card import (
        ClosingCardSpec,
        closing_card_text_row_or_reason,
    )

    words = " ".join(("follow for more brand and marketing content " * 4).split())
    spec = ClosingCardSpec(text=words, background_color="#FF0000", text_color="#FFFF00")
    row, reason = closing_card_text_row_or_reason(
        spec, start_s=10.0, end_s=13.0, canvas=_STORY_CANVAS
    )
    assert reason is None and row is not None and row["text"] == words
