"""KRI-991: the clip pickers pre-ticked nothing, and the two asks came in two turns.

Don Julio lunch M2 (prod af613077f then e98ca6c1e, thread 4C536AFE, 2026-10-27): five analysed
videos + two pool photos. "Skip the clip where he cuts the cheese" and "End on my friend smiling".
Turn 2 asked only the skip (``kria_clip_exclusions_contested asks=1 unmatched=1``): the resolver
matched the order ask, so the exclusion-only branch ran, with no suggestion (suspected=0) and the
friend clip left out of the candidates because the matched order pins it. After the tap, turn 3
re-planned and re-resolved: the order ask came back unmatched, so it asked a second picker with
``suggested_media_ids=[]``. The resolver's raw answers are not stored in prod; the verdicts below
are rebuilt from what IS recorded (the clip records, the picker payloads, the log line, the
token counts) and are stand-ins where noted. Only the model transport is faked.

Ways the fix could fail, written down before the code (each has a case below or is named):

a. A below-bar resolver match for an order/include/group ask is dropped instead of pre-ticked
   (``test_a_below_bar_order_match_is_pre_ticked``).
b. A pre-tick is treated as a choice: the suggested clip plays/leaves with no tap (nothing may
   be applied; only the creator's tap resolves it).
c. A match the asking turn found is lost on the answering turn (the resolver is not
   deterministic and re-runs every turn), so a second picker appears for an ask that already
   had its answer (``test_a_flapping_resolver_does_not_re_ask_the_order_after_the_tap``).
d. The carried match overrides a creator's tap or a later, different ask.
e. A carried match is applied to a clip no longer attached, or to a different ask that
   merely shares words.
f. Photos vanish from the multi-video pickers (KRI-693 thumbnails, KRI-949: a resolver's
   silence is no evidence a photo is not the answer) (``test_photos_stay_picker_candidates``).
g. A pinned clip (a matched first/last ask) becomes an exclusion candidate or pre-tick.
h. A Turkish chat gets English copy.
i. A pre-tick for an exclusion removes footage without a tap (KRI-667).
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents._runtime import ModelInvocation, RunContext
from app.agents._skills import load_skill
from app.agents.clip_question import ClipQuestionAgent
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.pipeline.prompt_loader import load_prompt
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution as resolution
from app.services.clip_intent_resolution import IntentClip, resolve_clip_intents_for_turn
from app.services.creator_capabilities import resolve_creator_manifest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASE: dict[str, Any] = json.loads((FIXTURES / "kri991_donjulio_lunch_m2_clips.json").read_text())
CABIN: dict[str, Any] = json.loads((FIXTURES / "kri623_tigre_m2_montage_turn.json").read_text())
BY_NAME = {c["file_name"]: c["media_id"] for c in CASE["clips"]}
FRIEND = BY_NAME["M2_05"]  # young man smiling, 4.7 s
BOARDS = BY_NAME["M2_04"]  # serving boards, 9 s
STEAK_WAITER = BY_NAME["M2_03"]  # steak waiter looks up, 3 s
STEAK_CUT = BY_NAME["M2_02"]  # steak cutting, 11 s
CHEESE_CUT = BY_NAME["M2_01"]  # provoleta cutting, recorded as "cutting toast", 12 s
PROVOLETA_PHOTO = CASE["photos"][0]["media_id"]
STEAK_PHOTO = CASE["photos"][1]["media_id"]
VIDEOS = [FRIEND, BOARDS, STEAK_WAITER, STEAK_CUT, CHEESE_CUT]  # attachment order in prod
EVERYTHING = [*VIDEOS, PROVOLETA_PHOTO, STEAK_PHOTO]

REQUEST = CASE["creator_request"]
SKIP = {
    "intent_id": "exclude_cheese",
    "op": "exclude",
    "attribute": "the clip where he cuts the cheese",
    "source_quote": "Skip the clip where he cuts the cheese",
}
STEAK = {
    "intent_id": "include_steak_cut",
    "op": "include",
    "attribute": "the steak being cut",
    "source_quote": "I only want the steak being cut",
}
FRIEND_LAST = {
    "intent_id": "order_friend_last",
    "op": "order",
    "position": "last",
    "attribute": "my friend smiling",
    "source_quote": "End on my friend smiling",
}
SKIP_LABEL = "the clip where he cuts the cheese"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)


@pytest.fixture
def no_vision(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]

    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001
        calls[0] += 1
        raise AssertionError("no vision check is part of this flow")

    monkeypatch.setattr(ClipQuestionAgent, "run", _run)
    return calls


def _intent_clips() -> list[IntentClip]:
    clips = [
        IntentClip(
            media_id=c["media_id"],
            kind="video",
            analysis=c["analysis"],
            gcs_path=f"users/u/clips/{c['media_id']}",
            file_name=c["file_name"],
        )
        for c in CASE["clips"]
    ]
    # The photo records were not captured in prod; stand-ins (the steak photo's own words are
    # the evidence the final prod turn recorded for it).
    clips.append(
        IntentClip(
            media_id=PROVOLETA_PHOTO,
            kind="image",
            analysis={"subject": "melted cheese in a small pan", "description": "baked cheese"},
            gcs_path="users/u/visuals/provoleta.jpg",
        )
    )
    clips.append(
        IntentClip(
            media_id=STEAK_PHOTO,
            kind="image",
            analysis={
                "subject": "sliced steak",
                "description": "hands using a knife and fork to slice a grilled steak",
            },
            gcs_path="users/u/visuals/steak.jpg",
        )
    )
    return clips


_PLANNER_PROMPT = load_prompt("clip_intent_planner")[:60]
_RESOLVER_PROMPT = (
    load_skill("meaningful-moment") + "\n\n" + load_prompt("clip_request_resolver")[:60]
)


def _alias_of(prompt: str, record_words: str) -> str:
    [alias] = re.findall(rf"^- (m\d{{3}}) \((?:video|image)\): .*{record_words}", prompt, re.M)
    return alias


def _match(intent_id: str, *pairs: tuple[str, float, str]) -> dict[str, Any]:
    return {
        "intent_id": intent_id,
        "assignments": [
            {"media": alias, "confidence": conf, "evidence": evidence}
            for alias, conf, evidence in pairs
        ],
    }


class _Provider:
    """The model provider as turns 2 and 3 saw it (verdicts reconstructed, see the module doc).

    ``order`` is what the resolver says for "my friend smiling": ``"match"`` (turn 2: the friend
    clip at 0.9, which is why turn 2 asked only the skip), ``"below_bar"`` (0.5) or ``"nothing"``
    (the turn-3 flap). The skip is always empty: ``suspected=0`` in the prod log, and no clip
    record mentions cheese (the provoleta clip is recorded as toasted bread).
    """

    def __init__(self, order: str) -> None:
        self.order = order
        self.calls: list[str] = []

    def invoke(self, *, prompt: str, **_kwargs: Any) -> ModelInvocation:
        if prompt.startswith(_PLANNER_PROMPT):
            self.calls.append("planner")
            return ModelInvocation(
                raw_text=json.dumps({"intents": [STEAK, SKIP, FRIEND_LAST], "question": None})
            )
        if prompt.startswith(_RESOLVER_PROMPT):
            self.calls.append("resolver")
            friend = _alias_of(prompt, "customer smiles and nods")
            steak = _alias_of(prompt, "waiter slices steak on left tray")
            intents: list[dict[str, Any]] = [
                _match("include_steak_cut", (steak, 0.9, "waiter slices steak on the left tray"))
            ]
            if "exclude_cheese" in prompt:
                intents.append({"intent_id": "exclude_cheese", "assignments": []})
            if "order_friend_last" in prompt:
                if self.order == "match":
                    intents.append(
                        _match("order_friend_last", (friend, 0.9, "customer smiles and nods"))
                    )
                elif self.order == "below_bar":
                    intents.append(
                        _match("order_friend_last", (friend, 0.5, "customer smiles and nods"))
                    )
                else:
                    intents.append({"intent_id": "order_friend_last", "assignments": []})
            return ModelInvocation(raw_text=json.dumps({"intents": intents}))
        self.calls.append("unexpected")
        raise AssertionError(f"unexpected model call: {prompt[:80]!r}")


def _provider(monkeypatch: pytest.MonkeyPatch, order: str) -> _Provider:
    provider = _Provider(order)
    monkeypatch.setattr(planning, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "default_client", lambda: provider)
    return provider


def test_the_fixture_is_what_the_resolver_read() -> None:
    # The cheese clip is recorded as toast: no record carries "cheese" for the skip to match.
    text = json.dumps([c["analysis"] for c in CASE["clips"]]).casefold()
    assert "cheese" not in text
    cheese = next(c for c in CASE["clips"] if c["media_id"] == CHEESE_CUT)["analysis"]
    assert "toast" in json.dumps(cheese["understanding"]["actions"]).casefold()
    # The friend clip's record does say the customer smiles.
    friend = next(c for c in CASE["clips"] if c["media_id"] == FRIEND)["analysis"]
    assert "smiles" in json.dumps(friend["understanding"]["notable_moments"])


# -- the resolver's below-bar guess is a suggestion, as the skip's is (a, b) ----------------


async def test_a_below_bar_order_match_is_pre_ticked(monkeypatch, no_vision) -> None:
    # Turn 3 asked "my friend smiling" with suggested=[]: the resolver's low-confidence match
    # for it was dropped (an order ask never re-checks, and only a failed check suggested).
    _provider(monkeypatch, "below_bar")
    result = await resolve_clip_intents_for_turn(
        intents=[ClipIntent(**{k: v for k, v in FRIEND_LAST.items() if k != "source_quote"})],
        creator_request=REQUEST,
        clips=_intent_clips(),
        run_context=RunContext(),
    )
    assert result.status == "needs_creator"
    [category] = result.clip_question["categories"]
    assert category["intent_id"] == "order_friend_last"
    assert category["suggested_media_ids"] == [FRIEND]
    # b: a suggestion is not a choice.
    [intent] = result.intents
    assert intent.assignments == []


# -- the turn, through the real resolution and the runtime-v2 planner -----------------------


def _agent_input() -> MainCreatorInput:
    data = copy.deepcopy(CABIN["main_creator_input"])
    data["user_message"] = REQUEST
    data["creator_request"] = REQUEST
    data["conversation"] = [data["conversation"][0], {"role": "user", "content": REQUEST}]
    data["capability_manifest"] = resolve_creator_manifest(
        item_id=str(uuid.uuid4()),
        edit_format="montage",
        media=[
            {"media_id": c["media_id"], "kind": "video", "duration_s": c["duration_s"]}
            for c in CASE["clips"]
        ],
        guided_capability_enabled=True,
    ).model_dump(mode="json")
    return MainCreatorInput.model_validate(data)


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    events: list | None = None,
    *,
    checked_inventory: Any = None,
    user_message: str = REQUEST,
    raw_request: str = REQUEST,
) -> Any:
    """One planning turn; the Main Creator's answer is parsed, the provider is scripted."""
    agent_input = _agent_input()
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": {
                "edit_format": "montage",
                "media_scope": "all",
                "target_duration_s": 10,
                "rationale": "A quick steak montage.",
            },
            "summary": "Here's a 10-second steak montage.",
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events or []))
    monkeypatch.setattr(
        planner, "_load_checked_inventory", AsyncMock(return_value=checked_inventory)
    )
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(planner, "_earlier_exclusions", AsyncMock(return_value=[]))
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    manifest = agent_input.capability_manifest
    return await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(manifest.item_id),
        creator_id=uuid.uuid4(),
        user_message=user_message,
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=_intent_clips(),
            creator_request=None,
            raw_creator_request=raw_request,
        ),
        output=output,
        brief_selects_clips=planner._brief_selects_clips(None),
        clip_scope_texts=planner._brief_clip_scope_texts(None),
    )


