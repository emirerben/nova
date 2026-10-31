"""Leverkusen M2 (2026-10-30, thread d048973b): an ask the clip-intent planner has no op
for is never a "Please restate" dead end.

Turn f4a6b82a: "Put Taha's scream in slow motion" and "Keep that clip, just start it after
the kids" came back from the planner as intents with ops outside ``ClipIntentOp``.
``_IntentRejected("intent_invalid:op")`` dropped them and the salvage reply said "Please
restate just those so I can add them". No wording adds an op, so the restate looped and
no draft was made in four turns. The planner output was not stored; the stand-in below
has the turn's shape (6 kept, 2 ``non_clip_op_dropped``, 2 invented ops).

Ways this could go wrong, written down before the code:

1. A speed ask under an invented op that doesn't say "speed" ("slow_motion", "slowmo",
   "slow", "playback_rate") still asks the creator to restate.
2. The speed ask is dropped from the clip steps but never reaches the speed lane: the
   Creative Brief's words must still read as a 0.5x ask on Taha's scream.
3. A real clip op whose quote mentions slow motion ("include Taha's scream") is dropped.
4. Any other invented op ("trim_start", "highlight", a missing op) still asks to restate,
   or holds the turn so the draft never comes.
5. ...or vanishes without a word: the draft must say once, plainly, what the clip steps
   couldn't do and what they can do.
6. Every intent invented: the old "every intent was rejected" path asks to restate.
7. A restatable drop (a garbled quote) beside an invented op: its question also names the
   invented one, so restating it can never fully succeed.
8. The same unexpressible ask on the next turn produces a card again (a loop). Nothing
   blocks on it, so there is no card to repeat; the picker answer reuses the checked
   list, which keeps the notice.
9. An op another session adds to ``ClipIntentOp`` reads as invented.
10. Turkish chat: the notice is English.
11. Diagnostics carry creator text instead of closed-vocabulary counts.
"""

from __future__ import annotations

import functools
import json
from typing import get_args
from unittest.mock import AsyncMock

import pytest

from app.agents import clip_intent_planner as planner_module
from app.agents._runtime import RunContext
from app.agents.clip_intent_planner import ClipIntentPlannerAgent, ClipIntentPlannerInput
from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.reply_language import reply_language_for
from app.kria.speed_ask import speed_spec
from app.schemas.clip_intents import ClipIntentOp
from app.services import clip_intent_planning as service
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentClip, IntentResolution
from tests.agents.conftest import MockModelClient
from tests.kria import test_kri667_clip_exclusions as kri667

PROMPT = (
    "Quick reaction edit about my friend Taha. He's the one with brown hair, round glasses "
    "and the dark zip sweater (on the left in our selfie). Order: our selfie, the 0:0 "
    'scoreboard, the "TOR" on the big screen, then only the moments where Taha is '
    "celebrating in both goal clips. The kids at the start of the second goal clip are "
    "another family's children, so don't show them. Put Taha's scream in slow motion. Keep "
    'the stadium cheering on the goal clips. Label the selfie "Taha 🤝 me". Make it 12 seconds.'
)
TURN_2 = (
    "It's already there, in the goal clip with the kids at the start, about 6 seconds in. "
    "The big screen shows TOR."
)
TURN_3 = (
    "Neither. The TOR is in the other goal clip, the one with the kids, about 6 seconds in. "
    "Keep that clip, just start it after the kids (after about 2.5 seconds)."
)
REQUEST = "\n".join((PROMPT, TURN_2, TURN_3))
ORDER_QUOTE = (
    'Order: our selfie, the 0:0 scoreboard, the "TOR" on the big screen, then only the '
    "moments where Taha is celebrating in both goal clips."
)
SCREAM_QUOTE = "Put Taha's scream in slow motion."
TRIM_QUOTE = "Keep that clip, just start it after the kids (after about 2.5 seconds)."
KIDS_CLIP = "the goal clip with the kids at the start"
# What the draft quotes back: the creator's own words, not the model's name for the clip.
TRIM_ASK = "Keep that clip, just start it after the kids (after about 2.5 seconds)"


def _intent(intent_id: str, op: str | None, attribute: str, quote: str, **extra) -> dict:
    return {
        "intent_id": intent_id,
        "op": op,
        "attribute": attribute,
        "creator_text": None,
        "caption_attribute": None,
        "position": None,
        "source_quote": quote,
        **extra,
    }


