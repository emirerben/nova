"""KRI-1068: after a reply stuck on an unplaced first / last clip, "skip that" drops that clip.

Leverkusen–Nürnberg M1 (thread 4144DA86, 2026-10-29, parent KRI-1066). The prompt asked for
"chronological order from arrival to the final whistle", so brief r2 stored
``last_clip: "the final whistle"``. No clip was seated last, so the draft backstop's render
contract refused: "I couldn't tell which clip "the final whistle" means, so I can't confirm
the order." The creator answered "Okay make it simplier" and got the identical sentence back,
then "Okay skip that part". Brief v1→v3 kept the pin: a same-kind change keeps first / last
clips (``STICKY_FACTS``, KRI-522), and "simpler" only answers a simpler-version offer, which
was never made. The chat ended with no video.

Ways this can fail, written before the code (each is pinned below):

 F1 the incident: "Okay make it simplier" right after the pin block leaves ``last_clip`` on
    the brief, so the next draft refuses on the same words.
 F2 the same for "Okay skip that part", "drop it", "never mind", "forget the ending" and
    a Turkish chat's "boşver" / "o kısmı atla".
 F3 the identical recovery sentence is sent twice in a row.
 F4 the second reply still offers nothing concrete: no way to drop the clip, no clip to name.
 F5 the way out the second reply offers ("skip it") is not itself read as a withdrawal.
 F6 a withdrawal drops a pin that did not block: "the lineup sheet" was seated first.
 F7 a withdrawal drops the whole order ask (filming order lost) instead of the one clip.
 F8 the brief still says "to the final whistle", or the next reply never says it was dropped.
 F9 KRI-522: a later message that does not withdraw the clip keeps it, including the model's
    own reworded change of the order requirement.
 F10 the same words when the previous reply was not stuck on a pin withdraw nothing.
 F11 "forget the opening" when only the ending blocked withdraws nothing.
 F12 a longer message that only contains a withdrawal phrase ("skip that part of the video
     where I'm talking") or a negated one ("don't skip it") is not a withdrawal.
 F13 the model changed the same requirement this turn: its change re-carries the sticky clip
     (undoing the withdrawal), or the two updates collide and the turn fails.
 F14 the creator named a different clip since the block (stale): nothing is withdrawn.
 F15 a pin-only ask ("end on X", no order basis) is withdrawn as a whole: no "ordering rule I
     can't verify" dead end left behind.
 F16 a null stored as the withdrawn fact, instead of the fact being gone.
 F17 a repeated recovery that is not about a clip also never goes out twice in a row.

Runs the real planner (the Main Creator and brief extractor return a recorded-shape output,
``tests/kria/test_creative_brief._wire_planner``), the real choice gate, the real draft
backstop (``kria_runtime._unresolved_choice_plan``) and the real render contract over the
incident's brief and footage (``kri1068_leverkusen_m1.json`` beside this file says which
parts are recorded and which are reconstructed). No model is called.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import AskUser, CreativeStrategy, ProposeStrategy
from app.config import settings
from app.kria import planner
from app.kria.brief import BriefUpdate, CreativeBrief, apply_updates
from app.kria.planner import PlannedKriaTurn, plan_live_turn
from app.kria.reply_language import reply_language_for
from app.schemas.clip_intents import ClipIntent, ResolvedClipIntent
from app.services.choice_questions import answered_brief, tag_event
from app.services.creator_render_contract import build_render_contract
from app.services.pin_withdrawal import (
    pin_block_coverage,
    unrepeated_reply,
    withdraw_stuck_pins,
    withdrawal_positions,
)
from app.tasks.kria_runtime import _unresolved_choice_plan
from tests.kria.test_choice_conflict_gate import _gate
from tests.kria.test_creative_brief import _ASK, _wire_planner

CASE: dict[str, Any] = json.loads(
    Path(__file__).with_name("kri1068_leverkusen_m1.json").read_text(encoding="utf-8")
)
PROMPT = CASE["events"][0]["content"]
STUCK = CASE["events"][1]["content"]
WHISTLE = "the final whistle"
LINEUP = "IMG_1038.HEIC"
LAST_FILMED = "IMG_1043.MOV"
SUMMARY = "Your night at the BayArena in the order you filmed it, opening on the lineup sheet."
DROPPED = (
    'As you asked, I dropped "the final whistle", so the video ends on the last clip you '
    "filmed. Tell me which clip to end on if you'd like a different one."
)


@pytest.fixture(autouse=True)
def _flags(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    # CLIP_INTENTS_ENABLED is on in prod (CLAUDE.md); the picker and choice gates default on.
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)


def _brief(**r2: Any) -> CreativeBrief:
    data = copy.deepcopy(CASE["brief_v1"])
    data["requirements"][1].update(r2)
    return CreativeBrief.model_validate(data)


def _rows() -> list[dict[str, Any]]:
    return [
        {
            "media_id": m["media_id"],
            "kind": m["kind"],
            "duration_s": m["duration_s"],
            "capture": {"capture_time": m["capture_time"]},
        }
        for m in CASE["media"]
    ]


def _draft(
    summary: str = SUMMARY, *, without: tuple[str, ...] = (), **extra: Any
) -> PlannedKriaTurn:
    """The turn's strategy draft as the planner hands it to the gate (no seated last clip);
    ``without`` leaves those clip intents out."""
    raw = copy.deepcopy(CASE["strategy"])
    resolved = [i for i in raw.pop("resolved_clip_intents") if i["intent_id"] not in without]
    requested = [i for i in raw.pop("clip_intents") if i["intent_id"] not in without]
    plan = planner.adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy.model_validate(raw),
            summary=summary,
        ),
        server_clip_intents=[ClipIntent.model_validate(i) for i in requested],
        server_resolved_clip_intents=[ResolvedClipIntent.model_validate(i) for i in resolved],
    )
    return PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64, **extra)


def _strategy(planned: PlannedKriaTurn) -> dict[str, Any]:
    assert planned.plan.mode == "act", planned.plan.response
    return planned.plan.intents[0].arguments["strategy"]


def _backstop(strategy: dict[str, Any], brief: CreativeBrief):  # noqa: ANN202
    """The runtime's draft backstop exactly as `_complete_draft_turn` calls it."""
    pinned = answered_brief(brief, strategy)
    return _unresolved_choice_plan(
        strategy,
        pinned,
        {"clip_assignments": _rows()},
        contract_brief=pinned,
        events=[],
        has_draft=False,
    )


