"""A refusal leaves its reason code on the turn row (KRI-1382).

Real `run_kria_turn` and the real completion transaction on Postgres; the planner is scripted
with the replies the real planner builds (the strategy check's own refusal, `_request_recovery`).

Failure modes (written before the code):

* a refusal completes as an ordinary assistant message, so the turn row has `status=completed`
  and no code: it counts as a success, or as nothing, and the reason is gone;
* the main creator refuses through `ask_user` (its prompt says to, and `is_refusal_code` is the
  server's own test for it): that turn is `question`, has no brief coverage and so counts as a
  success unless the planner stamps it; an ordinary clarifying `ask_user` must not be stamped;
* a turn closed because a newer message arrived is stamped as a failure, though it is not an
  attempt;
* a reply path added later forgets to stamp: a coded fallback exists so a recovery reply is never
  silently uncoded (`unclassified_recovery` is visible in the health page, an empty cell is not);
* an ordinary question or answer is stamped as a refusal and the success rate drops for no reason;
* the code is written to `turn.error`, and `media_collection_stories` reads a completed turn with
  an error as broken;
* stamping replaces diagnostics a plan already carried (a pinned-card refusal).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.agents._schemas.creator_agent import AskUser
from app.database import AsyncSessionLocal, sync_session
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import (
    PlannedKriaTurn,
    _refused_turn,
    _request_recovery,
    adapt_creator_action,
)
from app.kria.strategy_policy import RefusedStrategy, check_strategy_for_runtime_v2
from app.models import CreatorAgentTurn
from app.services.creation_health import creation_health
from app.tasks.kria_runtime import run_kria_turn
from tests.agents.test_main_creator_agent import _manifest
from tests.kria.test_pinned_text_limits import TOP, _strategy
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project
from tests.kria.test_turn_auto_continue_postgres import (  # noqa: F401
    FINAL_REPLY,
    _publishes,
    _respond,
    _runtime_enabled,
    _script_planner,
    _submit,
)


def _unique_day() -> datetime:
    """A day no other test or earlier run has rows on, so the report counts only ours."""
    return datetime(1990, 1, 1, 12, tzinfo=UTC) + timedelta(days=uuid.uuid4().int % 9000)


def _real_strategy_refusal() -> PlannedKriaTurn:
    manifest = _manifest()
    refused = check_strategy_for_runtime_v2(manifest, _strategy([TOP] * 5))
    assert isinstance(refused, RefusedStrategy)
    return _refused_turn(manifest, refused)


def _real_request_recovery() -> PlannedKriaTurn:
    return _request_recovery(_manifest(), None, reason="creator_planning_failed")


def _readiness_block() -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=KriaTurnPlan(mode="respond", turn_value="question", response="I can't make this yet:"),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        brief_coverage={"stage": "draft", "reason": "approval_requirements_not_ready"},
    )


def _ordinary_question() -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=KriaTurnPlan(mode="respond", turn_value="question", response="Which clip first?"),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
    )


def _ask_user(reason_code: str) -> PlannedKriaTurn:
    action = AskUser(
        kind="ask_user",
        question="I can't intercut those videos with their own audio. Switch to a Montage?",
        reason_code=reason_code,
        options=["Switch to Montage", "Keep it as is"],
    )
    return PlannedKriaTurn(
        plan=adapt_creator_action(action), manifest_hash="a" * 64, context_hash="b" * 64
    )


CASES = {
    "strategy_refusal": (_real_strategy_refusal, ("refused", "pinned_text_too_many")),
    "request_recovery": (_real_request_recovery, ("recovery", "creator_planning_failed")),
    "readiness_block": (_readiness_block, ("blocked", "approval_requirements_not_ready")),
    "unclassified_recovery": (
        lambda: _respond(FINAL_REPLY, continuable=False),
        ("recovery", "unclassified_recovery"),
    ),
    "ordinary_question": (_ordinary_question, None),
    "ask_user_refusal": (
        lambda: _ask_user("unsupported_audio_intercut"),
        ("refused", "unsupported_audio_intercut"),
    ),
    "ask_user_refusal_free_text_code": (
        lambda: _ask_user("Cannot: audio intercut!"),
        ("refused", "cannot_audio_intercut"),
    ),
    "ask_user_clarifying_question": (lambda: _ask_user("which_clip_first"), None),
}


async def _run_case(
    monkeypatch: pytest.MonkeyPatch, planned: PlannedKriaTurn, day: datetime
) -> uuid.UUID:
    user_id, thread_id, _ = _seed_runtime_project()
    _script_planner(monkeypatch, planned)
    _publishes(monkeypatch)
    accepted = await _submit(user_id, thread_id, "Make it exactly like my last post", 2)
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result == {"turn_id": accepted.turn_id, "status": "completed"}
    turn_id = uuid.UUID(accepted.turn_id)
    with sync_session() as db:
        db.execute(
            update(CreatorAgentTurn).where(CreatorAgentTurn.id == turn_id).values(created_at=day)
        )
        db.commit()
    return turn_id


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(CASES))
async def test_every_refusal_path_records_a_code_on_the_completed_turn(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    build, expected = CASES[name]
    turn_id = await _run_case(monkeypatch, build(), _unique_day())

    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, turn_id)
        assert turn is not None and turn.status == "completed"
        recorded = ((turn.plan_json or {}).get("diagnostics") or {}).get("outcome")
        assert turn.error is None
    if expected is None:
        assert recorded is None
    else:
        assert (recorded["kind"], recorded["code"]) == expected


@pytest.mark.asyncio
async def test_the_health_report_counts_each_refusal_under_its_own_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = _unique_day()
    for build, _expected in CASES.values():
        await _run_case(monkeypatch, build(), day)

    async with AsyncSessionLocal() as db:
        report = await creation_health(db, days=1, now=day)

    assert report["turns"]["attempts"] == len(CASES)
    assert report["turns"]["successes"] == 2
    assert report["turns"]["success_rate"] == round(2 / len(CASES), 4)
    reasons = {(r["kind"], r["code"]): r["count"] for r in report["top_failure_reasons"]}
    assert reasons == {
        ("refused", "pinned_text_too_many"): 1,
        ("recovery", "creator_planning_failed"): 1,
        ("blocked", "approval_requirements_not_ready"): 1,
        ("recovery", "unclassified_recovery"): 1,
        ("refused", "unsupported_audio_intercut"): 1,
        ("refused", "cannot_audio_intercut"): 1,
    }


@pytest.mark.asyncio
async def test_a_turn_closed_by_a_newer_message_is_not_an_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = _unique_day()
    user_id, thread_id, _ = _seed_runtime_project()
    _script_planner(monkeypatch, _respond(FINAL_REPLY, continuable=True))
    _publishes(monkeypatch)
    first = await _submit(user_id, thread_id, "Caption every clip with what happens", 2)
    second = await _submit(user_id, thread_id, "Actually make it shorter", first.thread_revision)
    assert second.status == "queued"

    result = await asyncio.to_thread(run_kria_turn.run, first.turn_id)

    assert result == {"turn_id": first.turn_id, "status": "completed"}
    with sync_session() as db:
        db.execute(
            update(CreatorAgentTurn)
            .where(CreatorAgentTurn.id.in_([uuid.UUID(first.turn_id), uuid.UUID(second.turn_id)]))
            .values(created_at=day)
        )
        db.commit()
        closed = db.get(CreatorAgentTurn, uuid.UUID(first.turn_id))
        outcome = ((closed.plan_json or {}).get("diagnostics") or {}).get("outcome")
    assert outcome == {"kind": "superseded", "code": "continuation_superseded"}

    async with AsyncSessionLocal() as db:
        report = await creation_health(db, days=1, now=day)
    assert report["turns"]["attempts"] == 0
    assert report["top_failure_reasons"] == []
