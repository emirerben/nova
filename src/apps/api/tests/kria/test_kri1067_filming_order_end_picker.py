"""KRI-1067: a filming-order montage whose brief names an ending no clip intent seats asks
with the clip picker (the last filmed clip pre-ticked), never a bare recovery.

Leverkusen-Nürnberg M1, thread 4144DA86 (2026-10-29, Fly 3c8d4dd82): "Keep it in chronological
order from arrival to the final whistle. Open on the lineup sheet, ...". Brief r2 stored
``key: capture_time``, ``first_clip: "the lineup sheet"``, ``last_clip: "the final whistle"``.
The clip-intent planner seated the lineup sheet but wrote no ``last`` pin, so nothing ever
asked about "the final whistle"; the draft backstop's render contract found a stated last clip
with no seated row, and the turn ended on "I couldn't tell which clip "the final whistle"
means, so I can't confirm the order." as a recovery: no picker, no question, no buttons. The
creator's "make it simpler" got the same sentence.

Ways this can fail, written before the code (each is pinned below):

 F1 the incident: the planner leaves out the brief's stated ending, nothing asks about it, and
    the turn ends on the contract's `_pin_not_found` with nothing to tap.
 F2 the picker for it pre-ticks nothing, so "the last thing I filmed" is a hunt through every
    thumbnail; or it ticks a clip and the reply does not say which one.
 F3 the same for a stated first clip: the first filmed clip must be the one ticked.
 F4 tapping the ticked clip is lost on the next turn (the planner re-inventories every turn and
    leaves the ending out again), so the picker comes back or the contract refuses again.
 F5 "None" (KRI-1025) or skipping the picker dead-ends instead of drafting on the last clip
    filmed and saying so.
 F6 the planner did pin that spot (other words, a file name): a second ending is added.
 F7 the planner wrote a plain sequence row with the same words: a conflicting pin is added
    (KRI-1123: that row takes the spot instead, so the contract has a seated ending; it was left
    unpinned and the contract refused by name).
 F8 no filming order (a pin-only "end on X", upload order): anything changes. Out of scope;
    KRI-762's path stays as it is.
 F9 no stated ending, or no brief: anything changes.
 F10 the resolver matches the ending itself: a question is asked anyway.
 F11 the resolver already pre-ticked a near miss: it is replaced or joined by the filming end.
 F12 a clip with no filming time is ticked as "the last clip you filmed"; with fewer than two
     dated candidates there is no filming order (`order_by_capture_time`), so nothing is ticked
     and nothing is said about a tick.
 F13 a Turkish chat hears about the tick in English.
 F14 the added ask breaks the inventory cap (MAX_CLIP_INTENTS).

The turns run the real Main Creator parser, the real runtime-v2 planner
(`_plan_from_creator_output`), the real clip-intent planning and resolver plumbing, the real
choice gate (`_gate_unresolved_choices`), the real draft backstop (`_unresolved_choice_plan`)
and the real render contract over this run's prompt, brief r2 and clip filming times
(`kri1067_leverkusen_m1_turn1.json`, which also says what is reconstructed). Only the model
transport is replayed: no model is called.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents._runtime import ModelInvocation, RunContext
from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.agents._skills import load_skill
from app.agents.clip_question import ClipQuestionAgent
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.brief import CreativeBrief
from app.kria.planner import PlannedKriaTurn
from app.kria.reply_language import reply_language_for
from app.pipeline.prompt_loader import load_prompt
from app.schemas.clip_intents import MAX_CLIP_INTENTS, ClipIntent, ResolvedClipIntent
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution as resolution
from app.services.choice_questions import answered_brief
from app.services.clip_intent_planning import plan_and_resolve_clip_intents, with_brief_anchors
from app.services.clip_intent_resolution import IntentClip
from app.services.clip_order_sequence import stated_anchors
from app.services.creator_capabilities import resolve_creator_manifest
from app.services.creator_render_contract import build_render_contract
from app.tasks.kria_runtime import _unresolved_choice_plan
from tests.kria.test_choice_conflict_gate import _gate

FIXTURE = Path(__file__).with_name("kri1067_leverkusen_m1_turn1.json")
CASE: dict[str, Any] = json.loads(FIXTURE.read_text())
CABIN: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "kri623_tigre_m2_montage_turn.json"
    ).read_text()
)
BY_FILE = {m["file_name"]: m["media_id"] for m in CASE["media"]}
VIDEOS = [m for m in CASE["media"] if m["kind"] == "video"]
FILMED = [m["media_id"] for m in sorted(VIDEOS, key=lambda m: m["capture_time"])]
LAST_FILMED = BY_FILE["IMG_1043"]  # after the final whistle, 21:47:37Z
FIRST_FILMED = BY_FILE["IMG_1012"]  # the team bus, 18:21:28Z
FULL_TIME = BY_FILE["IMG_1039"]  # the late goal, 21:35:40Z
LINEUP = BY_FILE["IMG_1038"]  # the printed lineup sheet (a Visuals photo)
EVERYTHING = [m["media_id"] for m in CASE["media"]]
PROMPT = CASE["prompt"]
DEAD_END = CASE["turn1_reply"]
ENDING = "the final whistle"
ENDING_KEY = "order:the-final-whistle"
TICKED_LAST = f'I ticked the last clip you filmed for "{ENDING}".'
# The text the app sends with a picker answer (any words: the selection is what counts).
ANSWER = f"{ENDING.capitalize()}: answered"
SUMMARY = "A 45-second montage of your night at the BayArena, in the order you filmed it."
INVENTORY: list[dict[str, Any]] = CASE["inventory_turn1"]


@pytest.fixture(autouse=True)
def _flags(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    # CLIP_INTENTS_ENABLED and CLIP_FACTS_ENABLED are on in prod (CLAUDE.md); the picker and
    # the choice gate default on.
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)

    def _no_vision(self, input, *, ctx=None):  # noqa: A002, ANN001, ANN202
        raise AssertionError("no vision check is part of this flow")

    monkeypatch.setattr(ClipQuestionAgent, "run", _no_vision)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _intent_clips(*, dated: set[str] | None = None) -> list[IntentClip]:
    """The chat's clips and Visuals photos, in the order they were attached.

    ``dated``: the media that keep their filming time (None: all of them, as in the run)."""
    return [
        IntentClip(
            media_id=m["media_id"],
            kind=m["kind"],
            analysis={"subject": m["shows"]},
            gcs_path=f"users/u/{m['media_id']}",
            capture_time=(
                _time(m["capture_time"]) if dated is None or m["media_id"] in dated else None
            ),
            file_name=m["file_name"],
            duration_s=m.get("duration_s"),
        )
        for m in CASE["media"]
    ]


def _rows() -> list[dict[str, Any]]:
    """The approved media snapshot's clip rows: the videos, each with its filming time."""
    return [
        {
            "media_id": m["media_id"],
            "kind": "video",
            "duration_s": m["duration_s"],
            "capture": {"capture_time": m["capture_time"]},
        }
        for m in VIDEOS
    ]


