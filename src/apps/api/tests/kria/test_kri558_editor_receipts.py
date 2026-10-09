"""KRI-558: an editor turn names what its ops changed instead of "can't check".

Real compiler, real receipts, real reply: only the model call is absent. Failure modes pinned
here (written before the code):
* "some text field changed" proves a style ask (KRI-524) — a cue-less ask must never be met by
  an unrelated change;
* two requirements of one turn both claim the same change;
* the wrong text / wrong direction / wrong field is reported as done;
* a no-op (value already set) is reported as a change;
* the copilot's declined request is lost, so a half-done turn reads as fully done;
* a creator's unsaved client edit is attributed to this turn;
* the Turkish reply drifts from the English structure.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import build_receipts, plan_facts_from_editor_payload
from app.kria.editor_receipts import (
    bind_editor_receipts,
    reply_from_diff,
    reply_from_editor_receipts,
)
from app.kria.reply_language import reply_language_for
from app.services.kria_editor_ops import compile_editor_ops
from app.services.kria_editor_ops_diff import describe_diff

ROWS = [
    {
        "id": "a",
        "text": "Must visit spots in Lisbon",
        "start_s": 1.0,
        "end_s": 9.0,
        "role": "generative_intro",
        "font_family": "Playfair Display",
        "size_px": 52,
        "color": "#FFFFFF",
        "effect": "static",
        "position": "custom",
        "alignment": "left",
        "x_frac": 0.0856,
        "y_frac": 0.84,
    },
    {
        "id": "b",
        "text": "Part 1",
        "start_s": 1.0,
        "end_s": 9.0,
        "role": "generative_intro",
        "font_family": "Playfair Display",
        "size_px": 52,
        "color": "#FFFFFF",
        "effect": "static",
        "position": "custom",
        "alignment": "left",
        "x_frac": 0.0856,
        "y_frac": 0.91,
    },
]


def _req(kind: str, description: str, rid: str = "r1", **extra: Any) -> BriefRequirement:
    scope = extra.pop("scope", "global")
    literal = extra.pop("literal", None)
    return BriefRequirement(
        id=rid, kind=kind, scope=scope, description=description, literal=literal, facts=extra
    )


def _patch(ids: list[str], patch: dict[str, Any]) -> dict[str, Any]:
    return {
        "op": "patch_text",
        "selector": {"ids": ids},
        "target_ids": ids,
        "expected_count": len(ids),
        "patch": patch,
    }


def _turn(reqs, ops, *, unmet=(), rows=None, variant_extra=None):
    job = SimpleNamespace(all_candidates={"clip_paths": []}, assembly_plan={}, id="job")
    variant = {"variant_id": "v", "text_elements": rows or ROWS, **(variant_extra or {})}
    compiled = compile_editor_ops(job, variant, ops)
    payload = compiled.payload.model_dump(mode="json", exclude_none=True)
    facts = plan_facts_from_editor_payload(payload, compiled.text_diff or None, compiled.changes)
    facts = replace(facts, anaphora_rows=compiled.diff.changed_text_ids())
    receipts = build_receipts(reqs, facts, include_unchecked=True)
    receipts, unbound = bind_editor_receipts(
        reqs, receipts, compiled.diff, facts.text_styles or (), unmet=list(unmet)
    )
    reply = reply_from_editor_receipts(
        reqs,
        receipts,
        unbound=describe_diff(unbound, live_text_count=compiled.diff.live_text_count, limit=2),
    )
    return {r.requirement_id: r for r in receipts}, reply, compiled


def _assert_checked(receipt) -> None:
    assert receipt.verification == "checked"


def test_fade_on_all_texts_is_done_and_names_the_change() -> None:
    reqs = [_req("style", "Add fade in animation to all of them")]
    receipts, reply, _ = _turn(
        reqs, [_patch(["a", "b"], {"animation_phases": {"entrance": "fade"}})]
    )
    assert receipts["r1"].status == "met"
    _assert_checked(receipts["r1"])
    assert reply == "Done: Added a fade-in animation to both texts."


def test_alte_haas_font_without_a_structured_intent_is_judged_from_the_words() -> None:
    reqs = [_req("style", "All texts should use alte haas font")]
    receipts, reply, _ = _turn(reqs, [_patch(["a", "b"], {"font_family": "Alte Haas Grotesk"})])
    assert receipts["r1"].status == "met"
    assert reply == "Done: Font → Alte Haas Grotesk on both texts."


def test_a_font_ask_the_ops_missed_is_partly_not_done() -> None:
    reqs = [_req("style", "All texts should use alte haas font")]
    receipts, reply, _ = _turn(reqs, [_patch(["a"], {"font_family": "Alte Haas Grotesk"})])
    assert receipts["r1"].status == "partial"
    assert "1 of 2 texts don't have the requested font" in (receipts["r1"].reason or "")
    assert reply.startswith("Partly: All texts should use alte haas font")


def test_smaller_on_the_named_text_is_done() -> None:
    reqs = [_req("style", "Make the lisbon text smaller")]
    receipts, reply, _ = _turn(reqs, [_patch(["a"], {"size_scale": 0.8})])
    assert receipts["r1"].status == "met"
    assert reply == "Done: Made 'Must visit spots in Lisbon' smaller (52→41.6px)."


def test_the_wrong_text_is_not_reported_as_the_one_asked_for() -> None:
    reqs = [_req("style", "Make the lisbon text smaller")]
    receipts, reply, _ = _turn(reqs, [_patch(["b"], {"size_scale": 0.8})])
    assert receipts["r1"].status == "not_possible"
    assert "not the one you named" in (receipts["r1"].reason or "")
    assert reply.startswith("Couldn't: Make the lisbon text smaller")


def test_the_wrong_direction_is_not_done() -> None:
    reqs = [_req("style", "Make the lisbon text smaller")]
    receipts, _reply, _ = _turn(reqs, [_patch(["a"], {"size_scale": 1.3})])
    assert receipts["r1"].status == "partial"
    assert "isn't what you asked for" in (receipts["r1"].reason or "")


def test_the_wrong_field_is_not_done() -> None:
    reqs = [_req("style", "Make the lisbon text smaller")]
    receipts, _reply, _ = _turn(reqs, [_patch(["a"], {"color": "#FF0000"})])
    assert receipts["r1"].status == "not_possible"


def test_an_ask_with_no_recognisable_dimension_is_never_met_by_an_unrelated_change() -> None:
    """KRI-524: "some text field changed" must not prove "add a glow" next to a resize."""
    reqs = [
        _req("style", "Make the lisbon text smaller", rid="r1"),
        _req("style", "Add a glow", rid="r2"),
    ]
    receipts, reply, _ = _turn(reqs, [_patch(["a"], {"size_scale": 0.8})])
    assert receipts["r1"].status == "met"
    # The only change was claimed by the resize, so "add a glow" has nothing to show.
    assert receipts["r2"].status == "not_possible"
    assert "Couldn't: Add a glow" in reply


def test_an_ask_naming_no_dimension_is_never_green_it_lists_what_changed() -> None:
    """KRI-524: "feel more cinematic" has no checkable dimension, so even a lone change is not
    claimed as the ask; the creator sees what changed on a neutral chip."""
    reqs = [_req("style", "Make it feel more cinematic")]
    receipts, reply, _ = _turn(reqs, [_patch(["a", "b"], {"letter_spacing": 2})])
    assert (receipts["r1"].status, receipts["r1"].verification) == ("partial", "unchecked")
    assert receipts["r1"].reason == "I changed: Changed the letter spacing of both texts"
    assert reply.startswith("Have a look: Make it feel more cinematic (I changed:")


def test_two_asks_naming_no_dimension_both_see_the_changes() -> None:
    reqs = [
        _req("style", "Add a glow", rid="r1"),
        _req("style", "Make it feel cinematic", rid="r2"),
    ]
    receipts, _reply, _ = _turn(reqs, [_patch(["a"], {"letter_spacing": 2})])
    for rid in ("r1", "r2"):
        assert receipts[rid].verification == "unchecked"
        assert "letter spacing" in (receipts[rid].reason or "")


def test_the_copilots_declined_request_is_reported_even_when_the_rest_applied() -> None:
    reqs = [
        _req("style", "Make the lisbon text smaller", rid="r1"),
        _req("style", "Add a glow", rid="r2"),
    ]
    receipts, reply, _ = _turn(
        reqs,
        [_patch(["a"], {"size_scale": 0.8})],
        unmet=[{"request": "add a glow", "reason": "Glow isn't available yet."}],
    )
    assert receipts["r1"].status == "met"
    assert receipts["r2"].status == "not_possible"
    assert "Glow isn't available yet" in reply


def test_two_requirements_each_get_their_own_change_and_nothing_is_double_counted() -> None:
    reqs = [
        _req("style", "Use the alte haas font", rid="r1"),
        _req("style", "Make the lisbon text smaller", rid="r2"),
    ]
    receipts, reply, _ = _turn(
        reqs,
        [
            _patch(["a", "b"], {"font_family": "Alte Haas Grotesk"}),
            _patch(["a"], {"size_scale": 0.8}),
        ],
    )
    assert receipts["r1"].status == receipts["r2"].status == "met"
    assert "Font → Alte Haas Grotesk on both texts" in reply
    assert "smaller (52→41.6px)" in reply
    assert "Also changed" not in reply


def test_a_change_no_requirement_claimed_is_listed_as_also_changed() -> None:
    reqs = [_req("style", "Make the lisbon text smaller")]
    _receipts, reply, _ = _turn(
        reqs, [_patch(["a"], {"size_scale": 0.8}), _patch(["b"], {"color": "#FFD60A"})]
    )
    assert "Also changed: Colour → #FFD60A on 'Part 1'" in reply


def test_a_no_op_is_nothing_changed_not_a_change() -> None:
    reqs = [_req("style", "Make the lisbon text smaller")]
    receipts, _reply, compiled = _turn(reqs, [_patch(["a"], {"size_px": 52})])
    assert compiled.diff.empty()
    assert receipts["r1"].status == "not_possible"
    assert "Nothing changed" in (receipts["r1"].reason or "")


def test_a_creators_unsaved_edit_is_not_this_turns_change() -> None:
    manual = [dict(ROWS[0], color="#FFD60A"), dict(ROWS[1])]
    reqs = [_req("style", "Add fade in animation to all of them")]
    receipts, reply, compiled = _turn(
        reqs, [_patch(["a", "b"], {"animation_phases": {"entrance": "fade"}})], rows=manual
    )
    assert receipts["r1"].status == "met"
    assert "olour" not in reply
    assert {e.field for e in compiled.diff.entries} == {"entrance"}


def test_a_title_rewrite_does_not_stand_in_for_a_label_on_each_clip() -> None:
    reqs = [
        _req("text", "Change the hook", rid="r1", scope="title", literal="Fresh matcha"),
        _req("text", "a label on each clip", rid="r2", scope="per_clip"),
    ]
    ops = [{"op": "edit_text", "bar_index": 0, "text": "Fresh matcha"}]
    receipts, reply, _ = _turn(reqs, ops)
    assert receipts["r1"].status == "met"
    assert receipts["r2"].status == "not_possible"
    assert "Couldn't: a label on each clip" in reply


def test_the_reply_is_turkish_when_the_turn_is_turkish() -> None:
    reqs = [_req("style", "tüm yazılara solma animasyonu ekle")]
    with reply_language_for("tr"):
        receipts, reply, _ = _turn(
            reqs, [_patch(["a", "b"], {"animation_phases": {"entrance": "fade"}})]
        )
    assert receipts["r1"].status == "met"
    assert reply.startswith("Yapıldı:") and "iki yazı" in reply
    assert "Done" not in reply


def test_a_turn_with_no_requirement_still_says_what_changed() -> None:
    _receipts, _reply, compiled = _turn([], [_patch(["a", "b"], {"color": "#FFD60A"})])
    phrases = describe_diff(compiled.diff.entries, live_text_count=2)
    assert reply_from_diff(phrases) == "Done: Colour → #FFD60A on both texts."


# ----------------------------------------------------------------------------------------
# Adversarial review of the first cut: a matched change must also DELIVER the asked outcome.
# ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ask", "patch", "ids"),
    [
        ("Change the font to Inter", {"font_family": "Syne"}, ["a", "b"]),
        ("yazı tipini Inter yap", {"font_family": "Syne"}, ["a", "b"]),
        ("Make the lisbon text red", {"color": "#0000FF"}, ["a"]),
        ("Make the texts white", {"color": "#0000FF"}, ["a", "b"]),
        ("Use white text", {"color": "#FF0000"}, ["a", "b"]),
        ("Make the texts lowercase", {"text_case": "upper"}, ["a", "b"]),
    ],
)
def test_a_value_the_ops_got_wrong_is_never_met(ask, patch, ids) -> None:
    receipts, reply, _ = _turn([_req("style", ask)], [_patch(ids, patch)])
    assert receipts["r1"].status != "met", reply
    assert "isn't what you asked for" in (receipts["r1"].reason or "") or (
        receipts["r1"].status == "partial"
    )


def test_the_value_the_ops_got_right_is_still_met_without_a_target_word() -> None:
    receipts, reply, _ = _turn(
        [_req("style", "Change the font to Inter")], [_patch(["a", "b"], {"font_family": "Inter"})]
    )
    assert receipts["r1"].status == "met" and reply == "Done: Font → Inter on both texts."


SHADOW_ROWS = [dict(r, shadow_enabled=True, animation_phases=None) for r in ROWS]


@pytest.mark.parametrize(
    ("ask", "ops_patch", "rows"),
    [
        ("Remove the animation from the texts", {"animation_phases": {"entrance": "fade"}}, ROWS),
        ("Add a shadow", {"shadow_enabled": False}, SHADOW_ROWS),
        (
            "Remove the shadow",
            {"shadow_enabled": True},
            [dict(r, shadow_enabled=False) for r in ROWS],
        ),
        ("Move the text to the left", {"position": "custom", "x_frac": 0.9}, ROWS),
        (
            "Move the text to the top",
            {"position": "custom", "x_frac": 0.0856, "y_frac": 0.95},
            ROWS,
        ),
    ],
)
def test_the_opposite_of_the_ask_is_never_met(ask, ops_patch, rows) -> None:
    receipts, reply, _ = _turn([_req("style", ask)], [_patch(["a"], ops_patch)], rows=rows)
    assert receipts["r1"].status != "met", reply


def test_louder_and_quieter_are_judged_on_the_level_not_just_that_it_changed() -> None:
    mix = {"mix": {"music_level": 0.5}}
    for ask, level, met in (
        ("Make the music louder", 0.2, False),
        ("Make the music quieter", 0.9, False),
        ("Make the music louder", 0.9, True),
    ):
        receipts, _reply, _ = _turn(
            [_req("audio", ask)], [{"op": "set_mix", "music_level": level}], variant_extra=mix
        )
        assert (receipts["r1"].status == "met") is met, (ask, level)


def test_remove_the_music_is_not_met_by_a_louder_mix_and_add_music_not_by_removing_it() -> None:
    mix = {"mix": {"music_level": 0.5}}
    receipts, _r, _ = _turn(
        [_req("audio", "Remove the music")],
        [{"op": "set_mix", "music_level": 0.9}],
        variant_extra=mix,
    )
    assert receipts["r1"].status != "met"
    receipts, _r, _ = _turn([_req("audio", "Remove the music")], [{"op": "remove_music"}])
    assert receipts["r1"].status == "met"
    receipts, _r, _ = _turn([_req("audio", "Add some music")], [{"op": "remove_music"}])
    assert receipts["r1"].status != "met"


SLOTS = [{"slot_id": f"s{i}", "clip_index": i, "in_s": 0.0, "duration_s": 3.0} for i in range(4)]


def test_shorter_is_not_met_by_a_longer_clip_and_the_named_clip_must_be_the_one_changed() -> None:
    ops = [{"op": "set_clip_duration", "slot_index": 0, "duration_s": 5.0}]
    receipts, _r, _ = _turn(
        [_req("timing", "Make clip 1 shorter")],
        ops,
        variant_extra={"user_timeline": {"slots": SLOTS}},
    )
    assert receipts["r1"].status != "met"
    ops = [{"op": "set_clip_duration", "slot_index": 2, "duration_s": 1.0}]
    receipts, _r, _ = _turn(
        [_req("timing", "Make clip 1 shorter")],
        ops,
        variant_extra={"user_timeline": {"slots": SLOTS}},
    )
    assert receipts["r1"].status != "met"  # clip 3 was shortened, not clip 1
    receipts, _r, _ = _turn(
        [_req("timing", "Make clip 3 shorter")],
        ops,
        variant_extra={"user_timeline": {"slots": SLOTS}},
    )
    assert receipts["r1"].status == "met"


def test_remove_the_second_clip_is_not_met_by_removing_the_third() -> None:
    extra = {"user_timeline": {"slots": SLOTS}}
    ops = [{"op": "remove_clip", "slot_index": 2}]
    receipts, _r, _ = _turn([_req("select", "Remove the second clip")], ops, variant_extra=extra)
    assert receipts["r1"].status != "met"
    ops = [{"op": "remove_clip", "slot_index": 1}]
    receipts, _r, _ = _turn([_req("select", "Remove the second clip")], ops, variant_extra=extra)
    assert receipts["r1"].status == "met"
    ops = [{"op": "remove_clip", "slot_index": 3}]
    receipts, _r, _ = _turn([_req("select", "Remove the last clip")], ops, variant_extra=extra)
    assert receipts["r1"].status == "met"


def test_a_literal_or_clip_scoped_text_ask_needs_that_text_on_that_clip() -> None:
    add = {"op": "add_text", "text": "Day 2", "start_s": 1.0, "end_s": 3.0}
    req = _req("text", "Add Day 1 on the first clip", scope="clip:c0", literal="Day 1")
    receipts, _r, _ = _turn([req], [add])
    assert receipts["r1"].status != "met"
    remove = {
        "op": "remove_texts",
        "selector": {"ids": ["b"]},
        "target_ids": ["b"],
        "expected_count": 1,
    }
    receipts, _r, _ = _turn([_req("text", "Add the text hello", literal="hello")], [remove])
    assert receipts["r1"].status != "met"


@pytest.mark.parametrize(
    ("ask", "seconds"),
    [
        ("make every clip 1.5 seconds", 1.5),
        ("each clip 1,5 seconds", 1.5),
        ("make all clips 1 second long except the first and the last", 1.0),
        ("every clip under 3 seconds", None),
        ("keep all the clips within 30 seconds total", None),
        ("each clip no longer than 2 seconds", None),
    ],
)
def test_per_clip_length_keeps_its_decimals_and_ignores_limits(ask, seconds) -> None:
    from app.kria.style_asks import clip_length_ask

    parsed = clip_length_ask(_req("timing", ask))
    assert (parsed[0] if parsed else None) == seconds


@pytest.mark.parametrize(
    ("ask", "patch"),
    [
        ("Make the font bigger", {"size_scale": 1.3}),
        ("Make the text bigger when the red car appears", {"size_scale": 1.3}),
        ("Add a slow exit animation to all texts", {"animation_phases": {"exit": "fade"}}),
    ],
)
def test_cue_words_that_only_describe_something_do_not_invent_a_second_ask(ask, patch) -> None:
    receipts, reply, _ = _turn([_req("style", ask)], [_patch(["a", "b"], patch)])
    assert receipts["r1"].status == "met", reply


def test_a_fade_transition_is_a_transition_ask_not_a_text_animation_ask() -> None:
    ops = [{"op": "set_transition", "boundary_index": 0, "transition": "crossfade"}]
    receipts, reply, _ = _turn(
        [_req("style", "Add a fade transition between clips")],
        ops,
        variant_extra={"user_timeline": {"slots": SLOTS}},
    )
    assert receipts["r1"].status == "met", reply


def test_small_and_big_without_a_comparative_still_name_a_direction() -> None:
    receipts, _r, _ = _turn(
        [_req("style", "Make the lisbon text small")], [_patch(["a"], {"size_scale": 1.3})]
    )
    assert receipts["r1"].status != "met"
    receipts, _r, _ = _turn(
        [_req("style", "Lizbon yazısını daha küçük yap")], [_patch(["a"], {"size_scale": 1.3})]
    )
    assert receipts["r1"].status != "met"


def test_caption_styling_is_found_in_the_caption_meta_lane() -> None:
    ops = [{"op": "set_caption_meta", "patch": {"size_px": 80}}]
    receipts, reply, _ = _turn([_req("style", "Make the captions bigger")], ops)
    assert receipts["r1"].status == "met", reply


def test_moving_a_clip_is_a_reorder_not_a_text_position_ask() -> None:
    ops = [{"op": "reorder_clip", "from_index": 3, "to_index": 0}]
    receipts, reply, _ = _turn(
        [_req("order", "Move the last clip first")],
        ops,
        variant_extra={"user_timeline": {"slots": SLOTS}},
    )
    assert receipts["r1"].status == "met", reply


@pytest.mark.parametrize(
    "ask", ["Make both texts smaller", "Make all texts smaller", "Make everything smaller"]
)
def test_a_quantifier_needs_every_text_to_change(ask) -> None:
    receipts, _r, _ = _turn([_req("style", ask)], [_patch(["a"], {"size_scale": 0.8})])
    assert receipts["r1"].status == "partial"
    assert "Only 1 of 2 texts changed" in (receipts["r1"].reason or "")
    receipts, _r, _ = _turn([_req("style", ask)], [_patch(["a", "b"], {"size_scale": 0.8})])
    assert receipts["r1"].status == "met"


def test_the_plural_texts_means_all_of_them_for_a_value_ask() -> None:
    receipts, _r, _ = _turn(
        [_req("style", "Use the Inter font for the texts")],
        [_patch(["a"], {"font_family": "Inter"})],
    )
    assert receipts["r1"].status == "partial"  # text b is still Playfair Display


def test_a_declined_glow_does_not_taint_a_finished_letter_spacing_ask() -> None:
    reqs = [
        _req("style", "Add letter spacing to all texts", rid="r1"),
        _req("style", "Add a glow", rid="r2"),
    ]
    receipts, _r, _ = _turn(
        reqs,
        [_patch(["a", "b"], {"letter_spacing": 2})],
        unmet=[{"request": "add a glow", "reason": "Glow isn't available yet."}],
    )
    assert receipts["r1"].status == "met"
    assert receipts["r2"].status == "not_possible"


def test_a_modal_verb_in_the_ask_is_not_read_as_a_text_name() -> None:
    receipts, _r, _ = _turn(
        [_req("style", "You must make all texts bigger")], [_patch(["a", "b"], {"size_scale": 1.3})]
    )
    assert receipts["r1"].status == "met"


def test_a_lane_the_diff_could_not_read_makes_the_whole_diff_unavailable(monkeypatch) -> None:
    from app.services import kria_editor_ops_diff as diff_module

    def boom(*_a, **_k):
        raise RuntimeError("bad slots")

    monkeypatch.setattr(diff_module, "_slot_entries", boom)
    _receipts, _reply, compiled = _turn(
        [_req("style", "Make it bigger")], [_patch(["a"], {"size_scale": 1.3})]
    )
    assert compiled.diff.unavailable and not compiled.diff.empty()


def test_a_transition_whose_only_change_is_its_duration_is_a_change() -> None:
    slots = [dict(s, transition_after="crossfade", transition_duration_s=0.3) for s in SLOTS]
    ops = [
        {"op": "set_transition", "boundary_index": 0, "transition": "crossfade", "duration_s": 0.8}
    ]
    _r, _reply, compiled = _turn(
        [_req("style", "Make the crossfade longer")],
        ops,
        variant_extra={"user_timeline": {"slots": slots}},
    )
    assert any(e.lane == "transition" for e in compiled.diff.real())


def test_setting_the_title_it_already_has_is_not_a_change() -> None:
    title_row = dict(ROWS[0], role="title", text="Lisbon")
    _r, _reply, compiled = _turn(
        [_req("text", "Title is Lisbon", scope="title", literal="Lisbon")],
        [{"op": "set_title", "title": "Lisbon"}],
        rows=[title_row, ROWS[1]],
    )
    assert not [e for e in compiled.diff.real() if e.lane == "title"]
