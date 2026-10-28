"""KRI-993: the pieces of "only about X" below the replay (see test_kri993_subject_scope.py).

Parse level (English and Turkish: the planner states the scope through the typed flag, nothing
is read from words on the server), the pick (`apply_subject_scope`) on the recorded manifest,
and the receipt row. The M2_03 vision match in the helpers is a stand-in for what the
resolver's vision re-query would answer; it was never recorded.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.agents.clip_intent_planner import ClipIntentPlannerAgent, ClipIntentPlannerInput
from app.kria.clip_scope import apply_subject_scope
from app.pipeline.unified_montage import plan_unified_montage
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from tests.kria.test_kri993_subject_scope import (
    BOARDS,
    CABIN,
    CASE,
    FRIEND,
    KNIFE_FORK,
    STEAK,
    STEAK_PHOTO,
    _agent_input,
    _clips,
    _dispatched,
    _drafted_strategy,
    _flags,  # noqa: F401  (autouse fixture)
    _friend_last,
    _include_steak,
    _skip_cheese,
)


def _plan_parse(request: str, intent: dict[str, Any], *, clip_exclusions: bool = True):
    raw = json.dumps({"intents": [intent], "question": None})
    return ClipIntentPlannerAgent(model_client=None).parse(
        raw, ClipIntentPlannerInput(creator_request=request, clip_exclusions=clip_exclusions)
    )


@pytest.mark.parametrize(
    ("request_text", "attribute", "quote"),
    [
        ("10-second montage, only about the steak.", "the steak", "only about the steak"),
        ("Sadece biftek ile 10 saniyelik montaj yap.", "biftek", "Sadece biftek"),
    ],
    ids=["english", "turkish"],
)
def test_the_planner_states_an_only_about_scope(
    request_text: str, attribute: str, quote: str
) -> None:
    out = _plan_parse(
        request_text,
        {
            "intent_id": "only-subject",
            "op": "include",
            "attribute": attribute,
            "exclusive": True,
            "source_quote": quote,
        },
    )

    [intent] = out.intents
    assert (intent.op, intent.exclusive) == ("include", True)
    assert intent.model_dump(mode="json")["exclusive"] is True


def test_a_plain_include_and_a_null_flag_stay_byte_identical() -> None:
    out = _plan_parse(
        "Make sure the gym clip is in.",
        {
            "intent_id": "gym",
            "op": "include",
            "attribute": "the gym",
            "exclusive": None,
            "source_quote": "the gym clip is in",
        },
    )

    [intent] = out.intents
    assert intent.exclusive is False
    assert "exclusive" not in intent.model_dump(mode="json")


def test_a_scope_on_any_other_op_is_dropped_not_obeyed() -> None:
    out = _plan_parse(
        "End on my friend smiling.",
        {
            "intent_id": "end",
            "op": "order",
            "position": "last",
            "attribute": "my friend smiling",
            "exclusive": True,
            "source_quote": "End on my friend smiling",
        },
    )

    [intent] = out.intents
    assert intent.exclusive is False


def test_the_kill_switch_hides_the_scope_from_prompt_and_parse() -> None:
    agent = ClipIntentPlannerAgent(model_client=None)
    on = agent.render_prompt(ClipIntentPlannerInput(creator_request="x", clip_exclusions=True))
    off = agent.render_prompt(ClipIntentPlannerInput(creator_request="x"))
    assert "exclusive" in on
    assert "exclusive" not in off
    [intent] = _plan_parse(
        "only about the steak",
        {
            "intent_id": "s",
            "op": "include",
            "attribute": "the steak",
            "exclusive": True,
            "source_quote": "only about the steak",
        },
        clip_exclusions=False,
    ).intents
    assert intent.exclusive is False


def test_exclusive_is_only_valid_on_an_include() -> None:
    with pytest.raises(ValueError, match="exclusive requires"):
        ClipIntent(intent_id="x", op="group", attribute="steak", exclusive=True)
    with pytest.raises(ValueError, match="exclusive requires"):
        ClipIntent(
            intent_id="x", op="include", attribute="steak", exclusive=True, creator_text="hi"
        )


# -- the pick: apply_subject_scope on the recorded manifest ------------------------------------


def _manifest() -> ResolvedCreatorManifest:
    return _agent_input().capability_manifest


def _strategy(selected: list[str] | None = None, **update: Any) -> CreativeStrategy:
    base = {
        **copy.deepcopy(CABIN["model_strategy"]),
        "media_scope": "selected",
        "selected_media_ids": list(CASE["model_selected"] if selected is None else selected),
        "clip_intents": [],
    }
    return CreativeStrategy.model_validate({**base, **update})


def _scope(strategy: CreativeStrategy, *intents: ResolvedClipIntent):
    return apply_subject_scope(_manifest(), strategy, resolved_intents=list(intents))


def _picked(strategy: CreativeStrategy) -> list[str]:
    return list(strategy.selected_media_ids)


def _scope_intent(status: str = "resolved", *rows: ClipAssignment) -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id="s",
        op="include",
        attribute="the dog",
        exclusive=True,
        status=status,
        assignments=list(rows),
    )


def test_the_scope_keeps_matched_and_pinned_clips_and_adds_a_matched_one_left_out() -> None:
    result = _scope(
        _strategy(),
        _include_steak(exclusive=True, vision_match=True),
        _skip_cheese(),
        _friend_last(),
    )

    assert set(_picked(result.strategy)) == {STEAK, KNIFE_FORK, FRIEND}
    assert result.dropped == (BOARDS,)
    assert result.added == (KNIFE_FORK,)


def test_nothing_matched_or_still_asked_drops_nothing() -> None:
    # 2: a scope that matched no clip (the creator is asked which show it) scopes nothing.
    asked = _scope_intent("needs_creator", ClipAssignment(media_id=STEAK, confidence=0.9))
    for intent in (_scope_intent(), asked):
        strategy = _strategy()
        assert _scope(strategy, intent).strategy is strategy


def test_a_below_bar_guess_neither_keeps_nor_adds_a_clip() -> None:
    # 3: only a confident match counts; a 0.4 guess adds nothing and the boards still go.
    guess = _scope_intent(
        "resolved",
        ClipAssignment(media_id=STEAK, confidence=0.9),
        ClipAssignment(media_id=KNIFE_FORK, confidence=0.4),
    )

    result = _scope(_strategy(), guess, _friend_last())

    assert set(_picked(result.strategy)) == {STEAK, FRIEND}
    assert result.added == ()


def test_a_match_outside_the_manifest_videos_changes_nothing() -> None:
    # 1: the only match is a photo: no own video would be left, so nothing is dropped.
    strategy = _strategy()
    alien = _scope_intent("resolved", ClipAssignment(media_id=STEAK_PHOTO, confidence=0.9))

    assert _scope(strategy, alien).strategy is strategy


def test_a_creator_skip_beats_a_scope_match() -> None:
    # 4 / 5: the creator skipped M2_03 by tap; the scope matching it never adds it back.
    skip_knife_fork = ResolvedClipIntent(
        intent_id="skip",
        op="exclude",
        attribute="the waiter looking up",
        status="resolved",
        assignments=[ClipAssignment(media_id=KNIFE_FORK, confidence=1.0)],
    )

    result = _scope(
        _strategy(),
        _include_steak(exclusive=True, vision_match=True),
        skip_knife_fork,
        _friend_last(),
    )

    assert set(_picked(result.strategy)) == {STEAK, FRIEND}
    assert result.added == ()


def test_a_clip_the_strategy_places_stays_whatever_the_scope_says() -> None:
    # 4: a pinned closing clip is the creator's own placement.
    strategy = _strategy(ending_clip={"media_id": BOARDS})

    result = _scope(strategy, _include_steak(exclusive=True))

    assert BOARDS in _picked(result.strategy)


def test_an_all_scope_becomes_the_matched_clips_plus_the_pinned_one() -> None:
    result = _scope(
        _strategy(media_scope="all", selected_media_ids=[]),
        _include_steak(exclusive=True),
        _skip_cheese(),
        _friend_last(),
    )

    assert result.strategy.media_scope == "selected"
    assert set(_picked(result.strategy)) == {STEAK, FRIEND}


def test_a_scope_that_changes_nothing_returns_the_same_strategy() -> None:
    strategy = _strategy(selected=[STEAK, FRIEND])

    assert _scope(strategy, _include_steak(exclusive=True), _friend_last()).strategy is strategy


def test_a_plain_include_is_never_a_scope() -> None:
    strategy = _strategy()

    assert _scope(strategy, _include_steak(exclusive=False)).strategy is strategy


async def test_the_receipt_row_is_met_only_when_every_played_clip_shows_the_subject(
    prod_profile, monkeypatch
) -> None:
    strategy = await _drafted_strategy(
        monkeypatch,
        [_include_steak(exclusive=True, vision_match=True), _skip_cheese(), _friend_last()],
    )
    clips = _clips(_dispatched(strategy))
    plan = plan_unified_montage(clips, None, strategy=strategy, clip_intents_enabled=True)
    rows = [r for r in plan.intent_outcomes if r["op"] == "include"]
    assert [(r["name"], r["status"]) for r in rows] == [("only about the steak being cut", "met")]

    # The unscoped pick (boards played, not matched) is reported partial, never met.
    unscoped = {**strategy, "selected_media_ids": list(CASE["model_selected"])}
    clips = _clips(_dispatched(unscoped))
    plan = plan_unified_montage(clips, None, strategy=unscoped, clip_intents_enabled=True)
    rows = [r for r in plan.intent_outcomes if r["op"] == "include"]
    assert [r["status"] for r in rows] == ["partial"]
