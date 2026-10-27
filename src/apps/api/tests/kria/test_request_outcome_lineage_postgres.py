"""Request lineage at the real submit boundary, before implementation.

Failures: a retry becomes a new original; a revision overwrites the original;
legacy/foreign parents become trusted; replay changes the first committed link;
analytics metadata changes idempotency or blocks the creator's actual action.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.database import engine as async_engine
from app.kria.api_schemas import SubmitTurnBody
from app.kria.runtime import submit_turn
from app.models import CreationThread, CreationThreadEvent, CreatorAgentTurn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project


@pytest.fixture
async def project(monkeypatch):
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setenv("CREATOR_DURATION_CHECK_REVISION", "b" * 40)
    owner, thread, _ = _seed_runtime_project()
    yield owner, thread
    await async_engine.dispose()


async def submit(project, context=None, *, body=None):
    owner, thread_id = project
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, thread_id)
        body = body or SubmitTurnBody(
            message=f"Request {uuid.uuid4()}",
            client_event_id=str(uuid.uuid4()),
            expected_thread_revision=thread.revision,
            **({"outcome_context": context} if context is not None else {}),
        )
        accepted, _ = await submit_turn(db, thread_id=thread_id, creator_id=owner, body=body)
    return accepted, body


async def link(accepted):
    async with AsyncSessionLocal() as db:
        turn = await db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
        source = await db.get(CreationThreadEvent, turn.source_event_id)
        return (source.payload or {}).get("request_outcome_link")


async def finish(accepted):
    async with AsyncSessionLocal() as db:
        turn = await db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
        turn.status = "failed"
        turn.error = {"code": "creator_planning_failed"}
        turn.completed_at = datetime.now(UTC)
        await db.commit()


async def test_original_retry_revision_and_recovery_keep_distinct_identities(project):
    original, _ = await submit(
        project,
        {
            "relation": "original",
            "client_build": "1763",
            "client_version": "0.1.0",
        },
    )
    root = await link(original)
    assert root["request_turn_id"] == original.turn_id
    assert root["intent_revision_turn_id"] == original.turn_id
    assert root["original_known"] is True
    assert root["client_build"] == "1763" and root["client_version"] == "0.1.0"
    assert root["server_revision"] == "b" * 40
    prior = original
    revision = original.turn_id
    for relation in ["retry", "revision", "recovery"]:
        await finish(prior)
        current, _ = await submit(project, {"relation": relation, "parent_turn_id": prior.turn_id})
        record = await link(current)
        if relation != "retry":
            revision = current.turn_id
        assert record["request_turn_id"] == original.turn_id
        assert record["intent_revision_turn_id"] == revision
        assert record["parent_turn_id"] == prior.turn_id
        assert record["relation"] == relation
        assert record["original_known"] is True
        prior = current
    assert await link(original) == root


async def test_a_retry_of_a_legacy_turn_does_not_fabricate_known_original_intent(project):
    old, _ = await submit(project)
    assert await link(old) is None
    await finish(old)
    retry, _ = await submit(project, {"relation": "retry", "parent_turn_id": old.turn_id})
    record = await link(retry)
    assert record["request_turn_id"] == old.turn_id
    assert record["original_known"] is False


@pytest.mark.parametrize("foreign", [False, True], ids=["missing", "foreign"])
async def test_unavailable_parent_keeps_the_action_but_not_a_trusted_link(project, foreign):
    parent = str(uuid.uuid4())
    if foreign:
        owner, thread, _ = _seed_runtime_project()
        earlier, _ = await submit((owner, thread), {"relation": "original"})
        parent = earlier.turn_id
    accepted, _ = await submit(project, {"relation": "retry", "parent_turn_id": parent})
    record = await link(accepted)
    assert record["original_known"] is False
    assert record["request_turn_id"] == accepted.turn_id
    assert record["link_status"] == "parent_unavailable"
    assert parent not in str(record)


async def test_transport_replay_keeps_the_first_link_without_another_event(project):
    first, body = await submit(project, {"relation": "original"})
    stored = await link(first)
    replay_body = SubmitTurnBody(
        **{
            **body.model_dump(mode="json"),
            "outcome_context": {"relation": "unclassified"},
        }
    )
    replay, _ = await submit(project, body=replay_body)
    assert replay.replayed is True and replay.turn_id == first.turn_id
    assert await link(replay) == stored
    async with AsyncSessionLocal() as db:
        events = list(
            (
                await db.scalars(
                    select(CreationThreadEvent).where(
                        CreationThreadEvent.thread_id == project[1],
                        CreationThreadEvent.client_event_id == body.client_event_id,
                    )
                )
            ).all()
        )
        assert len(events) == 1


async def test_future_protocol_metadata_cannot_block_the_requested_action(project):
    from app.services.client_protocol import clear_request_context, set_client_protocol

    set_client_protocol(10000)
    try:
        accepted, _ = await submit(project, {"relation": "original"})
        assert (await link(accepted))["client_protocol"] == 10000
    finally:
        clear_request_context()
