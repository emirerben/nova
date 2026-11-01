"""Leverkusen M2 run 2: the kids question came back after every answer.

Prod 355a33dae, thread e81aff13 (chat "Taha's", 2026-10-31). The prompt says "The kids at the
start of the second goal clip are another family's children, so don't show them" (PR #1984's
hide_part). After the creator ticked IMG_1039 for the TOR, every turn (24313c44, b0163715,
2d6ecba0) answered with the same ``hidden_part`` question, word for word:

  "I can keep a part out only at the start or the end of a clip. Is that part at the start or
  the end of IMG_1039, and for how many seconds? Or tell me to leave the whole clip out."

even after "At the start, the first 3 seconds". "that part" is the fallback for a missing
``part_attribute``: the question saw no edge, no seconds and no content, although the
planner prompt teaches this exact sentence as ``part_edge: "start"``. The resolver hop
(``resolve_clip_intents_for_turn``) rebuilds each intent field by field and never copied the
three hide_part fields, so whatever the planner wrote, ``hidden_part_question`` asked again.
PR #1984's replay built resolved rows by hand and never ran that hop.

Ways this could go wrong, written down before the fix (each has a case below):

a. The resolver drops ``part_edge`` / ``part_s`` / ``part_attribute`` on a settled hide_part,
   so "at the start, the first 3 seconds" asks "start or end?" again: a question no answer
   can settle (the prod loop).
b. The same drop on an intent the resolver leaves open (a picker row for the kids' clip):
   the creator taps the clip, the part is still empty, and the loop starts after the picker.
c. The same drop on a provider failure: the creator's edge and seconds are lost across a
   retry.
d. The fix lets a number the creator never typed decide the frames: ``part_s`` must still
   come only from the creator's words (PR #1984's fence in the planner parser).
e. The prompt alone (edge, no number) asks the edge question instead of only the seconds,
   in the creator's words ("the kids").
f. A part that covers the whole clip renders instead of asking (PR #1984, unchanged).
g. Other asks change: an order/label/include row now carries a field it never had, so a
   persisted draft re-dumps differently. The three fields are omitted when unset.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agents._runtime import ModelInvocation, RunContext
from app.config import settings
from app.kria.hidden_parts import hidden_part_question
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution as resolution
from app.services.clip_intent_resolution import resolve_clip_intents_for_turn
from tests.kria import test_lev_m2_found_clip_picker as m2

GOAL_1, GOAL_2 = m2.GOAL_1, m2.GOAL_2
KIDS_QUOTE = "The kids at the start of the second goal clip are another family's children"
ANSWER = "At the start, the first 3 seconds"
EDGE_QUESTION = "I can keep a part out only at the start or the end of a clip."
LOOP_REPLY = (
    "I can keep a part out only at the start or the end of a clip. Is that part at the start "
    "or the end of IMG_1039, and for how many seconds? Or tell me to leave the whole clip out."
)


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)


def _hide_kids(part_s: float | None) -> dict[str, Any]:
    """The planner's hide_part for the kids ask, as its prompt teaches this sentence."""
    return {
        "intent_id": "hide_kids_second_goal_clip",
        "op": "hide_part",
        "attribute": "the second goal clip",
        "part_edge": "start",
        "part_s": part_s,
        "part_attribute": "the kids",
        "source_quote": KIDS_QUOTE,
    }


def _planned(part_s: float | None) -> list[dict[str, Any]]:
    """Run 2's planner rows after the picker answer (ids from the turns' ``intent_stats``)."""
    return [
        {"intent_id": "order_selfie", "op": "order", "attribute": "our selfie",
         "source_quote": "Order: our selfie"},
        {"intent_id": "order_scoreboard", "op": "order", "attribute": "the 0:0 scoreboard",
         "source_quote": "the 0:0 scoreboard"},
        {"intent_id": "order_tor_screen", "op": "order", "attribute": m2.TOR,
         "source_quote": m2.TOR},
        {"intent_id": "order_taha_celebrating", "op": "order",
         "attribute": "the moments where Taha is celebrating in both goal clips",
         "source_quote": "then only the moments where Taha is celebrating in both goal clips"},
        _hide_kids(part_s),
    ]  # fmt: skip


def _assign(media_id: str, confidence: float) -> dict[str, Any]:
    return {"media": m2.ALIAS[media_id], "confidence": confidence, "evidence": "record"}


