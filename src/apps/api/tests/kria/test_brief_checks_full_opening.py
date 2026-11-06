"""Qendresa item 16 (S78): "Start with me full screen" is judged against what the video draws.

Test kit run Q2 (2026-11-04): the KRI-547 framing words read "full screen" as "fill the frame",
the phone Talking worker frames a vertical take as `not_landscape`, and the receipt said "Your clip
is already vertical, so it fills the frame" while the finished video draws her small in the
bottom-right corner from 0 s. Ways this can fail, each pinned below:

 1. a corner speaker from 0 s answers a framing ask "met" (the kit bug);
 2. an opening that WAS made is not reported (seconds), or one that was dropped is called made;
 3. the reason a dropped opening gives is missing or wrong (app too old / not available / take
    too short), or it is not told in Turkish;
 4. an editor Save (the variant's own document) loses to the stale lane receipt;
 5. a draft promises an opening the repair dropped, or hides that the model left it out;
 6. one requirement that asks for the background AND the opening is called "met" with the opening
    missing;
 7. the fix leaks into asks with no corner (a framing ask on a video with no background keeps its
    old answer) and into flag-off requirements without the new fact;
 8. the model-recorded fact `speaker_full_opening` is ignored, or the creator's words are read
    for it    (KRI-667: only the fact counts).
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from app.agents._schemas.reaction_beats import BackgroundMedia
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    PlanFacts,
    SpeakerBackgroundFact,
    build_receipts,
    plan_facts_from_phone_variant,
)
from app.kria.reply_language import reply_language_for

OPENING_WORDS = BriefRequirement(
    id="r1", kind="style", scope="global", description="Start with me full screen", facts={}
)
OPENING_FACT = BriefRequirement(
    id="r1",
    kind="style",
    scope="global",
    description="Start with me full screen",
    facts={"speaker_full_opening": True},
)
BOTH = BriefRequirement(
    id="r1",
    kind="style",
    scope="global",
    description="Start with me full screen, then my recordings with me small bottom right",
    facts={"speaker_background": True, "speaker_small": True, "speaker_full_opening": True},
)
BACKGROUND_ONLY = BriefRequirement(
    id="r1",
    kind="style",
    scope="global",
    description="My recordings behind me, me small bottom right",
    facts={"speaker_background": True, "speaker_small": True},
)


def _variant(*, opening_s: float | None = None, dropped: str | None = None) -> dict[str, Any]:
    """A finished phone Talking variant shaped like the worker writes it (corner from 0 s)."""
    background: dict[str, Any] = {"windows": 5, "media": 5, "speaker_frame": "small_bottom_right"}
    if opening_s is not None:
        background["full_screen_opening_s"] = opening_s
    return {
        "variant_id": "subtitled",
        "render_destination": "device",
        "resolved_archetype": "subtitled",
        "edit_format": "subtitled",
        "caption_cues": [{"text": "Here are five projects.", "start_s": 0.0, "end_s": 1.6}],
        "text_elements": [],
        "speaker_framing": {
            "version": 1,
            "mode": "centre_fill",
            "reason": "not_landscape",
            "eligible": False,
            "asked_by": ["r1"],
        },
        "phone_lane_receipt": {
            "applied": ["background"],
            "dropped": (
                [{"lane": "background", "part": "full_screen_opening", "reason": dropped}]
                if dropped
                else []
            ),
            "background": background,
        },
    }


def _receipt(req: BriefRequirement, facts: PlanFacts):
    (receipt,) = build_receipts([req], facts, include_unchecked=True)
    return receipt


def _finished(**kwargs: Any) -> PlanFacts:
    return plan_facts_from_phone_variant(_variant(**kwargs))


def _draft(fact: SpeakerBackgroundFact | None) -> PlanFacts:
    return dataclasses.replace(
        PlanFacts(), edit_format="subtitled", phone_talking=True, speaker_background=fact
    )


def _planned(**kwargs: Any) -> SpeakerBackgroundFact:
    base: dict[str, Any] = {
        "status": "planned",
        "pool_count": 5,
        "fill_id": "asset-1",
        "window_count": 5,
        "media_count": 5,
        "windows_open": True,
        "frame": "small_bottom_right",
    }
    return SpeakerBackgroundFact(**{**base, **kwargs})


# ------------------------------------------------ 1. the corner from 0 s is never "fills the frame"


def test_a_corner_speaker_from_0s_never_meets_start_full_screen_by_the_words():
    receipt = _receipt(OPENING_WORDS, _finished())
    assert receipt.status == "partial"  # the background made the corner; not a failure
    assert "bottom-right corner" in receipt.reason
    assert "fills the frame" not in receipt.reason


def test_a_framing_ask_on_a_video_with_no_background_keeps_its_old_answer():
    variant = _variant()
    variant["phone_lane_receipt"] = {"applied": [], "dropped": []}
    receipt = _receipt(OPENING_WORDS, plan_facts_from_phone_variant(variant))
    assert receipt.status == "met"
    assert receipt.reason == "Your clip is already vertical, so it fills the frame"


def test_a_full_size_speaker_over_a_background_keeps_its_old_answer():
    variant = _variant()
    del variant["phone_lane_receipt"]["background"]["speaker_frame"]
    receipt = _receipt(OPENING_WORDS, plan_facts_from_phone_variant(variant))
    assert receipt.status == "met"
    assert "fills the frame" in receipt.reason


@pytest.mark.parametrize(
    ("frame", "words"),
    [
        ("small_bottom_right", "bottom-right"),
        ("small_bottom_left", "bottom-left"),
        ("small_top_right", "top-right"),
        ("small_top_left", "top-left"),
    ],
)
def test_every_corner_is_named_in_plain_words(frame, words):
    variant = _variant()
    variant["phone_lane_receipt"]["background"]["speaker_frame"] = frame
    receipt = _receipt(OPENING_WORDS, plan_facts_from_phone_variant(variant))
    assert words in receipt.reason


# ------------------------------------------------ 2. an opening that was made / was not made


def test_an_opening_that_was_made_is_met_with_its_seconds_when_the_fact_asks_for_it():
    receipt = _receipt(OPENING_FACT, _finished(opening_s=7.2))
    assert receipt.status == "met"
    assert "7.2" in receipt.reason or "7 s" in receipt.reason
    assert "bottom-right corner" in receipt.reason


def test_an_opening_that_was_made_for_the_framing_words_alone_is_only_partly_what_was_asked():
    # Decision: the words "full screen" alone can also mean "fill the frame the whole video"
    # (KRI-547), so without the typed fact the receipt tells the truth (full screen first, then
    # the corner) but does not claim the ask is met.
    receipt = _receipt(OPENING_WORDS, _finished(opening_s=7.2))
    assert receipt.status == "partial"
    assert "bottom-right corner" in receipt.reason


def test_an_opening_asked_but_dropped_names_the_corner_and_says_to_update_the_app():
    receipt = _receipt(OPENING_FACT, _finished(dropped="app_too_old"))
    assert receipt.status == "not_possible"
    assert "bottom-right corner" in receipt.reason
    assert "update" in receipt.reason.lower()


def test_an_opening_dropped_because_the_take_was_too_short_says_so():
    receipt = _receipt(OPENING_FACT, _finished(dropped="take_too_short"))
    assert receipt.status == "not_possible"
    assert "too short" in receipt.reason


def test_an_opening_that_is_not_available_yet_says_so():
    receipt = _receipt(OPENING_FACT, _finished(dropped="unavailable"))
    assert receipt.status == "not_possible"
    assert "isn't available" in receipt.reason


def test_an_opening_asked_but_never_recorded_still_names_the_corner():
    receipt = _receipt(OPENING_FACT, _finished())
    assert receipt.status == "not_possible"
    assert "bottom-right corner" in receipt.reason


# ------------------------------------------------ 4. the editor document wins over the receipt


def _saved(windows: list[dict[str, float]] | None) -> PlanFacts:
    variant = _variant(opening_s=7.2)  # a stale lane receipt that claims an opening
    document: dict[str, Any] = {
        "media_id": "asset-1",
        "windows": [{"media_id": "asset-1", "start_s": 0.0, "end_s": 5.0}],
        "speaker_frame": {"x": 0.55, "y": 0.62, "width": 0.42, "height": 0.34},
    }
    if windows is not None:
        document["full_screen_windows"] = windows
    variant["speaker_background"] = document
    return plan_facts_from_phone_variant(variant)


def test_an_editor_save_that_removed_the_opening_beats_the_stale_lane_receipt():
    fact = _saved(None).speaker_background
    assert fact is not None and fact.full_screen_opening_s is None
    receipt = _receipt(OPENING_FACT, _saved(None))
    assert receipt.status == "not_possible"


def test_an_editor_save_that_kept_the_opening_reports_its_own_length():
    facts = _saved([{"start_s": 0.0, "end_s": 4.5}, {"start_s": 20.0, "end_s": 24.0}])
    assert facts.speaker_background.full_screen_opening_s == pytest.approx(4.5)
    receipt = _receipt(OPENING_FACT, facts)
    assert receipt.status == "met"
    assert "4.5" in receipt.reason


def test_an_editor_window_that_is_not_at_the_start_is_not_an_opening():
    facts = _saved([{"start_s": 20.0, "end_s": 24.0}])
    assert facts.speaker_background.full_screen_opening_s is None


# ------------------------------------------------ 5. the draft


def test_a_draft_that_plans_the_opening_says_when_she_moves_into_the_corner():
    receipt = _receipt(
        OPENING_FACT, _draft(_planned(opening_planned=True, asked_full_opening=True))
    )
    assert receipt.status == "met"
    assert "full screen" in receipt.reason
    assert "corner" in receipt.reason


def test_a_draft_that_planned_the_opening_without_a_corner_has_no_corner_words():
    fact = _planned(opening_planned=True, asked_full_opening=True, frame=None)
    receipt = _receipt(OPENING_FACT, _draft(fact))
    assert receipt.status == "met"
    assert "corner" not in receipt.reason


@pytest.mark.parametrize(
    ("code", "word"),
    [
        ("app_too_old", "update"),
        ("unavailable", "isn't available"),
        ("disabled", "isn't available"),
    ],
)
def test_a_draft_whose_opening_was_closed_names_why(code, word):
    fact = _planned(asked_full_opening=True, opening_code=code)
    receipt = _receipt(OPENING_FACT, _draft(fact))
    assert receipt.status == "not_possible"
    assert word in receipt.reason.lower()
    assert "bottom-right corner" in receipt.reason


def test_a_draft_the_model_left_the_opening_out_of_is_partial_not_met(monkeypatch):
    _gate(monkeypatch, open_from=None)  # the gate is open: the omission is the model's
    receipt = _receipt(OPENING_FACT, _draft(_planned()))
    assert receipt.status == "partial"
    assert "bottom-right corner" in receipt.reason


def test_a_draft_with_no_background_cannot_start_full_screen_before_one():
    fact = SpeakerBackgroundFact("none", pool_count=0, asked_full_opening=True)
    receipt = _receipt(OPENING_FACT, _draft(fact))
    assert receipt.status != "met"


def test_a_draft_corner_without_an_opening_is_not_called_vertical_fill():
    receipt = _receipt(
        OPENING_WORDS,
        dataclasses.replace(
            _draft(_planned()),
            speaker_framing="centre_fill",
            speaker_framing_reason="not_landscape",
        ),
    )
    assert receipt.status != "met"
    assert "corner" in receipt.reason


# ------------------------------------------------ 6. one requirement, background AND opening


def test_background_and_opening_in_one_requirement_is_partial_while_the_opening_is_missing():
    receipt = _receipt(BOTH, _finished())
    assert receipt.status == "partial"
    assert "full screen" in receipt.reason


def test_background_and_opening_in_one_requirement_is_met_when_both_happened():
    receipt = _receipt(BOTH, _finished(opening_s=7.2))
    assert receipt.status == "met"
    assert "7.2" in receipt.reason


def test_a_background_only_requirement_is_judged_exactly_as_before():
    before = _receipt(BACKGROUND_ONLY, _finished())
    assert before.status == "met"
    assert "full screen" not in before.reason.lower()


# ------------------------------------------------ 3. Turkish


def test_the_receipts_read_in_turkish_for_a_turkish_chat():
    with reply_language_for("tr"):
        dropped = _receipt(OPENING_FACT, _finished(dropped="app_too_old"))
        made = _receipt(OPENING_FACT, _finished(opening_s=7.2))
        corner = _receipt(OPENING_WORDS, _finished())
    for receipt in (dropped, made, corner):
        assert "corner" not in receipt.reason and "screen" not in receipt.reason
    assert "güncelle" in dropped.reason.lower()
    assert "tam ekran" in made.reason.lower()
    assert "sağ alt" in corner.reason


# ------------------------------------------------ 8. only the fact counts (KRI-667)


def test_the_words_alone_never_make_the_opening_route():
    from app.kria.brief_checks import _check_requirement

    req = OPENING_WORDS.model_copy(
        update={"description": "starts full screen, speaker_full_opening"}
    )
    # still the KRI-547 framing route (neutral corner answer), not the opening route
    receipt = _check_requirement(req, _finished())
    assert receipt.status == "partial" and "don't fill the frame" in receipt.reason


def test_a_false_fact_is_no_ask():
    req = OPENING_FACT.model_copy(update={"facts": {"speaker_full_opening": False}})
    from app.kria.brief_checks import _full_opening_asked

    assert not _full_opening_asked(req)


# ------------------------------------------------ readiness


def test_the_refused_opening_is_a_checked_refusal_the_readiness_lists():
    from app.kria.brief_checks import is_judged, needs_creator_choice

    receipt = _receipt(OPENING_FACT, _finished(dropped="app_too_old"))
    assert is_judged(OPENING_FACT, receipt)
    assert receipt.verification == "checked"
    assert needs_creator_choice(receipt)


# ------------------------------------------------ 5b. the draft facts from a real strategy


needs_schema = pytest.mark.skipif(
    "full_screen_opening" not in BackgroundMedia.model_fields,
    reason="BackgroundMedia.full_screen_opening lands with the strategy/render worker",
)


@needs_schema
def test_the_draft_facts_read_the_asked_opening_off_the_strategy(prod_profile, monkeypatch):
    from app.config import settings
    from app.kria.brief_checks import plan_facts_from_strategy
    from app.services import creator_capabilities as capabilities

    photo = "asset-5b2f9d1e-8c3a-4f6b-9e21-7a0d4c3b2a10"
    take = "analysis-proxy-ios-A.mp4"
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    monkeypatch.setattr(settings, "speaker_full_opening_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [
            *settings.phone_render_verified_features,
            "speakerBackground",
            "speakerBackgroundWindows",
            "speakerFrameWindows",
        ],
    )
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-16",
        edit_format="subtitled",
        media=[
            {"media_id": take, "kind": "video", "duration_s": 24.6},
            {"media_id": photo, "kind": "image", "label": "a.jpg"},
        ],
        phone_source_media_ids=[take],
        phone_rendering_allowed=True,
    )
    strategy = {
        "direction": "fast_montage",
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        "render_program": "native",
        "target_duration_s": 24,
        "selected_media_ids": [take],
        "rationale": "Keep the take whole.",
        "background_media": {"media_id": photo, "full_screen_opening": True},
    }
    fact = plan_facts_from_strategy(strategy, clip_ids=[take], manifest=manifest).speaker_background
    assert fact is not None and fact.asked_full_opening


# ---- review fixes: flag-off wording, old builds, no background


def _gate(monkeypatch, *, open_from: int | None = 41, reason: str = "app_too_old"):
    """The planner's gate: closed with ``reason`` below build ``open_from`` (None = always open)."""
    from app.services import phone_rollout

    monkeypatch.setattr(
        phone_rollout,
        "speaker_full_opening_planned_reason",
        lambda build: (
            None if open_from is None or (build is not None and build >= open_from) else reason
        ),
    )