def _brief(**facts: Any) -> CreativeBrief:
    data = copy.deepcopy(CASE["brief_v1"])
    if facts:
        data["requirements"][0]["facts"] = facts
    return CreativeBrief.model_validate(data)


# -- the model transport, replayed ---------------------------------------------------------

_PLANNER_PROMPT = load_prompt("clip_intent_planner")[:60]
_RESOLVER_PROMPT = (
    load_skill("meaningful-moment") + "\n\n" + load_prompt("clip_request_resolver")[:60]
)
_INTENT_LINE = re.compile(r'^- intent_id=(\S+) \| op=(\w+) \| attribute="([^"]*)"', re.M)


def _alias_of(prompt: str, media_id: str) -> str:
    shows = next(m["shows"] for m in CASE["media"] if m["media_id"] == media_id)
    [alias] = re.findall(
        rf"^- (m\d{{3}}) \((?:video|image)\): .*{re.escape(shows[:40])}", prompt, re.M
    )
    return alias


class _Provider:
    """The inventory planner and the clip resolver, as the incident turn read them.

    The planner returns ``inventory`` (default: the run's shape, a first pin and no last pin).
    The resolver matches the lineup sheet to its photo, as the run did (the reply named only
    the ending), and, for the ending, ``ending``: ``None`` = nothing (no clip record mentions a
    whistle), else ``(media_id, confidence)``.
    """

    def __init__(
        self,
        inventory: list[dict[str, Any]] | None = None,
        ending: tuple[str, float] | None = None,
    ) -> None:
        self.inventory = INVENTORY if inventory is None else inventory
        self.ending = ending
        self.resolved: list[tuple[str, str, str]] = []  # (intent id, op, attribute) asked

    def invoke(self, *, prompt: str, **_kwargs: Any) -> ModelInvocation:
        if prompt.startswith(_PLANNER_PROMPT):
            return ModelInvocation(
                raw_text=json.dumps({"intents": self.inventory, "question": None})
            )
        if not prompt.startswith(_RESOLVER_PROMPT):
            raise AssertionError(f"unexpected model call: {prompt[:80]!r}")
        out = []
        for intent_id, op, attribute in _INTENT_LINE.findall(prompt):
            self.resolved.append((intent_id, op, attribute))
            assignments = []
            if attribute == "the lineup sheet":
                assignments = [
                    {"media": _alias_of(prompt, LINEUP), "confidence": 0.95, "evidence": "sheet"}
                ]
            elif attribute.casefold() == ENDING and self.ending is not None:
                media_id, confidence = self.ending
                assignments = [
                    {
                        "media": _alias_of(prompt, media_id),
                        "confidence": confidence,
                        "evidence": "x",
                    }
                ]
            out.append({"intent_id": intent_id, "assignments": assignments})
        return ModelInvocation(raw_text=json.dumps({"intents": out}))