# The creator's picker answer settled the TOR and Taha on IMG_1039; the resolver finds the
# kids' clip at the bar (prod: ``hide_kids_second_goal_clip:n=1:min=0.80``).
RESOLVED = {
    "order_selfie": [_assign(m2.SELFIE, 1.0)],
    "order_scoreboard": [_assign(m2.SCOREBOARD, 1.0)],
    "order_tor_screen": [_assign(GOAL_2, 0.95)],
    "order_taha_celebrating": [_assign(GOAL_1, 0.9), _assign(GOAL_2, 0.9)],
    "hide_kids_second_goal_clip": [_assign(GOAL_2, 0.8)],
}


class _Provider:
    """The model transport only: the real planner and resolver agents render, call, parse."""

    def __init__(self, intents: list[dict[str, Any]], resolved: dict[str, list[dict]]) -> None:
        self.intents = intents
        self.resolved = resolved
        self.calls: list[str] = []

    def invoke(self, *, prompt: str, **_kwargs: Any) -> ModelInvocation:
        if prompt.startswith(m2._PLANNER_PROMPT):
            self.calls.append("planner")
            return ModelInvocation(raw_text=json.dumps({"intents": self.intents, "question": None}))
        if prompt.startswith(m2._RESOLVER_PROMPT):
            self.calls.append("resolver")
            rows = [
                {"intent_id": intent_id, "assignments": assignments, "needs_vision": []}
                for intent_id, assignments in self.resolved.items()
            ]
            return ModelInvocation(raw_text=json.dumps({"intents": rows}))
        raise AssertionError(f"unexpected model call: {prompt[:80]!r}")


def _provider(
    monkeypatch: pytest.MonkeyPatch,
    part_s: float | None,
    resolved: dict[str, list[dict]] | None = None,
) -> _Provider:
    provider = _Provider(_planned(part_s), RESOLVED if resolved is None else resolved)
    monkeypatch.setattr(planning, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "default_client", lambda: provider)
    return provider


async def _turn(monkeypatch: pytest.MonkeyPatch, *later: str) -> Any:
    """One run-2 planning turn: the prompt plus the creator's later messages."""
    message = "\n".join((m2.CASE["creator_request"], *later))
    agent_input = m2._agent_input(message)
    output = m2.MainCreatorAgent(model_client=None).parse(
        json.dumps(
            {
                "action": {
                    "kind": "propose_strategy",
                    "strategy": {
                        "edit_format": "montage",
                        "media_scope": "all",
                        "target_duration_s": 12,
                        "rationale": "A quick reaction edit about Taha.",
                    },
                    "summary": "Here's a 12-second reaction edit about Taha.",
                },
                "brief_updates": [],
            }
        ),
        agent_input,
    )
    monkeypatch.setattr(m2.planner, "_load_thread_events", m2.AsyncMock(return_value=[]))
    monkeypatch.setattr(m2.planner, "_kick_clip_understanding", m2.AsyncMock())
    monkeypatch.setattr(m2.planner, "_earlier_exclusions", m2.AsyncMock(return_value=[]))
    monkeypatch.setattr(m2.planner, "_earlier_hidden_parts", m2.AsyncMock(return_value=[]))
    monkeypatch.setattr(type(m2.planner.settings), "brief_binding_for", lambda _s, _i: True)
    monkeypatch.setattr(m2.planner, "persist_clip_intent_vision_answers", m2.AsyncMock())
    db = m2.SimpleNamespace(
        get=m2.AsyncMock(return_value=m2.SimpleNamespace()),
        commit=m2.AsyncMock(),
        rollback=m2.AsyncMock(),
    )
    manifest = agent_input.capability_manifest
    return await m2.planner._plan_from_creator_output(
        db,
        thread_id=m2.uuid.uuid4(),
        item_id=m2.uuid.UUID(manifest.item_id),
        creator_id=m2.uuid.uuid4(),
        user_message=message,
        manifest=manifest,
        inputs=m2.planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=m2._intent_clips(),
            creator_request=None,
            raw_creator_request=message,
        ),
        output=output,
        brief_selects_clips=m2.planner._brief_selects_clips(None),
        clip_scope_texts=m2.planner._brief_clip_scope_texts(None),
    )


# -- the prod turns, through the planner, the real resolver and the turn (a, e) -------------


async def test_the_first_3_seconds_answer_is_not_asked_again(prod_profile, monkeypatch) -> None:
    # a: turn b0163715 answered "At the start, the first 3 seconds" and got LOOP_REPLY back.
    provider = _provider(monkeypatch, 3)
    planned = await _turn(monkeypatch, ANSWER)
    assert provider.calls == ["planner", "resolver"]
    assert planned.plan.response != LOOP_REPLY
    assert EDGE_QUESTION not in (planned.plan.response or "")
    assert (planned.plan.diagnostics or {}).get("stage") != "hidden_part"
    # The turn drafts, and the draft keeps the kids' 3 seconds of IMG_1039 out.
    assert planned.plan.mode == "act"
    strategy = planned.plan.intents[0].arguments["strategy"]
    (kids,) = [r for r in strategy["resolved_clip_intents"] if r["op"] == "hide_part"]
    assert (kids["part_edge"], kids["part_s"], kids["part_attribute"]) == ("start", 3.0, "the kids")
    assert [a["media_id"] for a in kids["assignments"]] == [GOAL_2]


