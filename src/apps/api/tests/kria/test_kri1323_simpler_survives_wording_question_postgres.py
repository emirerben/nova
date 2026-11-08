"""KRI-1323: "Make it without these" survives a wording question asked before the draft.

Qendresa Q2 run 2 (prod thread 9db44dc2, 2026-11-08): 13:47 the first draft was held (card
4e569a98 left out r2). 13:51 she tapped "Make it without these"; that turn asked a
``creative_copy_wording`` question for the ending instead of drafting (brief_version None).
13:53 she tapped "Use this wording"; 13:55 the SAME hold came back with a new card (796cdd17),
and only her third tap (13:56) wrote brief v2. The consent was read only from the current turn's
own message, which now answered the wording question, so the earlier tap was lost.

Ways this can fail, written before the fix:
 F1 the incident: an answer to an intervening question costs the earlier simpler-version tap.
 F2 over-reach: a NEW typed instruction after the tap (not an answer to a question Kria asked)
    still rides on the old tap.
 F3 over-reach: a tap on an older card than the latest hold consents to that later hold, or a
    tap that a later answer on the same card ("Change my request") or a draft replaced still counts.
 F4 the second tap is still needed (the same card comes back).

Runs the real runtime over scratch Postgres with the KRI-1236 harness (planner output and
readiness rows replayed from Qendresa Q1, which held r4 the same way).
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import app.tasks.kria_runtime as kria_runtime
from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.database import AsyncSessionLocal, sync_session
from app.kria import approval_readiness
from app.kria.api_schemas import ChoiceSelectionIn, SubmitTurnBody
from app.kria.brief import BriefUpdate
from app.kria.brief_binding import snapshot_media
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.kria.runtime import submit_turn
from app.models import CreationThread, PlanItem
from app.services.creative_copy_decisions import media_digest, question_message, wording_question
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_kri1236_simpler_leaves_out_only_unsupported_postgres import (
    BRIEF_V1,
    FIXTURE,
    NOT_READY,
    PROMPT,
    TAP,
    _brief,
    _dispose_async_engine,  # noqa: F401 - autouse fixture
    _flags,  # noqa: F401 - autouse fixture
    _holds,
    _last_reply,
    _prompt_updates,
    _replay_readiness,
    _revision,
    _seed,
)

ENDING = "comment REPO"


def _strategy_plan():  # noqa: ANN202
    strategy = {
        **copy.deepcopy(FIXTURE["approved_strategy"]),
        "selected_media_ids": [],
        "closing_title": ENDING,
    }
    return adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy.model_validate(strategy),
            summary="Your talk with the opening title, captions and the ending.",
        )
    )


def _draft(*updates) -> PlannedKriaTurn:  # noqa: ANN002
    return PlannedKriaTurn(
        plan=_strategy_plan(),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        brief_updates=tuple(updates),
        brief_route="replan",
        brief_clip_ids=("clip-1",),
    )


def _wording(thread_id: uuid.UUID) -> PlannedKriaTurn:
    """What the tap turn answered in prod: the ending-wording question, no draft."""
    with sync_session() as db:
        item = db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id)
        digest = media_digest(snapshot_media(item))
    question = wording_question(target="closing_title", candidate=ENDING, dependency_digest=digest)
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=question_message(question, f"I’d try “{ENDING}”. Does this wording work?"),
            choice_question=question,
        ),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
    )


async def _send(
    monkeypatch: pytest.MonkeyPatch,
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    message: str,
    planned: PlannedKriaTurn,
    *,
    answer: tuple[str, str] | None = None,
):  # noqa: ANN202
    if planned.plan.intents:
        async with AsyncSessionLocal() as db:
            thread = await db.get(CreationThread, thread_id)
            item = await db.get(PlanItem, thread.active_plan_item_id)
            manifest = await approval_readiness.resolve_readiness_manifest(
                db, item, planned.plan.intents[0].arguments["strategy"]
            )
        planned = dataclasses.replace(planned, readiness_manifest=manifest)

    async def _plan(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return planned

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _plan)
    selection = (
        {"choice_selection": ChoiceSelectionIn(question_id=answer[0], option_key=answer[1])}
        if answer
        else {}
    )
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"kri1323-{uuid.uuid4().hex}",
                expected_thread_revision=_revision(thread_id),
                **selection,
            ),
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] in {"completed", "awaiting_approval"}, result
    return _last_reply(thread_id)


async def _held_then_tapped_then_asked(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN202
    _replay_readiness(monkeypatch, promises_approvable=True)
    user_id, thread_id = _seed()
    held = await _send(monkeypatch, user_id, thread_id, PROMPT, _draft(*_prompt_updates()))
    card = held.payload["choice_question"]
    assert card["omitted_requirement_ids"] == ["r4"]
    asked = await _send(
        monkeypatch,
        user_id,
        thread_id,
        TAP,
        _wording(thread_id),
        answer=(card["question_id"], "simpler_version"),
    )
    wording = asked.payload["choice_question"]
    assert wording["kind"] == "creative_copy_wording"
    assert _brief(thread_id).version == 1
    return user_id, thread_id, wording


@pytest.mark.asyncio
async def test_f1_the_tap_survives_the_wording_answer(monkeypatch) -> None:
    user_id, thread_id, wording = await _held_then_tapped_then_asked(monkeypatch)
    reply = await _send(
        monkeypatch,
        user_id,
        thread_id,
        "Use this wording",
        _draft(),
        answer=(wording["question_id"], "approve"),
    )
    # F4: no second card for the same omission; the simpler draft is made.
    assert reply.event_type == "draft_applied", reply.content
    assert _holds(thread_id) == 1
    brief = _brief(thread_id)
    assert brief.version == 2
    assert "r4" not in {req.id for req in brief.live()}


@pytest.mark.asyncio
async def test_f2_a_new_instruction_after_the_tap_is_not_covered_by_it(monkeypatch) -> None:
    """A message that adds an ask (a brief update) needs its own offer, tap or no tap."""
    user_id, thread_id, _wording_card = await _held_then_tapped_then_asked(monkeypatch)
    bigger = BriefUpdate(kind="style", scope="global", description="Make the ending text bigger")
    reply = await _send(
        monkeypatch, user_id, thread_id, "Actually make the ending text bigger", _draft(bigger)
    )
    assert reply.event_type == "assistant_response", reply.content
    assert "r4" in {req.id for req in _brief(thread_id).live()}


class _Rows:
    def __init__(self, rows: list[tuple[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, Any]]:
        return self._rows


class _Db:
    def __init__(self, rows: list[tuple[str, Any]]) -> None:
        self._rows = rows

    def execute(self, _statement: object) -> _Rows:
        return _Rows(self._rows)


def _hold(question_id: str) -> dict[str, Any]:
    question = {
        "question_id": question_id,
        "conflict": "simplification_requires_choice",
        "brief_version": BRIEF_V1.version,
        "omitted_requirement_ids": ["r4"],
        "options": [{"key": "simpler_version", "label": TAP}],
    }
    return {"brief_coverage": {"reason": NOT_READY}, "choice_question": question}


def _tap(question_id: str, option: str = "simpler_version") -> dict[str, Any]:
    return {"choice_selection": {"question_id": question_id, "option_key": option}}


def _consent(rows: list[tuple[str, Any]], source: dict[str, Any]) -> str | None:
    return kria_runtime._simpler_version_consent(
        _Db(rows),
        uuid.uuid4(),
        ["r4"],
        brief=BRIEF_V1,
        source_event=SimpleNamespace(payload=source, content="Use this wording"),
    )


WORDING_ANSWER = _tap("wording-q", "approve")
WORDING_Q = {"choice_question": {"question_id": "wording-q", "kind": "creative_copy_wording"}}


def test_f1_unit_an_earlier_tap_on_the_latest_card_counts() -> None:
    rows = [  # newest first, as the query orders them
        ("user_message", WORDING_ANSWER),
        ("assistant_response", WORDING_Q),
        ("user_message", _tap("card-a")),
        ("assistant_response", _hold("card-a")),
    ]
    assert _consent(rows, WORDING_ANSWER) == "named"


def test_f3_a_tap_on_an_older_card_never_consents_to_a_later_hold() -> None:
    rows = [
        ("user_message", WORDING_ANSWER),
        ("assistant_response", WORDING_Q),
        ("assistant_response", _hold("card-b")),
        ("user_message", _tap("card-a")),
        ("assistant_response", _hold("card-a")),
    ]
    assert _consent(rows, WORDING_ANSWER) is None


def test_f3_a_later_change_my_request_on_the_same_card_wins() -> None:
    rows = [
        ("user_message", WORDING_ANSWER),
        ("assistant_response", WORDING_Q),
        ("user_message", _tap("card-a", "change_request")),
        ("user_message", _tap("card-a")),
        ("assistant_response", _hold("card-a")),
    ]
    assert _consent(rows, WORDING_ANSWER) is None


def test_f3_a_draft_since_the_tap_ends_it() -> None:
    rows = [
        ("user_message", WORDING_ANSWER),
        ("draft_applied", {}),
        ("user_message", _tap("card-a")),
        ("assistant_response", _hold("card-a")),
    ]
    assert _consent(rows, WORDING_ANSWER) is None
