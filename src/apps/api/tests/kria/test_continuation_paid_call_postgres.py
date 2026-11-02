"""A continued turn (KRI-709) must be able to pay for its planner calls again.

Prod thread b61b7673, 2026-11-02: two turns in a row ended "The AI service ran out of time"
(``runtime_provider_outcome_unknown``, ``paid call 07ddbdc5f695 already settled``). The turn's
first run planned, settled its Main Creator call and stopped on "clips still checking"
(``continuable``); `_continue_turn` ran the same turn again as lease epoch 2, which re-sent the
SAME prompt under the SAME paid-call key (feature, model, request id = the thread id, attempt 1,
prompt hash) and the ledger refused it as an already-settled call.

These tests run the real reservation ledger (Postgres), the real `MainCreatorAgent` runtime and
the real `_call_main_creator`; only the model client is a fake. The epoch reaches the planner
through the turn task's own `_plan_with_live_agent`, exactly as in production.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.agents._runtime import ProviderOutcomeUnknownError, TerminalError, TransientError
from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreatorMediaRef,
    ResolvedCreatorManifest,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.database import sync_engine
from app.kria import planner
from app.kria.contracts import KriaTurnPlan
from app.kria.paid_call_scope import paid_call_request_id, turn_run_epoch
from app.services import ai_cost_control
from app.tasks import kria_runtime
from tests.agents.conftest import MockModelClient

_ASK = {
    "action": {
        "kind": "ask_user",
        "question": "Which clip should lead?",
        "reason_code": "choice",
        "options": [],
    }
}


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch):
    creator_id = uuid.uuid4()
    try:
        with sync_engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM ai_cost_reservations LIMIT 0"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable for the reservation ledger: {exc!r}")
    except ProgrammingError as exc:
        pytest.fail(f"database was not migrated to the cost-control schema: {exc!r}")
    monkeypatch.setattr(settings, "ai_cost_control_enabled", True)
    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    monkeypatch.setattr(settings, "ai_production_monthly_budget_usd", 1_000.0)
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": str(creator_id), "email": f"continuation-{creator_id}@example.test"},
        )
    yield creator_id
    with sync_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ai_cost_reservations WHERE creator_id = :id"), {"id": str(creator_id)}
        )
        conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(creator_id)})


def _main_creator_input() -> MainCreatorInput:
    available = CapabilityAvailability(available=True)
    manifest = ResolvedCreatorManifest(
        item_id="item-1",
        edit_format="montage",
        render_program="guided",
        media=[CreatorMediaRef(media_id="clip-01", kind="video")],
        capabilities={"edit_format:montage": available},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    return MainCreatorInput(
        user_message="Start with the cooking clip.", capability_manifest=manifest
    )


def _snapshot(thread_id: uuid.UUID, creator_id: uuid.UUID) -> dict[str, Any]:
    return {
        "thread_id": str(thread_id),
        "item_id": str(uuid.uuid4()),
        "creator_id": str(creator_id),
        "creator_agent_session_id": None,
    }


@pytest.fixture
def one_run(monkeypatch: pytest.MonkeyPatch, ledger):
    """Return ``run(epoch)``: the turn task's planning step for one lease epoch.

    Everything the planner would decide before and after Main Creator is replaced by the
    shortest possible stand-in; Main Creator itself goes through the real runtime and ledger.
    The creator's message, the clips and so the prompt are identical on every run, which is
    what a continuation is: the same turn, planned again.
    """

    client = MockModelClient()
    monkeypatch.setattr(planner, "default_client", lambda: client)
    reservations: list[ai_cost_control.PaidCallRequest] = []
    real_reserve = ai_cost_control.reserve_paid_call

    def recording_reserve(request):  # noqa: ANN001, ANN202
        reservations.append(request)
        return real_reserve(request)

    monkeypatch.setattr(ai_cost_control, "reserve_paid_call", recording_reserve)

    async def fake_plan_live_turn(db, *, thread_id, item_id, creator_id, **_kwargs):  # noqa: ANN001, ANN003
        await planner._call_main_creator(
            planner._CreatorInputs(
                agent_input=_main_creator_input(), intent_clips=[], creator_request=""
            ),
            thread_id=thread_id,
            creator_id=creator_id,
            item_id=item_id,
        )
        return SimpleNamespace(
            plan=KriaTurnPlan(mode="respond", turn_value="recovery", response="still checking")
        )

    monkeypatch.setattr(kria_runtime, "plan_live_turn", fake_plan_live_turn)

    thread_id = uuid.uuid4()
    snapshot = _snapshot(thread_id, ledger)

    async def run(epoch: int) -> None:
        client.queue(MainCreatorAgent.spec.model, _ASK)
        await kria_runtime._plan_with_live_agent(
            snapshot,
            "Start with the cooking clip.",
            turn_id=uuid.uuid4(),
            lease_owner="worker",
            lease_epoch=epoch,
        )

    run.thread_id = thread_id  # type: ignore[attr-defined]
    run.reservations = reservations  # type: ignore[attr-defined]
    return run


async def test_continued_run_can_repay_the_same_main_creator_call(one_run) -> None:
    await one_run(1)

    # Epoch 2 sends the identical prompt. Before the fix it reused epoch 1's settled paid-call
    # key and the ledger refused it: "paid call ... already settled".
    await one_run(2)

    keys = [request.idempotency_key for request in one_run.reservations]
    assert len(keys) == 2
    assert keys[0] != keys[1]


async def test_a_replay_of_the_same_run_is_still_refused(one_run) -> None:
    """The ledger still protects a genuine duplicate: same epoch, same prompt, same key."""

    await one_run(2)
    with pytest.raises(ProviderOutcomeUnknownError, match="already settled"):
        await one_run(2)


async def test_first_run_keys_are_unchanged(one_run) -> None:
    """Epoch 1 reserves under the thread id exactly as before this change."""

    await one_run(1)

    (request,) = one_run.reservations
    assert request.ctx.request_id == str(one_run.thread_id)


# --- the other planner calls that share the thread id as their request id ---------------


def test_request_ids_of_the_first_run_are_the_thread_id() -> None:
    thread_id = uuid.uuid4()

    assert paid_call_request_id(thread_id) == str(thread_id)
    # The brief extractor's retry id as it was spelled before the run epoch existed.
    assert paid_call_request_id(thread_id, "extractor-retry") == (f"{thread_id}:extractor-retry")


def test_request_ids_of_a_later_run_are_new_for_every_planner_call() -> None:
    thread_id = uuid.uuid4()
    token = turn_run_epoch.set(2)
    try:
        first = paid_call_request_id(thread_id)
        retry = paid_call_request_id(thread_id, "extractor-retry")
    finally:
        turn_run_epoch.reset(token)

    assert first == f"{thread_id}:e2"
    assert retry == f"{thread_id}:e2:extractor-retry"
    assert len({str(thread_id), f"{thread_id}:extractor-retry", first, retry}) == 4


@pytest.mark.parametrize("epoch", [1, 2])
async def test_brief_extractor_and_its_retry_use_the_run_scoped_request_id(
    monkeypatch: pytest.MonkeyPatch, epoch: int
) -> None:
    """The extractor reserves under the same id as Main Creator, and its retry under a second."""

    seen: list[str] = []

    class FakeExtractor:
        def __init__(self, _client: object) -> None: ...

        def run(self, _input: object, *, ctx: Any) -> object:
            seen.append(ctx.request_id)
            if len(seen) == 1:
                failure = TerminalError("provider timed out")
                failure.__cause__ = TransientError("deadline")
                raise failure
            return SimpleNamespace(brief_updates=[])

    monkeypatch.setattr(planner, "BriefExtractorAgent", FakeExtractor)
    monkeypatch.setattr(planner, "default_client", lambda: None)
    monkeypatch.setattr(planner, "_EXTRACTOR_RETRY_BACKOFF_S", 0)
    monkeypatch.setattr(planner.settings, "kria_error_honesty_enabled", True)
    thread_id = uuid.uuid4()
    token = turn_run_epoch.set(epoch)
    try:
        await planner._call_brief_extractor(
            planner._CreatorInputs(
                agent_input=_main_creator_input(), intent_clips=[], creator_request=""
            ),
            creator_request="Start with the cooking clip.",
            thread_id=thread_id,
            creator_id=uuid.uuid4(),
            item_id=uuid.uuid4(),
            creator_agent_session_id=None,
        )
    finally:
        turn_run_epoch.reset(token)

    scope = "" if epoch == 1 else f":e{epoch}"
    assert seen == [f"{thread_id}{scope}", f"{thread_id}{scope}:extractor-retry"]