async def test_the_prompt_alone_asks_only_for_the_seconds(prod_profile, monkeypatch) -> None:
    # e: "the kids at the start of …" gives the edge; only the number is missing.
    _provider(monkeypatch, None)
    planned = await _turn(monkeypatch)
    assert planned.plan.diagnostics["stage"] == "hidden_part"
    assert planned.plan.response == (
        "How many seconds at the start of IMG_1039 show the kids? I'll start that clip right "
        "after them, or tell me to leave the whole clip out."
    )


async def test_a_number_the_creator_never_typed_still_asks(prod_profile, monkeypatch) -> None:
    # d: Flash wrote 2.5 s, the creator wrote no number: the frames are never its guess.
    _provider(monkeypatch, 2.5)
    planned = await _turn(monkeypatch)
    assert planned.plan.response.startswith("How many seconds at the start of IMG_1039")


# -- the resolver hop on its own (a, b, c, f, g) ----------------------------------------------


def _intents(part_s: float | None) -> list[ClipIntent]:
    return [ClipIntent.model_validate(row) for row in _planned(part_s)]


async def _resolve(
    monkeypatch: pytest.MonkeyPatch,
    resolved: dict[str, list[dict]],
    part_s: float | None = 3.0,
    client: Any = None,
) -> Any:
    monkeypatch.setattr(resolution, "default_client", lambda: client or _Provider([], resolved))
    return await resolve_clip_intents_for_turn(
        intents=_intents(part_s),
        creator_request=m2.CASE["creator_request"] + "\n" + ANSWER,
        clips=m2._intent_clips(),
        run_context=RunContext(),
    )


def _kids(resolution_: Any) -> Any:
    (kids,) = [i for i in resolution_.intents if i.op == "hide_part"]
    return kids


async def test_a_settled_part_keeps_its_edge_seconds_and_content(monkeypatch) -> None:
    # a
    kids = _kids(await _resolve(monkeypatch, RESOLVED))
    assert kids.status == "resolved"
    assert [a.media_id for a in kids.assignments] == [GOAL_2]
    assert (kids.part_edge, kids.part_s, kids.part_attribute) == ("start", 3.0, "the kids")
    assert hidden_part_question([kids], m2._intent_clips()) is None


async def test_an_open_part_keeps_them_for_the_picker_answer(monkeypatch) -> None:
    # b: below the bar with an unsure vision check, the row goes to the creator.
    monkeypatch.setattr(resolution, "query_clip_vision", lambda *_a, **_k: m2.ClipQuestionOutput())
    unsure = {**RESOLVED, "hide_kids_second_goal_clip": [_assign(GOAL_2, 0.5)]}
    kids = _kids(await _resolve(monkeypatch, unsure))
    assert kids.status == "needs_creator"
    assert (kids.part_edge, kids.part_s, kids.part_attribute) == ("start", 3.0, "the kids")


async def test_a_provider_failure_keeps_them_for_the_retry(monkeypatch) -> None:
    # c: a resolver that returns no JSON fails the turn's resolution.
    class _Broken:
        def invoke(self, **_kwargs: Any) -> ModelInvocation:
            return ModelInvocation(raw_text="not json")

    kids = _kids(await _resolve(monkeypatch, RESOLVED, client=_Broken()))
    assert (kids.part_edge, kids.part_s, kids.part_attribute) == ("start", 3.0, "the kids")


async def test_a_part_covering_the_whole_clip_still_asks(monkeypatch) -> None:
    # f: IMG_1039 is 18.3 s; hiding 18 s leaves nothing to show.
    kids = _kids(await _resolve(monkeypatch, RESOLVED, part_s=18.0))
    question = hidden_part_question([kids], m2._intent_clips())
    assert question is not None and "leaves nothing of it to show" in question


async def test_other_asks_dump_without_the_part_fields(monkeypatch) -> None:
    # g: persisted rows of every other op stay byte-identical.
    out = await _resolve(monkeypatch, RESOLVED)
    for intent in out.intents:
        if intent.op != "hide_part":
            dumped = intent.model_dump(mode="json")
            assert not {"part_edge", "part_s", "part_attribute"} & dumped.keys(), dumped
