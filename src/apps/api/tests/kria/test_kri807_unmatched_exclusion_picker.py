"""KRI-807: "Skip the blurry shot of the empty table" matched no clip, and nothing asked.

Prod thread F4083F1D (Don Julio M1, 2026-10-21, turn bce4a799). The resolver stats were
``exclude_blurry_table:n=0:...:nv=1``: no confident match, and ONE clip put forward for a
vision check. An exclude never pays for a vision check and never asks (KRI-667), so the
suspect was thrown away, nothing was left out, and the held draft said "I couldn't tell
which clips you asked me to leave out ... Tell me which ones": free text, no picker. The
creator had to type a clip number from memory.

Ways this could go wrong, written down before the code (each has a case below):

a. An exclude that left nothing out still ends in the free-text "Tell me which ones"
   notice instead of the clip picker.
b. The resolver's suspect (a below-bar match, or the clip it wanted a vision check for) is
   lost, so the picker pre-ticks nothing although the resolver had a guess.
c. A suspect is LEFT OUT without the creator's tap: footage removed on a guess (KRI-667).
d. A vision check is paid for to settle the exclude.
e. A clip another ask pins (first/last, a creator label, media the strategy places) is
   offered or pre-ticked.
f. The question loops: after a tap, a typed file name or "none of these", the same skip
   is asked again.
g. An earlier draft's match (KRI-761 carry-forward) is asked about instead of applied.
h. A confident, clean exclude now asks (regression).
i. An unmatched skip and a look-alike skip, or a skip and the resolver's own clip question
   (the red-cap picker on bce4a799), are asked in two turns instead of one picker.
j. A one-clip project is asked which of its clips to leave out (an empty edit).
k. A skip the planner dropped (no intent for a picker answer to bind to) goes silent:
   the notice must stay for it.
l. Flags: the picker flag off keeps the notice; exclusions off ask nothing.
m. A Turkish chat gets English copy.
n. The suspects leak into the persisted draft (the strategy's resolved intents).
o. The tap does not reach the next turn: the chosen clip still plays.
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
from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._skills import load_skill
from app.agents.clip_question import ClipQuestionAgent
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.clip_exclusions import exclusion_question, exclusions_to_ask
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.pipeline.prompt_loader import load_prompt
from app.schemas.clip_intents import ClipAssignment, ResolvedClipIntent
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution as resolution
from app.services.clip_intent_resolution import IntentClip, resolve_clip_intents_for_turn
from app.services.creator_capabilities import resolve_creator_manifest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASE: dict[str, Any] = json.loads((FIXTURES / "kri807_don_julio_m1_clips.json").read_text())
# The KRI-623 cabin turn supplies the rest of a real Main Creator input (conversation shape,
# language, copy state); only its clips are swapped for the Don Julio ones.
CABIN: dict[str, Any] = json.loads((FIXTURES / "kri623_tigre_m2_montage_turn.json").read_text())
BY_NAME = {c["file_name"].removesuffix("_footage"): c["media_id"] for c in CASE["clips"]}
ALL = [c["media_id"] for c in CASE["clips"]]  # attachment order: M1_09 first
BLURRY = BY_NAME["M1_04"]  # the fast blurry pan over plates and a knife
SHARP_LOOK_ALIKE = BY_NAME["M1_06"]  # the sharp overhead bread/salad shot
SKIP = "Skip the blurry shot of the empty table."
ATTRIBUTE = "the blurry shot of the empty table"
INTENT_ID = "exclude_blurry_table"
NOTICE = "I couldn't tell which clips you asked me to leave out"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)


@pytest.fixture
def no_vision(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    # d: an exclude never pays for a vision check, whatever the resolver asked for.
    calls = [0]

    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001
        calls[0] += 1
        raise AssertionError("an exclusion never pays for a vision check")

    monkeypatch.setattr(ClipQuestionAgent, "run", _run)
    return calls


def test_the_fixture_is_the_prod_turn() -> None:
    # The resolver shape this file reproduces: no match, one clip sent for a vision check.
    assert "exclude_blurry_table:n=0:min=0.00:max=0.00:nv=1:q=0" in CASE["resolver_intent_stats"]
    assert SKIP in CASE["creator_request"]
    record = next(c["record"] for c in CASE["clips"] if c["media_id"] == BLURRY)
    assert "blur" not in json.dumps(record).casefold()  # the record never says so


# -- which excludes ask (a, b, e, h, i, j) -----------------------------------------------


def _montage(**update: Any) -> CreativeStrategy:
    return CreativeStrategy.model_validate({"edit_format": "montage", **update})


def _exclude(*matches: tuple[str, float], intent_id: str = INTENT_ID, **kw: Any):
    return ResolvedClipIntent(
        intent_id=intent_id,
        op="exclude",
        attribute=kw.pop("attribute", ATTRIBUTE),
        status="resolved",
        assignments=[ClipAssignment(media_id=m, confidence=c, **kw) for m, c in matches],
    )


def _first(media_id: str) -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id="order_waiter_first",
        op="order",
        position="first",
        attribute="the waiter smiling at the camera",
        status="resolved",
        assignments=[ClipAssignment(media_id=media_id, confidence=0.9)],
    )


def _clips(*ids: str) -> list[IntentClip]:
    return [IntentClip(media_id=m, kind="video", analysis=None) for m in ids]


def test_a_skip_that_matched_nothing_asks_with_its_suspect_pre_ticked() -> None:
    # a, b, e: the waiter is pinned first; the blurry pan is the resolver's only suspect.
    waiter = BY_NAME["M1_05"]
    asked = exclusions_to_ask(
        [_first(waiter), _exclude()],
        _montage(),
        suspects={INTENT_ID: [BLURRY]},
        clip_count=len(ALL),
    )
    text, question = exclusion_question(asked, _clips(*ALL))
    [category] = question["categories"]
    assert (category["op"], category["intent_id"]) == ("exclude", INTENT_ID)
    assert category["candidate_media_ids"] == [m for m in ALL if m != waiter]
    assert category["suggested_media_ids"] == [BLURRY]
    assert question["allow_none"] is True
    assert ATTRIBUTE in text and "leave out" in text and "Tell me which ones" not in text


def test_with_no_suspect_every_unpinned_clip_is_offered_and_nothing_is_ticked() -> None:
    asked = exclusions_to_ask([_exclude()], _montage(), suspects={}, clip_count=len(ALL))
    _text, question = exclusion_question(asked, _clips(*ALL))
    [category] = question["categories"]
    assert category["candidate_media_ids"] == ALL
    assert category["suggested_media_ids"] == []


def test_a_pinned_suspect_is_never_offered_or_ticked() -> None:
    # e
    asked = exclusions_to_ask(
        [_first(BLURRY), _exclude()],
        _montage(),
        suspects={INTENT_ID: [BLURRY]},
        clip_count=len(ALL),
    )
    _text, question = exclusion_question(asked, _clips(*ALL))
    [category] = question["categories"]
    assert BLURRY not in category["candidate_media_ids"]
    assert category["suggested_media_ids"] == []


@pytest.mark.parametrize("evidence", ["creator selected", "file name", ""])
def test_a_settled_skip_is_never_asked(evidence: str) -> None:
    # f, h: a tap, a typed file name, or a confident match leaves the clip out unasked.
    intents = [_exclude((BLURRY, 0.95 if not evidence else 1.0), evidence=evidence)]
    assert exclusions_to_ask(intents, _montage(), suspects={}, clip_count=len(ALL)) == []


def test_an_unmatched_and_a_look_alike_skip_share_one_picker() -> None:
    # i
    look_alike = _exclude(
        (BLURRY, 0.8), (SHARP_LOOK_ALIKE, 0.8), intent_id="skip_table", attribute="the table"
    )
    asked = exclusions_to_ask(
        [look_alike, _exclude()], _montage(), suspects={}, clip_count=len(ALL)
    )
    text, question = exclusion_question(asked, _clips(*ALL))
    assert [c["intent_id"] for c in question["categories"]] == ["skip_table", INTENT_ID]
    assert question["categories"][0]["suggested_media_ids"] == [BLURRY, SHARP_LOOK_ALIKE]
    assert "the table" in text and ATTRIBUTE in text


def test_a_one_clip_project_is_never_asked_which_clip_to_leave_out() -> None:
    # j
    assert exclusions_to_ask([_exclude()], _montage(), suspects={}, clip_count=1) == []


def test_the_question_is_turkish_in_a_turkish_chat() -> None:
    # m
    asked = exclusions_to_ask([_exclude()], _montage(), suspects={}, clip_count=len(ALL))
    token = bind_reply_language("tr")
    try:
        text, _question = exclusion_question(asked, _clips(*ALL))
    finally:
        release_reply_language(token)
    assert "çıkar" in text


# -- the resolver keeps its suspect, removes nothing, pays nothing (b, c, d, n) ------------


def _intent_clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=c["media_id"],
            kind="video",
            analysis={"understanding": c["record"]},
            gcs_path=f"users/u/clips/{c['media_id']}",
            file_name=c["file_name"],
        )
        for c in CASE["clips"]
    ]


RED_CAP_LAST = {
    "intent_id": "order_friend_last",
    "op": "order",
    "position": "last",
    "attribute": "my friend in the red cap smiling at the camera",
    "source_quote": "End on my friend in the red cap smiling at the camera",
}
_PLANNER_PROMPT = load_prompt("clip_intent_planner")[:60]
_RESOLVER_PROMPT = (
    load_skill("meaningful-moment") + "\n\n" + load_prompt("clip_request_resolver")[:60]
)


class _Provider:
    """The model provider, answering as it did on turn bce4a799.

    Only the transport is a stand-in: the real clip-intent planner and resolver agents
    render their prompts, call it, and parse its raw text.
    """

    def __init__(self, *, red_cap: bool = False, below_bar: bool = False) -> None:
        self.red_cap, self.below_bar = red_cap, below_bar
        self.calls: list[str] = []

    def invoke(self, *, prompt: str, **_kwargs: Any) -> ModelInvocation:
        if prompt.startswith(_PLANNER_PROMPT):
            self.calls.append("planner")
            skip = {
                "intent_id": INTENT_ID,
                "op": "exclude",
                "attribute": ATTRIBUTE,
                "source_quote": SKIP.rstrip("."),
            }
            intents = [RED_CAP_LAST, skip] if self.red_cap else [skip]
            return ModelInvocation(raw_text=json.dumps({"intents": intents, "question": None}))
        if prompt.startswith(_RESOLVER_PROMPT):
            self.calls.append("resolver")
            # The table pan, by its alias in the prompt (the record never says "blurry").
            [alias] = re.findall(r"^- (m\d{3}) \(video\): .*pans across", prompt, re.M)
            skip: dict[str, Any] = {"intent_id": INTENT_ID, "assignments": [], "needs_vision": []}
            if self.below_bar:
                skip["assignments"] = [{"media": alias, "confidence": 0.6, "evidence": "table"}]
            else:
                skip["needs_vision"] = [{"media": alias, "question": "Is this shot blurry?"}]
            intents = [skip]
            if "order_friend_last" in prompt:
                # No clip record mentions the red cap: only the creator can say.
                intents.append({"intent_id": "order_friend_last", "assignments": []})
            return ModelInvocation(raw_text=json.dumps({"intents": intents}))
        self.calls.append("unexpected")
        raise AssertionError(f"unexpected model call: {prompt[:80]!r}")


def _provider(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> _Provider:
    provider = _Provider(**kwargs)
    monkeypatch.setattr(planning, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "default_client", lambda: provider)
    return provider


@pytest.mark.parametrize("below_bar", [False, True], ids=["vision-check", "below-the-bar"])
async def test_the_resolver_keeps_its_suspect_and_removes_nothing(
    monkeypatch, no_vision, below_bar: bool
) -> None:
    provider = _provider(monkeypatch, below_bar=below_bar)
    result = await resolve_clip_intents_for_turn(
        intents=[_exclude()],
        creator_request=CASE["creator_request"],
        clips=_intent_clips(),
        run_context=RunContext(),
    )
    assert provider.calls == ["resolver"]
    assert result.status == "resolved" and result.clip_question is None
    [intent] = result.intents
    assert (intent.op, intent.status, intent.assignments) == ("exclude", "resolved", [])  # c
    assert result.exclusion_suspects == {INTENT_ID: [BLURRY]}  # b
    assert no_vision == [0]  # d
    assert "suspect" not in json.dumps(intent.model_dump(mode="json"))  # n


# -- the turn, through the real resolution and the runtime-v2 planner (a, f, i, k, l, o) --


def _agent_input() -> MainCreatorInput:
    data = copy.deepcopy(CABIN["main_creator_input"])
    message = CASE["creator_request"]
    data["user_message"] = message
    data["creator_request"] = message
    data["conversation"] = [data["conversation"][0], {"role": "user", "content": message}]
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


async def _turn(monkeypatch: pytest.MonkeyPatch, events: list | None = None) -> Any:
    """One planning turn; the Main Creator's answer is parsed, the provider is scripted."""
    agent_input = _agent_input()
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": {
                "edit_format": "montage",
                "media_scope": "all",
                "target_duration_s": 15,
                "rationale": "A quick lunch montage.",
            },
            "summary": "Here's a 15-second montage of your lunch.",
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events or []))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(planner, "_earlier_exclusions", AsyncMock(return_value=[]))
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    manifest = agent_input.capability_manifest
    return await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(manifest.item_id),
        creator_id=uuid.uuid4(),
        user_message=CASE["creator_request"],
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=_intent_clips(),
            creator_request=None,
            raw_creator_request=CASE["creator_request"],
        ),
        output=output,
        brief_selects_clips=planner._brief_selects_clips(None),
        clip_scope_texts=planner._brief_clip_scope_texts(None),
    )