def test_the_flag_off_corner_answer_is_neutral_and_partial(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "speaker_full_opening_enabled", False)
    receipt = _receipt(OPENING_WORDS, _finished())
    assert receipt.status == "partial"
    assert "bottom-right corner" in receipt.reason
    assert "fill the frame" in receipt.reason
    assert "fills the frame" not in receipt.reason
    assert "not full screen first" not in receipt.reason
    with reply_language_for("tr"):
        tr = _receipt(OPENING_WORDS, _finished())
    assert "sağ alt" in tr.reason and "screen" not in tr.reason


@pytest.mark.parametrize("build", [35, 41])
def test_a_draft_on_an_old_build_names_the_app_not_the_model(monkeypatch, build):
    _gate(monkeypatch)
    fact = _planned(client_protocol=build)  # the strategy never carried the ask
    receipt = _receipt(OPENING_FACT, _draft(fact))
    if build == 35:
        assert receipt.status == "not_possible"
        assert "update" in receipt.reason.lower()
    else:
        assert receipt.status == "partial"  # the gate is open: the model left it out
        assert "update" not in receipt.reason.lower()


@pytest.mark.parametrize(
    ("reason", "word"), [("unavailable", "isn't available"), ("disabled", "isn't available")]
)
def test_a_draft_with_the_gate_closed_says_not_available_yet(monkeypatch, reason, word):
    _gate(monkeypatch, reason=reason)
    receipt = _receipt(OPENING_FACT, _draft(_planned(client_protocol=41 - 10)))
    assert receipt.status == "not_possible" and word in receipt.reason.lower()


@pytest.mark.parametrize("build", [35, 41])
def test_a_finished_video_on_an_old_build_says_update_the_app(monkeypatch, build):
    from app.services.client_protocol import bind_sending_build, unbind_sending_build

    _gate(monkeypatch)
    token = bind_sending_build(build)
    try:
        receipt = _receipt(OPENING_FACT, _finished())
    finally:
        unbind_sending_build(token)
    assert receipt.status == "not_possible"
    assert ("update" in receipt.reason.lower()) is (build == 35)


def test_the_closed_code_is_none_when_the_gate_is_open(monkeypatch):
    from app.kria.brief_checks import _opening_closed_code

    _gate(monkeypatch)
    assert _opening_closed_code(41) is None
    assert _opening_closed_code(35) == "app_too_old"


def test_a_draft_whose_background_did_not_survive_says_she_stays_full_screen(monkeypatch):
    _gate(monkeypatch, open_from=None)
    fact = SpeakerBackgroundFact("none", pool_count=0, asked_full_opening=True)
    receipt = _receipt(OPENING_FACT, _draft(fact))
    assert receipt.status == "partial"
    assert "whole video" in receipt.reason
    assert "isn't available" not in receipt.reason