def _stuck_reply(brief: CreativeBrief, strategy: dict | None = None) -> tuple[str, dict]:
    """Turn 1's reply and the payload the runtime stores with it."""
    strategy = strategy or _strategy(_draft())
    blocked = _backstop(strategy, brief)
    assert blocked is not None
    plan, reason = blocked
    return plan.response, {
        "turn_value": plan.turn_value,
        "brief_coverage": {
            "stage": "draft",
            "reason": reason,
            **pin_block_coverage(answered_brief(brief, strategy), plan.response),
        },
    }


def _events(*messages: str, brief: CreativeBrief | None = None) -> list[tuple[str, dict]]:
    """The thread as the planner loads it: the prompt, turn 1's stuck reply, then ``messages``."""
    _reply, payload = _stuck_reply(brief or _brief())
    out = [
        tag_event("user", {}, "user_message", PROMPT),
        tag_event("assistant", payload, "assistant_response", None),
    ]
    return out + [tag_event("user", {}, "user_message", m) for m in messages]


def _withdraw(message: str, brief: CreativeBrief | None = None, updates=(), events=None):  # noqa: ANN001, ANN202
    brief = brief or _brief()
    return withdraw_stuck_pins(
        brief, tuple(updates), _events(message, brief=brief) if events is None else events, message
    )