def _played(planned: Any) -> set[str]:
    strategy = planned.plan.intents[0].arguments["strategy"]
    selected = strategy.get("selected_media_ids") or []
    return set(ALL) if strategy.get("media_scope") == "all" or not selected else set(selected)


async def test_the_don_julio_skip_asks_with_the_picker_not_free_text(
    prod_profile, monkeypatch, no_vision
) -> None:
    # a, b, d: the prod turn. The creator taps instead of typing a clip number from memory.
    provider = _provider(monkeypatch)
    planned = await _turn(monkeypatch)
    assert provider.calls == ["planner", "resolver"]
    assert (planned.plan.mode, planned.plan.turn_value) == ("respond", "question")
    assert planned.plan.intents == []
    [category] = planned.plan.clip_question["categories"]
    assert (category["op"], category["intent_id"]) == ("exclude", INTENT_ID)
    assert category["candidate_media_ids"] == ALL
    assert category["suggested_media_ids"] == [BLURRY]
    assert "leave out" in planned.plan.response
    assert NOTICE not in planned.plan.response and "Tell me which ones" not in planned.plan.response
    assert planned.plan.diagnostics["reason"] == "unmatched"
    assert no_vision == [0]


def _answered(planned: Any, *answers: dict[str, Any]) -> list:
    question = planned.plan.clip_question
    categories = question["categories"]
    assert len(categories) == len(answers)
    return [
        ("assistant", {"clip_question": question}),
        (
            "user",
            {
                "clip_selection": {
                    "question_id": question["question_id"],
                    "answers": [
                        {"key": category["key"], **answer}
                        for category, answer in zip(categories, answers, strict=True)
                    ],
                }
            },
        ),
    ]