def _provider(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> _Provider:
    provider = _Provider(**kwargs)
    monkeypatch.setattr(planning, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "default_client", lambda: provider)
    return provider


# -- one chat turn through the runtime-v2 planner ------------------------------------------


def _agent_input(message: str) -> MainCreatorInput:
    data = copy.deepcopy(CABIN["main_creator_input"])
    data["user_message"] = message
    data["creator_request"] = PROMPT
    data["conversation"] = [data["conversation"][0], {"role": "user", "content": PROMPT}]
    data["capability_manifest"] = resolve_creator_manifest(
        item_id=str(uuid.uuid4()),
        edit_format="montage",
        media=[
            {"media_id": m["media_id"], "kind": "video", "duration_s": m["duration_s"]}
            for m in VIDEOS
        ],
        guided_capability_enabled=True,
    ).model_dump(mode="json")
    return MainCreatorInput.model_validate(data)


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    brief: CreativeBrief | None,
    events: list[tuple[str, dict[str, Any]]] | None = None,
    message: str = PROMPT,
    dated: set[str] | None = None,
) -> PlannedKriaTurn:
    """One planning turn: the Main Creator's draft is parsed; the provider is replayed."""
    agent_input = _agent_input(message)
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": {
                "edit_format": "montage",
                "media_scope": "all",
                "target_duration_s": 45,
                "rationale": SUMMARY,
            },
            "summary": SUMMARY,
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events or []))
    monkeypatch.setattr(planner, "_load_checked_inventory", AsyncMock(return_value=None))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(planner, "_earlier_exclusions", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_load_kept_labels", AsyncMock(return_value=[]))
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    manifest = agent_input.capability_manifest
    return await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(manifest.item_id),
        creator_id=uuid.uuid4(),
        user_message=message,
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=_intent_clips(dated=dated),
            creator_request=None,
            raw_creator_request=PROMPT,
        ),
        output=output,
        # Exactly as `plan_live_turn` derives them from the effective brief.
        wants_capture_order=planner._brief_wants_capture_order(brief),
        effective_brief=brief,
    )