async def _plan_turn(monkeypatch: pytest.MonkeyPatch, message: str, model_updates=()):  # noqa: ANN001, ANN202
    """The real live planner for the creator's next message; the model asks a question."""
    # Turn 1 as it ran, with clip intents on (``_wire_planner`` turns them off).
    events = _events(message)
    output = SimpleNamespace(
        action=AskUser(**_ASK),
        brief_updates=[BriefUpdate.model_validate(u) for u in model_updates],
    )
    db, item, creator_id, _copilot, _runs = _wire_planner(
        monkeypatch, output=output, editor_plan=None, snapshot=None
    )
    item.current_job_id = None  # nothing rendered yet: the Main Creator reads the brief
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=_brief()))
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events))
    planned = await plan_live_turn(
        db, thread_id=uuid.uuid4(), item_id=item.id, creator_id=creator_id, user_message=message
    )
    monkeypatch.setattr(settings, "clip_intents_enabled", True)  # back on for the draft checks
    return planned


# ── turn 1: the stuck reply, as today ─────────────────────────────────────────


def test_turn1_is_stuck_on_the_final_whistle_and_records_which_clip() -> None:
    reply, payload = _stuck_reply(_brief())
    assert reply.startswith(STUCK)  # the recorded incident sentence
    assert payload["brief_coverage"]["blocked_pins"] == [
        {"requirement_id": "r2", "position": "last", "words": WHISTLE}
    ]  # F6: the seated lineup sheet is not recorded as stuck


# ── F1 / F2 / F7 / F8: the incident's next turns withdraw the ending ──────────


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["Okay make it simplier", "Okay skip that part"])
async def test_f1_the_creators_next_message_withdraws_the_ending_and_a_draft_follows(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    planned = await _plan_turn(monkeypatch, message)

    after = apply_updates(_brief(), planned.brief_updates, source_turn_id="t2")
    r2 = next(req for req in after.live() if req.id == "r2")
    # F7 / F16: only the ending goes; filming order and the opening stay, no null is stored.
    assert r2.facts == {"key": "capture_time", "first_clip": "the lineup sheet"}
    assert r2.description == "in the order you filmed them, starting with the lineup sheet"
    assert WHISTLE not in r2.text()  # F8
    assert planned.pin_notices == (DROPPED,)

    # The same draft that was refused on turn 1 now passes the backstop and the contract.
    gated = await _gate(
        monkeypatch,
        _draft(brief_updates=planned.brief_updates, pin_notices=planned.pin_notices),
        rows=_rows(),
        brief=_brief(),
        events=_events(message),
    )
    strategy = _strategy(gated)
    assert _backstop(strategy, after) is None
    contract = build_render_contract(
        strategy,
        generation_id="g",
        brief=answered_brief(after, strategy),
        media_snapshot={"clip_assignments": _rows()},
    )
    assert contract is not None and not contract.unresolved
    assert contract.order_basis == "capture_time"
    assert contract.order_ids[0] == LINEUP and contract.order_ids[-1] == LAST_FILMED
    # F8: the draft reply says it, whether it keeps the model summary or the receipts.
    summary = gated.plan.intents[0].arguments["summary"]
    assert summary.startswith(SUMMARY) and summary.endswith(DROPPED)
    assert DROPPED in gated.policy_notices


@pytest.mark.parametrize(
    ("message", "language"),
    [
        ("Okay skip that part", "en"),
        ("skip it", "en"),
        ("Drop it.", "en"),
        ("never mind", "en"),
        ("Nevermind, forget it", "en"),
        ("forget the ending", "en"),
        ("make it simpler please", "en"),
        ("ok just leave it out", "en"),
        ("boşver", "tr"),
        ("Tamam o kısmı atla", "tr"),
        ("daha basit yap", "tr"),
        ("bosver", "tr"),
    ],
)
def test_f2_each_way_of_saying_it_withdraws_only_the_stuck_ending(
    message: str, language: str
) -> None:
    with reply_language_for(language):
        result = _withdraw(message)
        (update,) = result.updates
        assert (update.operation, update.target_requirement_id) == ("change", "r2")
        after = apply_updates(_brief(), result.updates, source_turn_id="t")
        assert after.requirements[1].facts == {
            "key": "capture_time",
            "first_clip": "the lineup sheet",
        }
        (notice,) = result.notices
        assert WHISTLE in notice
        if language == "tr":
            assert "çektiğin son klip" in notice  # F14 of KRI-1025's copy: Turkish chat, Turkish


# ── F3 / F4 / F5 / F17: never the same recovery twice in a row ────────────────


def test_f3_the_second_stuck_reply_offers_the_ways_forward_and_they_work() -> None:
    reply, _payload = _stuck_reply(_brief())
    pinned = answered_brief(_brief(), _strategy(_draft()))

    again = unrepeated_reply(reply, reply, pinned)
    assert again != reply
    assert again.startswith(STUCK)  # still names the clip, so the next turn knows it was stuck
    assert pin_block_coverage(pinned, again)["blocked_pins"][0]["words"] == WHISTLE
    assert 'reply "skip it"' in again and "end on the last clip you filmed" in again
    assert withdrawal_positions("skip it") is not None  # F5: the offered words work
    # The reply after that is the first wording again, never the second one twice.
    assert unrepeated_reply(reply, again, pinned) == reply
    assert unrepeated_reply(reply, "Here's your draft.", pinned) == reply


def test_f3_turkish_chat_gets_turkish_ways_forward() -> None:
    with reply_language_for("tr"):
        reply, _payload = _stuck_reply(_brief())
        pinned = answered_brief(_brief(), _strategy(_draft()))
        again = unrepeated_reply(reply, reply, pinned)
    assert again != reply and '"boşver" yaz' in again
    assert withdrawal_positions("boşver") is not None


def test_f17_a_repeated_recovery_about_something_else_is_not_sent_twice() -> None:
    reply = "I need capture times for every selected clip to verify chronological order."
    again = unrepeated_reply(reply, reply, _brief())
    assert again != reply and again.startswith(reply)
    assert unrepeated_reply(reply, again, _brief()) == reply


# ── F9 / F10 / F11 / F12 / F14: nothing else is withdrawn ─────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "Okay make it 30 seconds",
        "skip that part of the video where I'm talking",
        "don't skip it",
        "forget the opening",  # F11: only the ending was stuck
        "end on the confetti",
        "make it simpler and add a title",
    ],
)
def test_f12_other_messages_withdraw_nothing(message: str) -> None:
    result = _withdraw(message)
    assert result.updates == () and result.notices == ()