def _kept() -> list[dict]:
    """The six intents turn f4a6b82a kept (its inventory on turn befdbf7e)."""
    return [
        _intent("order_selfie_first", "order", "our selfie", ORDER_QUOTE),
        _intent("order_scoreboard", "order", "the 0:0 scoreboard", ORDER_QUOTE),
        _intent("order_tor_screen", "order", 'the "TOR" on the big screen', ORDER_QUOTE),
        _intent(
            "order_taha_celebrating",
            "order",
            "the moments where Taha is celebrating in both goal clips",
            ORDER_QUOTE,
        ),
        _intent(
            "exclude_kids",
            "exclude",
            "the kids at the start of the second goal clip",
            "The kids at the start of the second goal clip are another family's children, "
            "so don't show them.",
        ),
        _intent(
            "label_selfie",
            "label",
            "the selfie",
            'Label the selfie "Taha 🤝 me".',
            creator_text="Taha 🤝 me",
        ),
    ]


def _turn_output(speed_op: str | None = "slow_motion", trim_op: str | None = "trim_start"):
    return {
        "intents": [
            *_kept(),
            _intent("keep_cheering", "audio", "the goal clips", "Keep the stadium cheering"),
            _intent("length_12s", "duration", "the whole video", "Make it 12 seconds."),
            _intent("slowmo_scream", speed_op, "Taha's scream", SCREAM_QUOTE),
            _intent("trim_kids_clip", trim_op, KIDS_CLIP, TRIM_QUOTE),
        ],
        "question": None,
    }


def _input(**extra) -> ClipIntentPlannerInput:
    return ClipIntentPlannerInput(
        creator_request=REQUEST,
        latest_user_message=TURN_3,
        available_clip_count=4,
        clip_exclusions=True,
        **extra,
    )


def _parse(raw: dict, planner_input: ClipIntentPlannerInput | None = None):
    agent = ClipIntentPlannerAgent(None)  # type: ignore[arg-type]
    return agent.parse(json.dumps(raw, ensure_ascii=False), planner_input or _input())


# -- the parser ---------------------------------------------------------------------------


def test_m2_turn_never_asks_to_restate_and_keeps_the_six_clip_steps() -> None:
    out = _parse(_turn_output())

    assert out.salvage_question is None  # was "... Please restate just those ..."
    assert out.salvage_reasons == []
    assert [i.intent_id for i in out.intents] == [i["intent_id"] for i in _kept()]
    # 5: the start trim is named for the draft's notice; 2: slow motion is the brief's.
    assert out.unexpressed == [TRIM_ASK]
    # 11: closed-vocabulary counts only.
    assert out.silent_drops == {
        "non_clip_op_dropped": 2,
        "speed_op_dropped": 1,
        "unexpressible_op_dropped": 1,
    }


@pytest.mark.parametrize(
    "op", ["slow_motion", "slowmo", "slow", "slo_mo", "playback_rate", "speed", "retime"]
)
def test_a_speed_ask_under_any_invented_op_goes_to_the_speed_lane(op: str) -> None:
    # 1
    out = _parse(_turn_output(speed_op=op, trim_op=None))
    assert out.salvage_question is None
    assert out.silent_drops.get("speed_op_dropped") == 1
    assert "Taha's scream" not in out.unexpressed


def test_the_brief_reads_the_same_words_as_half_speed_on_the_scream() -> None:
    # 2: the speed lane (KRI-689) the clip planner leaves the ask to.
    spec = speed_spec(
        BriefRequirement(id="r_slowmo", kind="style", scope="global", description=SCREAM_QUOTE)
    )
    assert spec is not None
    assert spec.rate == 0.5
    assert {"taha", "scream"} <= set(spec.target_words)


def test_a_real_op_quoting_slow_motion_is_kept() -> None:
    # 3
    raw = {"intents": [_intent("scream", "include", "Taha's scream", SCREAM_QUOTE)]}
    out = _parse({**raw, "question": None})
    assert [(i.op, i.attribute) for i in out.intents] == [("include", "Taha's scream")]
    assert out.silent_drops == {}


@pytest.mark.parametrize("op", ["trim_start", "start_after", "highlight", "order_by_time", None])
def test_any_other_invented_op_is_named_never_restated(op: str | None) -> None:
    # 4, 5
    out = _parse(_turn_output(speed_op=None, trim_op=op))
    assert out.salvage_question is None
    assert out.unexpressed == [TRIM_ASK]
    assert len(out.intents) == 6


