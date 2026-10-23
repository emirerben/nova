"""Unsupported decorations must not stop an otherwise supported video.

Failure cases fixed before implementation: a title/label/SFX stops all creation;
dropping labels also drops a supported closing title; multiple unsupported fields
loop; the fallback hides omissions; media errors or ambiguous timing are waived.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import ProposeStrategy
from app.config import settings
from app.kria import planner
from app.kria.reply_language import reply_language_for
from app.kria.strategy_policy import CheckedStrategy, RefusedStrategy, check_strategy_for_runtime_v2
from tests.kria.test_strategy_policy import CLIPS, _talking, _talking_manifest


@pytest.mark.parametrize("language", ["en", "tr"])
def test_unsupported_labels_keep_the_title_closing_text_and_captions(prod_profile, language):
    strategy = _talking(
        opening_title="Breakfast",
        closing_title="MY PICK",
        shot_labels=["Day 1"],
        caption_style="karaoke",
    )
    with reply_language_for(language):
        checked = check_strategy_for_runtime_v2(
            _talking_manifest(),
            strategy,
            ask_before_simplifying=True,
            ask_about_stated_settings=True,
            simplify_unsupported=True,
        )
    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.shot_labels is None
    assert checked.strategy.opening_title == "Breakfast"
    assert checked.strategy.closing_title == "MY PICK"
    assert checked.strategy.caption_style == "karaoke"
    assert checked.strategy.selected_media_ids == [CLIPS[0]]
    assert checked.strategy.audio_strategy == "original_audio"
    assert checked.notices and all("?" not in notice for notice in checked.notices)
    assert "Day 1" in " ".join(checked.notices)
    rechecked = check_strategy_for_runtime_v2(_talking_manifest(), checked.strategy)
    assert isinstance(rechecked, CheckedStrategy)
    assert rechecked.strategy == checked.strategy


def test_multiple_unsupported_fields_are_removed_without_touching_the_source(
    prod_profile, monkeypatch
):
    monkeypatch.setattr(settings, "phone_subtitled_title_enabled", False)
    monkeypatch.setattr(settings, "phone_subtitled_closing_title_enabled", False)
    checked = check_strategy_for_runtime_v2(
        _talking_manifest(),
        _talking(opening_title="Start", closing_title="End", shot_labels=["Day 1"]),
        simplify_unsupported=True,
    )
    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.opening_title is None
    assert checked.strategy.closing_title is None
    assert checked.strategy.shot_labels is None
    assert checked.strategy.selected_media_ids == [CLIPS[0]]
    assert len(checked.notices) == 3


def test_missing_selected_media_is_still_a_blocker(prod_profile):
    strategy = _talking().model_copy(update={"selected_media_ids": ["missing.mp4"]})
    assert isinstance(
        check_strategy_for_runtime_v2(
            _talking_manifest(),
            strategy,
            simplify_unsupported=True,
        ),
        RefusedStrategy,
    )


@pytest.mark.asyncio
async def test_real_planner_drafts_supported_parts_on_the_first_message(prod_profile, monkeypatch):
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=_talking(opening_title="Breakfast", shot_labels=["Day 1"]),
        summary="Breakfast with a label on every shot.",
    )
    planned = await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        user_message='Title "Breakfast" and label each shot "Day 1".',
        manifest=_talking_manifest(),
        inputs=planner._CreatorInputs(
            agent_input=SimpleNamespace(),
            intent_clips=[],
            creator_request="Breakfast video",
        ),
        output=SimpleNamespace(action=action),
    )
    assert planned.plan.mode == "act", planned.plan.response
    arguments = planned.plan.intents[0].arguments
    assert arguments["strategy"]["opening_title"] == "Breakfast"
    assert not arguments["strategy"].get("shot_labels")
    assert "Day 1" in " ".join(planned.policy_notices)
    assert "with a label on every shot" not in arguments["summary"]


def test_unsupported_title_does_not_leave_its_hold_as_a_second_blocker(prod_profile, monkeypatch):
    monkeypatch.setattr(settings, "phone_subtitled_title_enabled", False)
    checked = check_strategy_for_runtime_v2(
        _talking_manifest(),
        _talking(opening_title="Breakfast", opening_title_duration_s=2),
        ask_before_simplifying=True,
        ask_about_stated_settings=True,
        simplify_unsupported=True,
    )
    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.opening_title is None
    assert checked.strategy.opening_title_duration_s is None


def test_unavailable_general_sound_effect_does_not_stop_a_talking_video(prod_profile):
    checked = check_strategy_for_runtime_v2(
        _talking_manifest(),
        _talking(licensed_sfx={"effect_id": "funny-1"}),
        ask_before_simplifying=True,
        simplify_unsupported=True,
    )
    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.licensed_sfx is None
    assert checked.notices


def test_pin_limits_do_not_erase_all_valid_pins(prod_profile):
    checked = check_strategy_for_runtime_v2(
        _talking_manifest(),
        _talking(pinned_texts=[{"text": "x" * 121, "corner": "top_left"}]),
        simplify_unsupported=True,
    )
    assert isinstance(checked, RefusedStrategy)
    assert checked.code == "pinned_text_too_long"