def _answered(planned: Any, *answers: dict[str, Any]) -> tuple[list, str]:
    question = planned.plan.clip_question
    turn_id = str(uuid.uuid4())
    tap = "The clip where he cuts the cheese: clip 5"
    return (
        [
            ("user", {"turn_id": str(uuid.uuid4())}),
            ("assistant", {"clip_question": question, "turn_id": turn_id}),
            (
                "user",
                {
                    "clip_selection": {
                        "question_id": question["question_id"],
                        "answers": [
                            {"key": category["key"], **answer}
                            for category, answer in zip(
                                question["categories"], answers, strict=True
                            )
                        ],
                    }
                },
            ),
        ],
        tap,
    )


async def test_turn_two_asks_only_the_skip_because_the_order_already_matched(
    prod_profile, monkeypatch, no_vision
) -> None:
    # Documents the prod turn 2 (this passes on main): the matched order ask pins the friend
    # clip, so the exclusion-only branch asks the skip alone. Nothing is pre-ticked because
    # the resolver put no clip forward for the skip (suspected=0).
    provider = _provider(monkeypatch, "match")
    planned = await _turn(monkeypatch)
    assert provider.calls == ["planner", "resolver"]
    assert (planned.plan.mode, planned.plan.turn_value) == ("respond", "question")
    [category] = planned.plan.clip_question["categories"]
    assert (category["op"], category["label"]) == ("exclude", SKIP_LABEL)
    assert category["candidate_media_ids"] == [*VIDEOS[1:], PROVOLETA_PHOTO, STEAK_PHOTO]
    assert category["suggested_media_ids"] == []
    assert planned.plan.diagnostics["reason"] == "unmatched"
    # The photos are in the card, by KRI-693/KRI-761/807 design; not #1849's one-video rule.
    assert [m["media_id"] for m in planned.plan.clip_question["candidate_media"]] == [
        PROVOLETA_PHOTO,
        STEAK_PHOTO,
    ]