def _answer(
    asked: PlannedKriaTurn, *, media_ids: list[str] | None = None, skipped: bool = False
) -> list[tuple[str, dict[str, Any]]]:
    """The thread after the creator answers the picker: tapped clips, None, or a skip."""
    question = asked.plan.clip_question
    [category] = question["categories"]
    selection: dict[str, Any] = {
        "question_id": question["question_id"],
        "answers": [{"key": category["key"], "media_ids": media_ids}] if media_ids else [],
        "none_keys": [] if media_ids or skipped else [category["key"]],
        "skipped": skipped,
    }
    return [
        ("user", {"turn_id": "kri1067-turn-1"}),
        ("assistant", {"clip_question": question, "turn_id": "kri1067-turn-1-reply"}),
        ("user", {"clip_selection": selection, "turn_id": "kri1067-turn-2"}),
    ]


def _strategy(result: PlannedKriaTurn) -> dict[str, Any]:
    assert result.plan.mode == "act", result.plan.response
    return result.plan.intents[0].arguments["strategy"]


def _backstop(strategy: dict[str, Any], brief: CreativeBrief, events=()):  # noqa: ANN001, ANN202
    """The runtime's draft backstop exactly as `_complete_turn_sync` calls it."""
    pinned = answered_brief(brief, strategy)
    return _unresolved_choice_plan(
        strategy,
        pinned,
        {"clip_assignments": _rows()},
        contract_brief=pinned,
        events=list(events),
        has_draft=False,
    )


def _contract(strategy: dict[str, Any], brief: CreativeBrief):  # noqa: ANN202
    return build_render_contract(
        strategy,
        generation_id="g",
        brief=answered_brief(brief, strategy),
        media_snapshot={"clip_assignments": _rows()},
    )


# -- F1: the incident, and why the backstop alone cannot fix it ---------------------------


def test_the_fixture_reproduces_the_dead_end_the_creator_saw() -> None:
    """The run's draft (the lineup sheet seated first, nothing at last) through today's
    backstop: the contract names the ending, and there is still nothing to tap."""
    lineup = ResolvedClipIntent(
        intent_id="order_first_lineup",
        op="order",
        position="first",
        attribute="the lineup sheet",
        status="resolved",
        assignments=[{"media_id": LINEUP, "confidence": 0.95, "evidence": "sheet"}],
    )
    plan = planner.adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                edit_format="montage",
                direction="guided_story",
                render_program="guided",
                selected_media_ids=FILMED,
                target_duration_s=45,
                rationale=SUMMARY,
            ),
            summary=SUMMARY,
        ),
        server_clip_intents=[
            ClipIntent(**{k: v for k, v in INVENTORY[1].items() if k != "source_quote"})
        ],
        server_resolved_clip_intents=[lineup],
    )
    strategy = plan.intents[0].arguments["strategy"]
    blocked = _backstop(strategy, _brief())
    assert blocked is not None
    reply, _reason = blocked
    assert reply.response.startswith(DEAD_END)
    assert reply.turn_value == "recovery"
    assert reply.clip_question is None and reply.choice_question is None
    assert not reply.reply_options


async def test_f1_f2_the_incident_turn_asks_with_the_picker_and_ticks_the_last_filmed_clip(
    monkeypatch,
) -> None:
    provider = _provider(monkeypatch)

    result = await _turn(monkeypatch, brief=_brief())

    # The stated ending went through the resolver like any clip ask, under the brief's words.
    assert ("order_first_lineup", "order", "the lineup sheet") in provider.resolved
    [added] = [r for r in provider.resolved if r[0] != "order_first_lineup"]
    assert added[1:] == ("order", ENDING)
    # Unmatched, it is the existing tappable picker: never the recovery with nothing to tap.
    plan = result.plan
    assert (plan.mode, plan.turn_value) == ("respond", "question")
    assert DEAD_END not in plan.response
    question = plan.clip_question
    assert question is not None and question["allow_none"] is True
    [category] = question["categories"]
    assert (category["key"], category["op"], category["label"]) == (ENDING_KEY, "order", ENDING)
    assert set(category["candidate_media_ids"]) == set(EVERYTHING)
    # F2: the end of filming order is ticked, and the reply says so.
    assert category["suggested_media_ids"] == [LAST_FILMED]
    assert plan.response.endswith(TICKED_LAST), plan.response
    assert f'I couldn\'t tell which of your clips show "{ENDING}".' in plan.response


