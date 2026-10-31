"""KRI-1123 (Leverkusen M4): a story told in a listed order, whose first/last the brief states,
must not dead-end on "I couldn't tell which clip \"the stadium from outside\" means".

Leverkusen-Nürnberg M4, thread eeac2d3b (2026-10-31, Fly 9ea7858ae): "Tell the evening as a
story, in this order: the stadium from outside, the entrance gate, ... and the Nürnberg players
thanking their fans at the end." The Main Creator filed the order as brief r1 ``key:
capture_time`` with ``first_clip`` / ``last_clip`` in those words, and the clip-intent planner
wrote every beat as a plain ``order`` row with no ``position``. The KRI-1067 backstop
(`with_brief_anchors`) left a stated first / last clip alone when an order row already had its
words, so no row was seated at first or last; the render contract compares positions only and
refused with `_pin_not_found`, and the readiness gate copied that one reason onto all nine
requirements ("Please clarify..."): a dead end with nothing to tap, 14:32Z and 14:45Z.

Ways this can fail, written before the code (each is pinned below):

 F1 the incident: the words are on an unpinned order row, the backstop skips them, the contract
    refuses the draft by name.
 F2 the backstop answers by adding a SECOND row for the same words: two rows argue (KRI-764).
 F3 a stated first / last that NO row has still refuses by name (KRI-522); it is never
    silently left in plain filming order.
 F4 promotion moves a pin into the order-basis question (KRI-762).
 F5 the Main Creator rewords the first clip after a correction: no row has the words, so the
    backstop adds a row (a slot: it needs room under the cap).
 F6 one blocked pin is said once, with a next step, not copied onto every requirement.

The turns run the real Main Creator parser, the real runtime-v2 planner
(`_plan_from_creator_output`), the real clip-intent planning, `with_brief_anchors`, render
contract and readiness gate over this run's prompt, brief r1 and media. Only the planner's and
the resolver's model calls are replayed (`kri1123_m4_story_order.json` says what is
reconstructed): no model is called.
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

from app.agents._runtime import ModelInvocation
from app.agents._skills import load_skill
from app.agents.clip_question import ClipQuestionAgent
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.approval_readiness import assess_readiness, readiness_reply
from app.kria.brief import CreativeBrief
from app.kria.reply_language import reply_language_for
from app.pipeline.prompt_loader import load_prompt
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution as resolution
from app.services.choice_questions import answered_brief
from app.services.clip_intent_resolution import IntentClip
from app.services.creator_capabilities import resolve_creator_manifest
from app.services.creator_render_contract import build_render_contract

CASE: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri1123_m4_story_order.json").read_text()
)
CABIN: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "kri623_tigre_m2_montage_turn.json"
    ).read_text()
)
PROMPT = CASE["prompt"]
MEDIA = CASE["media"]
VIDEOS = [m for m in MEDIA if m["kind"] == "video"]
FIRST_WORDS = "the stadium from outside"
LAST_WORDS = "the Nürnberg players thanking their fans at the end"
PIN_MESSAGE = "I couldn't tell which clip \"{}\" means, so I can't confirm the order."
SUMMARY = "A 20-second story of your evening, in the order you listed."


@pytest.fixture(autouse=True)
def _flags(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    monkeypatch.setattr(
        ClipQuestionAgent, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no vision"))
    )


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _brief(*, first: str = FIRST_WORDS) -> CreativeBrief:
    data = copy.deepcopy(CASE["brief"])
    data["requirements"][0]["facts"]["first_clip"] = first
    return CreativeBrief.model_validate(data)


_PLANNER_PROMPT = load_prompt("clip_intent_planner")[:60]
_RESOLVER_PROMPT = (
    load_skill("meaningful-moment") + "\n\n" + load_prompt("clip_request_resolver")[:60]
)
_INTENT_LINE = re.compile(r'^- intent_id=(\S+) \| op=(\w+) \| attribute="([^"]*)"', re.M)


def _alias_of(prompt: str, media_id: str) -> str:
    shows = next(m["shows"] for m in MEDIA if m["media_id"] == media_id)
    found = re.findall(
        rf"^- (m\d{{3}}) \((?:video|image)\): .*{re.escape(shows[:40])}", prompt, re.M
    )
    return found[0] if found else None  # the clip is in another resolver shard


class _Provider:
    def __init__(self, inventory: list[dict[str, Any]]) -> None:
        self.inventory = inventory
        self.resolved: list[tuple[str, str, str]] = []

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
            media_id = CASE["resolutions"].get(intent_id)
            if (
                media_id is None
                and attribute == "the night photo of the whole stadium lit up in red"
            ):
                media_id = CASE["resolutions"]["order_stadium_outside"]
            alias = _alias_of(prompt, media_id) if media_id else None
            assignments = [{"media": alias, "confidence": 1.0, "evidence": "x"}] if alias else []
            out.append({"intent_id": intent_id, "assignments": assignments})
        return ModelInvocation(raw_text=json.dumps({"intents": out}))


def _provider(monkeypatch, inventory=None) -> _Provider:
    provider = _Provider(CASE["inventory"] if inventory is None else inventory)
    monkeypatch.setattr(planning, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "default_client", lambda: provider)
    return provider


def _agent_input() -> MainCreatorInput:
    data = copy.deepcopy(CABIN["main_creator_input"])
    data["user_message"] = PROMPT
    data["creator_request"] = PROMPT
    data["conversation"] = [data["conversation"][0], {"role": "user", "content": PROMPT}]
    data["capability_manifest"] = resolve_creator_manifest(
        item_id=str(uuid.uuid4()),
        edit_format="montage",
        media=[
            {"media_id": m["media_id"], "kind": m["kind"], "duration_s": m["duration_s"]}
            for m in MEDIA
        ],
        guided_capability_enabled=True,
    ).model_dump(mode="json")
    return MainCreatorInput.model_validate(data)


async def _turn(monkeypatch, *, brief):
    agent_input = _agent_input()
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": {
                "edit_format": "montage",
                "media_scope": "all",
                "target_duration_s": 20,
                "rationale": SUMMARY,
            },
            "summary": SUMMARY,
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
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
        user_message=PROMPT,
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=[
                IntentClip(
                    media_id=m["media_id"],
                    kind=m["kind"],
                    analysis={"subject": m["shows"]},
                    gcs_path=f"users/u/{m['media_id']}",
                    capture_time=_time(m["capture_time"]),
                    file_name=m["file_name"],
                    duration_s=m.get("duration_s"),
                )
                for m in MEDIA
            ],
            creator_request=None,
            raw_creator_request=PROMPT,
        ),
        output=output,
        wants_capture_order=planner._brief_wants_capture_order(brief),
        effective_brief=brief,
    )


def _strategy(result) -> dict[str, Any]:
    assert result.plan.mode == "act", result.plan.response
    return result.plan.intents[0].arguments["strategy"]


def _contract(strategy, brief):
    return build_render_contract(
        strategy,
        generation_id="g",
        brief=answered_brief(brief, strategy),
        media_snapshot=CASE["snapshot"],
    )


def _readiness(strategy, brief):
    """The approval gate over a draft, exactly as the runtime builds it (`assess_readiness`)."""
    manifest = resolve_creator_manifest(
        item_id=str(uuid.uuid4()),
        edit_format="montage",
        media=[
            {"media_id": m["media_id"], "kind": m["kind"], "duration_s": m["duration_s"]}
            for m in MEDIA
        ],
    )
    return assess_readiness(brief, strategy, media=CASE["snapshot"], manifest=manifest)


def _spots(strategy) -> dict[str, str | None]:
    return {
        r["intent_id"]: r.get("position")
        for r in strategy["resolved_clip_intents"]
        if r["op"] == "order"
    }


# -- the incident (RED before the fix) --------------------------------------------------------


async def test_m4_listed_story_with_stated_first_and_last_is_seated_and_contract_passes(
    monkeypatch,
) -> None:
    """F1/F2: the brief says first_clip "the stadium from outside" / last_clip "...at the end", the
    planner wrote an unpinned row with those very words for each, and the list is exactly at the
    cap (16, as on main when this was filed; #1999 / KRI-1122 raises it). They are seated
    (promoted) in place: no second row, nothing refused."""
    monkeypatch.setattr(planning, "MAX_CLIP_INTENTS", 16)
    provider = _provider(monkeypatch)
    brief = _brief()

    strategy = _strategy(await _turn(monkeypatch, brief=brief))

    spots = _spots(strategy)
    assert spots["order_stadium_outside"] == "first", spots
    assert spots["order_nurnberg_players_thanking"] == "last", spots
    assert list(spots.values()).count("first") == 1, "a second first row was added"
    assert list(spots.values()).count("last") == 1, "a second last row was added"
    planned = {row["intent_id"] for row in provider.inventory}
    assert set(spots) <= planned, f"rows the planner never wrote: {set(spots) - planned}"
    assert list(_contract(strategy, brief).unresolved) == []


async def test_m4_readiness_passes_once_the_pins_are_seated(monkeypatch) -> None:
    """The gate that turned the one refusal into nine: with the pins seated the story order is
    not a policy problem, so no requirement is blocked by it."""
    monkeypatch.setattr(planning, "MAX_CLIP_INTENTS", 16)
    _provider(monkeypatch)
    brief = _brief()
    strategy = _strategy(await _turn(monkeypatch, brief=brief))

    report = _readiness(strategy, brief)

    assert report["policy_problem"] is None, report["policy_problem"]


# -- F5: neighbouring shape, not the incident ---------------------------------------------------


async def test_m4_main_creator_rewords_the_first_clip_after_a_correction(monkeypatch) -> None:
    """t8: the Main Creator rewrote first_clip into the creator's naming words. No planner row has
    them, so the backstop adds a first row; the planner's last row still has the stated last
    words and is seated in place. The "keep the light show" row is left out of the list so the added
    row has a slot at any cap (#1999 / KRI-1122 raises it)."""
    inventory = [row for row in CASE["inventory"] if row["intent_id"] != "include_light_show_dark"]
    _provider(monkeypatch, inventory=inventory)
    brief = _brief(first=CASE["brief_t8_first"])

    strategy = _strategy(await _turn(monkeypatch, brief=brief))

    spots = _spots(strategy)
    assert list(spots.values()).count("first") == 1, spots
    assert spots["order_nurnberg_players_thanking"] == "last", spots
    assert list(_contract(strategy, brief).unresolved) == []


# -- controls (green before AND after the fix) -----------------------------------------------


async def test_control_a_stated_first_clip_no_row_has_is_still_refused_by_name(monkeypatch) -> None:
    """F3, KRI-522 / KRI-762: words no row has, and no room to add one, still refuse by name
    through the real turn; nothing is silently promoted onto a row with other words."""
    monkeypatch.setattr(planning, "MAX_CLIP_INTENTS", 16)
    _provider(monkeypatch)
    words = "the red smoke before kick-off"
    brief = _brief(first=words)

    strategy = _strategy(await _turn(monkeypatch, brief=brief))

    assert "first" not in _spots(strategy).values(), _spots(strategy)
    assert PIN_MESSAGE.format(words) in _contract(strategy, brief).unresolved


def test_control_unseated_first_pin_is_still_refused_by_name() -> None:
    """KRI-522 / KRI-762 guard: a stated first clip no row seats still refuses, same words."""
    strategy = {
        "edit_format": "montage",
        "media_scope": "all",
        "resolved_clip_intents": [
            {
                "intent_id": "order_stadium_outside",
                "op": "order",
                "status": "resolved",
                "attribute": FIRST_WORDS,
                "assignments": [
                    {
                        "media_id": CASE["resolutions"]["order_stadium_outside"],
                        "confidence": 1.0,
                        "evidence": "x",
                    }
                ],
            }
        ],
    }
    contract = _contract(strategy, _brief())
    assert PIN_MESSAGE.format(FIRST_WORDS) in contract.unresolved


def test_control_t11_pins_seated_by_photos_pass_the_contract_and_readiness() -> None:
    """t11: first/last rows whose only members are Visuals photos (not video rows) are placed."""
    rows = []
    for intent_id, media_id in CASE["resolutions"].items():
        intent = next(i for i in CASE["inventory"] if i["intent_id"] == intent_id)
        row = {
            "intent_id": intent_id,
            "op": intent["op"],
            "status": "resolved",
            "attribute": intent["attribute"],
            "assignments": [{"media_id": media_id, "confidence": 1.0, "evidence": "x"}],
        }
        if intent.get("creator_text"):
            row["creator_text"] = intent["creator_text"]
            row["assignments"][0].update(value=intent["creator_text"], grounding="creator_text")
        rows.append(row)
    rows[0]["position"] = "first"
    next(r for r in rows if r["intent_id"] == "order_nurnberg_players_thanking")["position"] = (
        "last"
    )
    strategy = {"edit_format": "montage", "media_scope": "all", "resolved_clip_intents": rows}
    assert list(_contract(strategy, _brief()).unresolved) == []


@pytest.mark.parametrize(
    ("language", "refusal", "next_step"),
    [
        (
            None,
            PIN_MESSAGE,
            "Tell me which clip you mean, and I'll make a new draft.",
        ),
        (
            "tr",
            '"{}" derken hangi klibi kastettiğini anlayamadım, o yüzden sırayı doğrulayamıyorum.',
            "Hangi klibi kastettiğini söyle, yeni bir taslak hazırlayayım.",
        ),
    ],
    ids=["en", "tr"],
)
async def test_f6_a_blocked_pin_is_said_once_with_a_next_step_not_on_every_requirement(
    monkeypatch, language, refusal, next_step
) -> None:
    """The refusal is the whole draft's, not each requirement's: M4 14:32Z showed the same
    sentence under "Make it 20 seconds", "Skip the dark photo" and every label. Said once, with
    what to do. (Words no row has, no room to add one: the refusal this fix leaves in place.)"""
    monkeypatch.setattr(planning, "MAX_CLIP_INTENTS", 16)
    _provider(monkeypatch)
    words = "the red smoke before kick-off"
    brief = _brief(first=words)
    strategy = _strategy(await _turn(monkeypatch, brief=brief))

    with reply_language_for(language):
        report = _readiness(strategy, brief)
        reply = readiness_reply(brief, report)

    assert report["policy_problem"] == refusal.format(words), report["policy_problem"]
    assert not report["ready"]
    assert reply.count(refusal.format(words)) == 1, reply
    assert next_step in reply, reply
    held = [line for line in reply.splitlines() if line.startswith("- ")]
    assert "- Make it 20 seconds" in held and len(held) == 9, held  # still named, once each
    assert not any(words in line for line in held), held