def test_f10_the_same_words_after_a_reply_that_was_not_stuck_withdraw_nothing() -> None:
    events = [
        tag_event("user", {}, "user_message", PROMPT),
        tag_event(
            "assistant",
            {"turn_value": "action", "brief_coverage": {"stage": "draft"}},
            "assistant_response",
            None,
        ),
        tag_event("user", {}, "user_message", "skip that part"),
    ]
    assert _withdraw("skip that part", events=events).updates == ()
    # An older stuck reply, with a newer reply in between, does not count either.
    older = _events("Okay make it 30 seconds")
    later = [
        *older,
        tag_event("assistant", {"turn_value": "question"}, "assistant_response", None),
        tag_event("user", {}, "user_message", "skip that part"),
    ]
    assert _withdraw("skip that part", events=later).updates == ()
    # A newer reply about something else, with no message in between, is the last reply.
    asked = [
        *_events(),
        tag_event("assistant", {"turn_value": "question"}, "assistant_question", None),
        tag_event("user", {}, "user_message", "skip that part"),
    ]
    assert _withdraw("skip that part", events=asked).updates == ()
    # A render finishing in between is not a reply: the stuck reply is still the last one.
    progress = [
        *_events(),
        tag_event("assistant", {"job_id": "j"}, "generation_ready", None),
        tag_event("user", {}, "user_message", "skip that part"),
    ]
    assert len(_withdraw("skip that part", events=progress).updates) == 1