async def test_photos_stay_picker_candidates(prod_profile, monkeypatch, no_vision) -> None:
    # f: with several videos nothing narrows the photos (KRI-949: silence is no evidence).
    _provider(monkeypatch, "nothing")
    result = await resolve_clip_intents_for_turn(
        intents=[ClipIntent(**{k: v for k, v in FRIEND_LAST.items() if k != "source_quote"})],
        creator_request=REQUEST,
        clips=_intent_clips(),
        run_context=RunContext(),
    )
    [category] = result.clip_question["categories"]
    assert category["candidate_media_ids"] == EVERYTHING


async def test_a_flapping_resolver_does_not_re_ask_the_order_after_the_tap(
    prod_profile, monkeypatch, no_vision
) -> None:
    # c: turn 2 matched "my friend smiling" (0.9) and asked the skip; the tap settles the skip.
    # Turn 3 re-resolves the order ask and the resolver, not deterministic, now matches
    # nothing. The match the asking turn already found must hold: no second picker.
    _provider(monkeypatch, "match")
    asked = await _turn(monkeypatch)
    inventory = asked.plan.clip_intent_inventory
    assert inventory is not None  # the KRI-950 record the tap reuses
    events, tap = _answered(asked, {"media_ids": [CHEESE_CUT]})

    provider = _provider(monkeypatch, "nothing")
    planned = await _turn(
        monkeypatch,
        events,
        checked_inventory=inventory,
        user_message=tap,
        raw_request=f"{REQUEST}\n{tap}",
    )

    assert planned.plan.mode == "act", planned.plan.response
    assert planned.plan.clip_question is None
    resolved = planned.plan.intents[0].arguments["strategy"]["resolved_clip_intents"]
    by_op = {i["op"]: [a["media_id"] for a in i["assignments"]] for i in resolved}
    assert by_op["order"] == [FRIEND]  # carried from the asking turn, not asked again
    assert by_op["exclude"] == [CHEESE_CUT]  # d: the tap stays the tap
    assert "unexpected" not in provider.calls