async def test_f3_a_stated_first_clip_ticks_the_first_filmed_clip(monkeypatch) -> None:
    _provider(monkeypatch, inventory=[INVENTORY[0]])
    brief = _brief(key="capture_time", first_clip="the team arriving")

    plan = (await _turn(monkeypatch, brief=brief)).plan

    [category] = plan.clip_question["categories"]
    assert (category["key"], category["label"]) == ("order:the-team-arriving", "the team arriving")
    assert category["suggested_media_ids"] == [FIRST_FILMED]
    assert plan.response.endswith('I ticked the first clip you filmed for "the team arriving".')


# -- F4 / F5: every answer to the picker reaches a draft -----------------------------------


async def test_f4_tapping_the_ticked_clip_drafts_with_it_last(monkeypatch) -> None:
    provider = _provider(monkeypatch)
    asked = await _turn(monkeypatch, brief=_brief())
    events = _answer(asked, media_ids=[LAST_FILMED])
    provider.resolved.clear()

    # The planner leaves the ending out again on the answering turn; the tap still lands.
    tapped = await _turn(monkeypatch, brief=_brief(), events=events, message=ANSWER)
    assert all(r[0] == "order_first_lineup" for r in provider.resolved)  # the tap needs no model
    gated = await _gate(monkeypatch, tapped, rows=_rows(), brief=_brief(), events=tuple(events))
    strategy = _strategy(gated)

    [ending] = [
        i
        for i in strategy["resolved_clip_intents"]
        if i["op"] == "order" and i.get("position") == "last"
    ]
    assert [a["media_id"] for a in ending["assignments"]] == [LAST_FILMED]
    assert _backstop(strategy, _brief(), events) is None
    contract = _contract(strategy, _brief())
    assert contract is not None and not contract.unresolved
    assert contract.order_basis == "capture_time"
    assert list(contract.order_ids) == FILMED
    assert contract.order_ids[-1] == LAST_FILMED


async def test_f4b_tapping_another_clip_ends_on_that_clip(monkeypatch) -> None:
    _provider(monkeypatch)
    asked = await _turn(monkeypatch, brief=_brief())
    events = _answer(asked, media_ids=[FULL_TIME])

    tapped = await _turn(monkeypatch, brief=_brief(), events=events, message=ANSWER)
    strategy = _strategy(
        await _gate(monkeypatch, tapped, rows=_rows(), brief=_brief(), events=tuple(events))
    )

    assert _backstop(strategy, _brief(), events) is None
    contract = _contract(strategy, _brief())
    assert contract is not None and not contract.unresolved
    assert contract.order_ids[-1] == FULL_TIME
    assert list(contract.order_ids[:-1]) == [m for m in FILMED if m != FULL_TIME]


@pytest.mark.parametrize(
    ("skipped", "said"),
    [
        (False, f'None of your clips show "{ENDING}"'),
        (True, f'You didn\'t pick a clip for "{ENDING}"'),
    ],
)
async def test_f5_none_or_a_skip_drafts_on_the_last_filmed_clip_and_says_so(
    monkeypatch, skipped, said
) -> None:
    _provider(monkeypatch)
    asked = await _turn(monkeypatch, brief=_brief())
    events = _answer(asked, skipped=skipped)

    answered = await _turn(monkeypatch, brief=_brief(), events=events, message=ANSWER)
    gated = await _gate(monkeypatch, answered, rows=_rows(), brief=_brief(), events=tuple(events))
    strategy = _strategy(gated)

    summary = str(gated.plan.intents[0].arguments["summary"])
    assert (
        f"{said}, so the video ends on the last clip you filmed. Tell me which clip to end on "
        "if you'd like a different one."
    ) in summary
    (answer,) = [a for a in strategy["choice_answers"] if a["kind"] == "pin_absent"]
    assert (answer["option"], answer["value"]) == ("last", ENDING)
    assert _backstop(strategy, _brief(), events) is None
    contract = _contract(strategy, _brief())
    assert contract is not None and not contract.unresolved
    assert list(contract.order_ids) == FILMED