def test_f14_a_clip_named_since_the_block_is_not_withdrawn() -> None:
    renamed = _brief(facts={"key": "capture_time", "last_clip": "the confetti"})
    events = _events(brief=_brief()) + [tag_event("user", {}, "user_message", "skip that")]
    assert withdraw_stuck_pins(renamed, (), events, "skip that").updates == ()


@pytest.mark.asyncio
async def test_f9_kri522_a_reworded_order_change_still_keeps_the_ending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reword = {
        "operation": "change",
        "target_requirement_id": "r2",
        "kind": "order",
        "scope": "global",
        "description": "in the order I filmed it",
        "facts": {"key": "capture_time"},
    }
    planned = await _plan_turn(monkeypatch, "keep it in the order I filmed it", [reword])
    after = apply_updates(_brief(), planned.brief_updates, source_turn_id="t2")
    assert after.requirements[1].facts == {
        "key": "capture_time",
        "first_clip": "the lineup sheet",
        "last_clip": WHISTLE,
    }
    assert planned.pin_notices == ()


# ── F13 / F15 / F16: the brief update itself ──────────────────────────────────


@pytest.mark.asyncio
async def test_f13_the_models_own_change_this_turn_cannot_carry_the_ending_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restated = {
        "operation": "change",
        "target_requirement_id": "r2",
        "kind": "order",
        "scope": "global",
        "description": "chronological order, simpler",
        "facts": {"key": "capture_time"},
    }
    planned = await _plan_turn(monkeypatch, "Okay make it simplier", [restated])
    (update,) = [u for u in planned.brief_updates if u.target_requirement_id == "r2"]
    after = apply_updates(_brief(), planned.brief_updates, source_turn_id="t2")
    assert after.requirements[1].facts == {"key": "capture_time", "first_clip": "the lineup sheet"}
    assert planned.pin_notices == (DROPPED,)
    assert update.operation == "change"


def test_f13_a_model_remove_of_the_same_requirement_stands() -> None:
    remove = BriefUpdate(operation="remove", target_requirement_id="r2", expected_version=1)
    result = _withdraw("skip that part", updates=[remove])
    assert result.updates == (remove,)


def test_f15_a_pin_only_ending_is_withdrawn_as_a_whole() -> None:
    brief = _brief(facts={"last_clip": WHISTLE}, description="end on the final whistle")
    # With no basis order a seated opening would settle the order rule (KRI-510), so this
    # draft seats nothing: only the ending is left to refuse on.
    strategy = _strategy(_draft(without=("order_first_lineup",)))
    reply, payload = _stuck_reply(brief, strategy)
    assert payload["brief_coverage"]["blocked_pins"][0]["words"] == WHISTLE, reply
    events = [
        tag_event("user", {}, "user_message", PROMPT),
        tag_event("assistant", payload, "assistant_response", None),
        tag_event("user", {}, "user_message", "never mind"),
    ]
    result = withdraw_stuck_pins(brief, (), events, "never mind")
    (update,) = result.updates
    assert (update.operation, update.target_requirement_id) == ("remove", "r2")
    after = apply_updates(brief, result.updates, source_turn_id="t")
    assert [req.id for req in after.live()] == ["r1"]
    assert _backstop(strategy, after) is None
    assert "so I didn't pick a clip to end on" in result.notices[0]


def test_f16_kri522_a_change_that_sets_the_sticky_clip_to_null_withdraws_it() -> None:
    keep = BriefUpdate(
        operation="change",
        target_requirement_id="r2",
        expected_version=1,
        kind="order",
        scope="global",
        description="in the order I filmed it",
        facts={"key": "capture_time"},
    )
    kept = apply_updates(_brief(), [keep], source_turn_id="t")
    assert kept.requirements[1].facts["last_clip"] == WHISTLE  # KRI-522 unchanged
    drop = keep.model_copy(update={"facts": {"key": "capture_time", "last_clip": None}})
    dropped = apply_updates(_brief(), [drop], source_turn_id="t")
    assert dropped.requirements[1].facts == {
        "key": "capture_time",
        "first_clip": "the lineup sheet",
    }