def test_every_intent_invented_is_no_rejection() -> None:
    # 6
    raw = {
        "intents": [
            _intent("slowmo", "slow_motion", "Taha's scream", SCREAM_QUOTE),
            _intent("trim", "trim_start", KIDS_CLIP, TRIM_QUOTE),
        ],
        "question": None,
    }
    out = _parse(raw)
    assert out.intents == []
    assert out.salvage_question is None
    assert out.unexpressed == [TRIM_ASK]


def test_a_restatable_drop_beside_an_invented_op_names_only_itself() -> None:
    # 7
    raw = _turn_output()
    raw["intents"].append(_intent("bad", "group", "rooftop clips", "Group the rooftop clips"))
    out = _parse(raw)
    assert out.salvage_reasons == ["source_quote_not_creator_text"]
    assert "rooftop clips" in (out.salvage_question or "")
    assert TRIM_ASK not in (out.salvage_question or "")
    assert KIDS_CLIP not in (out.salvage_question or "")
    assert "Taha's scream" not in (out.salvage_question or "")


def test_an_op_added_to_the_schema_is_not_invented() -> None:
    # 9: the parser reads the schema's op list, not a copy of it.
    assert planner_module._CLIP_OPS == frozenset(get_args(ClipIntentOp))


# -- the notice ---------------------------------------------------------------------------


def test_the_notice_says_what_and_what_kria_can_do_and_never_restate() -> None:
    labels = [KIDS_CLIP, KIDS_CLIP, "Taha's chant"]
    notice = planner_module.unexpressed_notice(labels, language="en")
    assert notice is not None
    assert notice.count(KIDS_CLIP) == 1
    assert "Taha's chant" in notice
    assert "restate" not in notice.casefold()
    assert "skip" in notice and "caption" in notice
    assert planner_module.unexpressed_notice([], language="en") is None


def test_the_notice_follows_a_turkish_chat() -> None:
    # 10
    with reply_language_for("tr"):
        notice = planner_module.unexpressed_notice([KIDS_CLIP])
    assert notice is not None
    assert "yapamıyorum" in notice


# -- the turn -----------------------------------------------------------------------------


def _clips() -> list[IntentClip]:
    return [IntentClip(media_id=f"clip-{i}", kind="video", analysis=None) for i in range(4)]


@pytest.mark.asyncio
async def test_m2_turn_resolves_instead_of_holding_on_a_restate(monkeypatch) -> None:
    # 4, 8: the turn goes on to the resolver (and the draft); nothing blocks to repeat.
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    client = MockModelClient()
    client.queue("gemini-2.5-flash", _turn_output())
    monkeypatch.setattr(service, "default_client", lambda: client)
    resolver = AsyncMock(return_value=IntentResolution())
    monkeypatch.setattr(service, "resolve_clip_intents_for_turn", resolver)

    result = await service.plan_and_resolve_clip_intents(
        creator_request=REQUEST,
        latest_user_message=TURN_3,
        candidate_intents=None,
        clips=_clips(),
        run_context=RunContext(),
    )

    assert result.resolution.error_code != "planner_salvage"
    assert "restate" not in (result.resolution.question or "").casefold()
    resolver.assert_awaited_once()
    assert result.unexpressed_asks == (TRIM_ASK,)
    # 8: the picker's answer turn reuses this checked list, notice included.
    saved = service.ClipIntentPlannerOutput.model_validate(result.inventory["output"])
    assert saved.unexpressed == [TRIM_ASK]


async def test_the_draft_says_what_the_clip_steps_could_not_do(prod_profile, monkeypatch) -> None:
    # 5: once, on the draft, never a held turn.
    monkeypatch.setattr(
        kri667,
        "PlannedIntentResolution",
        functools.partial(PlannedIntentResolution, unexpressed_asks=(TRIM_ASK,)),
    )
    planned = await kri667._planned_turn(monkeypatch, [])
    summary = planned.plan.intents[0].arguments["summary"]
    assert summary.count(TRIM_ASK) == 1
    assert "restate" not in summary.casefold()


async def test_no_unexpressed_ask_adds_no_notice(prod_profile, monkeypatch) -> None:
    planned = await kri667._planned_turn(monkeypatch, [])
    assert "with your clips yet" not in planned.plan.intents[0].arguments["summary"]