# -- F6-F9: nothing changes where the ending is already handled or not ours --------------


_WHISTLE_ROW = {  # F7: a plain sequence row with the same words
    "intent_id": "order_then_whistle",
    "op": "order",
    "attribute": "the final whistle",
    "source_quote": "to the final whistle",
}


async def test_f6_an_ending_the_planner_already_pinned_is_not_added_again(monkeypatch) -> None:
    row = {  # the planner pinned the spot in other words
        "intent_id": "order_last_end",
        "op": "order",
        "position": "last",
        "attribute": "the end of the match",
        "source_quote": "to the final whistle",
    }
    provider = _provider(monkeypatch, inventory=[*INVENTORY, row])

    await _turn(monkeypatch, brief=_brief())

    assert sorted(r[0] for r in provider.resolved) == sorted(
        ["order_first_lineup", row["intent_id"]]
    )


async def test_f7_a_plain_row_with_the_stated_ending_words_takes_the_last_spot(monkeypatch) -> None:
    """KRI-1123: no second row (it would argue with the planner's), and the planner's own row is
    seated at last, so the contract no longer refuses the ending by name."""
    provider = _provider(
        monkeypatch, inventory=[*INVENTORY, _WHISTLE_ROW], ending=(LAST_FILMED, 0.9)
    )

    result = await _turn(monkeypatch, brief=_brief())

    assert sorted(r[0] for r in provider.resolved) == sorted(
        ["order_first_lineup", "order_then_whistle"]
    )
    strategy = _strategy(result)
    spots = {
        r["intent_id"]: r.get("position")
        for r in strategy["resolved_clip_intents"]
        if r["op"] == "order"
    }
    assert (spots["order_first_lineup"], spots["order_then_whistle"]) == ("first", "last")
    assert list(spots.values()).count("last") == 1  # no second ending row
    contract = _contract(strategy, _brief())
    assert contract is not None and not contract.unresolved
    assert contract.order_ids[-1] == LAST_FILMED


async def test_f7_an_unmatched_plain_row_asks_with_the_picker_and_a_tap_seats_it(
    monkeypatch,
) -> None:
    """The same row when the resolver finds nothing: the picker asks for it (the row's own ask,
    nothing is ticked for a row the planner wrote), and the tap reaches a draft whose contract
    has the ending."""
    _provider(monkeypatch, inventory=[*INVENTORY, _WHISTLE_ROW])
    asked = await _turn(monkeypatch, brief=_brief())
    [category] = asked.plan.clip_question["categories"]
    assert (category["key"], category["label"]) == (ENDING_KEY, ENDING)
    events = _answer(asked, media_ids=[LAST_FILMED])

    tapped = await _turn(monkeypatch, brief=_brief(), events=events, message=ANSWER)
    gated = await _gate(monkeypatch, tapped, rows=_rows(), brief=_brief(), events=tuple(events))
    strategy = _strategy(gated)

    assert _backstop(strategy, _brief(), events) is None
    contract = _contract(strategy, _brief())
    assert contract is not None and not contract.unresolved
    assert contract.order_ids[-1] == LAST_FILMED