async def test_the_tapped_clip_leaves_the_edit_and_nothing_is_asked_again(
    prod_profile, monkeypatch, no_vision
) -> None:
    # f, o: ask -> tap the blurry pan -> the next turn drafts without it, no second ask.
    _provider(monkeypatch)
    asked = await _turn(monkeypatch)
    provider = _provider(monkeypatch)
    events = _answered(asked, {"media_ids": [BLURRY]})
    planned = await _turn(monkeypatch, events)
    assert planned.plan.mode == "act", planned.plan.response
    played = _played(planned)
    assert BLURRY not in played and SHARP_LOOK_ALIKE in played
    assert provider.calls == ["planner"]  # the tap settled it: the resolver never ran
    assert not [n for n in planned.policy_notices if NOTICE in n]
    resolved = planned.plan.intents[0].arguments["strategy"]["resolved_clip_intents"]
    assert [(i["op"], [a["media_id"] for a in i["assignments"]]) for i in resolved] == [
        ("exclude", [BLURRY])
    ]
    assert "suspect" not in json.dumps(resolved)  # n


async def test_none_of_these_keeps_every_clip_and_never_asks_again(
    prod_profile, monkeypatch, no_vision
) -> None:
    # f
    _provider(monkeypatch)
    asked = await _turn(monkeypatch)
    events = _answered(asked, {"none": True})
    planned = await _turn(monkeypatch, events)
    assert planned.plan.mode == "act", planned.plan.response
    assert _played(planned) == set(ALL)
    assert not [n for n in planned.policy_notices if NOTICE in n]