@pytest.mark.parametrize(
    "facts",
    [
        {"first_clip": "the lineup sheet", "last_clip": ENDING},  # F8: pin-only (KRI-762)
        {"key": "attachment", "last_clip": ENDING},  # F8: upload order
        {"key": "capture_time"},  # F9: no stated ending
    ],
    ids=["f8_pin_only", "f8_upload_order", "f9_no_ending"],
)
async def test_f8_f9_without_a_filming_order_ending_the_inventory_is_unchanged(
    monkeypatch, facts
) -> None:
    provider = _provider(monkeypatch)

    await _turn(monkeypatch, brief=_brief(**facts))

    assert [r[0] for r in provider.resolved] == ["order_first_lineup"]


async def test_f9_no_brief_is_unchanged(monkeypatch) -> None:
    provider = _provider(monkeypatch)

    result = await _turn(monkeypatch, brief=None)

    assert [r[0] for r in provider.resolved] == ["order_first_lineup"]
    assert result.plan.mode == "act"


# -- F10-F12: the resolver's own answer, and the tick, stay honest -----------------------


async def test_f10_an_ending_the_resolver_matches_needs_no_question(monkeypatch) -> None:
    _provider(monkeypatch, ending=(LAST_FILMED, 0.9))

    result = await _turn(monkeypatch, brief=_brief())

    strategy = result.plan.intents[0].arguments["strategy"]
    assert result.plan.mode == "act" and result.plan.clip_question is None
    contract = _contract(strategy, _brief())
    assert contract is not None and not contract.unresolved
    assert contract.order_ids[-1] == LAST_FILMED


async def test_f11_the_resolvers_near_miss_stays_the_only_tick(monkeypatch) -> None:
    _provider(monkeypatch, ending=(FULL_TIME, 0.5))  # below the bar: a suggestion only

    plan = (await _turn(monkeypatch, brief=_brief())).plan

    [category] = plan.clip_question["categories"]
    assert category["suggested_media_ids"] == [FULL_TIME]
    assert "I ticked" not in plan.response


@pytest.mark.parametrize("dated", [set(), {FULL_TIME}], ids=["none_dated", "one_dated"])
async def test_f12_without_a_filming_order_nothing_is_ticked_or_claimed(monkeypatch, dated) -> None:
    _provider(monkeypatch)

    plan = (await _turn(monkeypatch, brief=_brief(), dated=dated)).plan

    [category] = plan.clip_question["categories"]
    assert category["key"] == ENDING_KEY
    assert category["suggested_media_ids"] == []
    assert "I ticked" not in plan.response


# -- F13 / F14 -------------------------------------------------------------------------------


async def test_f13_a_turkish_chat_hears_about_the_tick_in_turkish(monkeypatch) -> None:
    _provider(monkeypatch)
    with reply_language_for("tr"):
        planned = await plan_and_resolve_clip_intents(
            creator_request=PROMPT,
            latest_user_message=PROMPT,
            candidate_intents=None,
            clips=_intent_clips(),
            run_context=RunContext(),
            brief_anchors=stated_anchors(_brief()),
        )
    assert planned.resolution.question.endswith(
        f'"{ENDING}" için çektiğin son klibi işaretledim.'
    ), planned.resolution.question
    assert "I ticked" not in planned.resolution.question


def test_f14_the_inventory_cap_holds() -> None:
    full = [
        ClipIntent(intent_id=f"include_{n}", op="include", attribute=f"thing {n}")
        for n in range(MAX_CLIP_INTENTS)
    ]
    assert with_brief_anchors(full, {"last": ENDING}) == full
    one_short = full[:-1]
    added = with_brief_anchors(one_short, {"first": "the lineup sheet", "last": ENDING})
    assert len(added) == MAX_CLIP_INTENTS
    assert (added[-1].op, added[-1].position, added[-1].attribute) == (
        "order",
        "first",
        "the lineup sheet",
    )


def test_the_added_ask_has_the_same_identity_every_turn() -> None:
    """A tap is bound to the row's key and id; both come from the brief's words alone."""
    once = with_brief_anchors([], {"last": ENDING})
    again = with_brief_anchors([], {"last": "The final whistle "})
    assert [i.intent_id for i in once] == [i.intent_id for i in again]
    assert [(i.op, i.position, i.source_kind) for i in once] == [("order", "last", None)]