async def test_the_picker_off_keeps_the_notice(prod_profile, monkeypatch, no_vision) -> None:
    # l (and k: a skip with nothing to bind a picker to keeps the notice, see
    # test_kri667_clip_exclusions::test_kri680_a_skip_the_planner_dropped_is_said_...).
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", False)
    _provider(monkeypatch)
    planned = await _turn(monkeypatch)
    assert planned.plan.mode == "act"
    assert _played(planned) == set(ALL)
    assert [n for n in planned.policy_notices if NOTICE in n]


async def test_exclusions_off_ask_nothing_and_remove_nothing(
    prod_profile, monkeypatch, no_vision
) -> None:
    # l
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", False)
    _provider(monkeypatch)
    planned = await _turn(monkeypatch)
    assert planned.plan.mode == "act"
    assert planned.plan.clip_question is None
    assert _played(planned) == set(ALL)


async def test_the_skip_rides_in_the_red_cap_picker_and_one_answer_settles_both(
    prod_profile, monkeypatch, no_vision
) -> None:
    # i: turn bce4a799 asked the red-cap picker alone; the skip would have come a turn later.
    _provider(monkeypatch, red_cap=True)
    asked = await _turn(monkeypatch)
    assert (asked.plan.mode, asked.plan.turn_value) == ("respond", "question")
    question = asked.plan.clip_question
    assert [(c["op"], c["intent_id"]) for c in question["categories"]] == [
        ("order", "order_friend_last"),
        ("exclude", INTENT_ID),
    ]
    assert question["categories"][1]["suggested_media_ids"] == [BLURRY]
    response = asked.plan.response
    assert "red cap" in response and ATTRIBUTE in response and "leave out" in response
    assert NOTICE not in response

    red_cap = BY_NAME["M1_02"]
    events = _answered(asked, {"media_ids": [red_cap]}, {"media_ids": [BLURRY]})
    provider = _provider(monkeypatch, red_cap=True)
    planned = await _turn(monkeypatch, events)
    assert planned.plan.mode == "act", planned.plan.response
    assert provider.calls == ["planner"]  # both taps settled their asks: no resolver
    assert BLURRY not in _played(planned) and red_cap in _played(planned)
    resolved = planned.plan.intents[0].arguments["strategy"]["resolved_clip_intents"]
    assert {(i["op"], tuple(a["media_id"] for a in i["assignments"])) for i in resolved} == {
        ("order", (red_cap,)),
        ("exclude", (BLURRY,)),
    }
    assert not [n for n in planned.policy_notices if NOTICE in n]


async def test_with_exclusions_off_the_red_cap_picker_is_unchanged(
    prod_profile, monkeypatch, no_vision
) -> None:
    # l
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", False)
    _provider(monkeypatch, red_cap=True)
    asked = await _turn(monkeypatch)
    assert [c["op"] for c in asked.plan.clip_question["categories"]] == ["order"]
    assert "leave out" not in asked.plan.response
