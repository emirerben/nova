from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.content_plan_persona import (
    PlanPersonaOwnershipError,
    is_plan_persona_owned,
    load_owned_plan_persona,
    load_owned_plan_persona_sync,
    require_plan_persona_owned,
)


def _rows() -> tuple[SimpleNamespace, SimpleNamespace]:
    user_id = uuid.uuid4()
    persona_id = uuid.uuid4()
    plan = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        persona_id=persona_id,
        ownership_quarantined_at=None,
    )
    persona = SimpleNamespace(id=persona_id, user_id=user_id)
    return plan, persona


def _result(value: object) -> MagicMock:
    return MagicMock(scalar_one_or_none=MagicMock(return_value=value))


def test_owner_predicate_requires_both_ids_and_clear_quarantine() -> None:
    plan, persona = _rows()

    assert is_plan_persona_owned(plan, persona)  # type: ignore[arg-type]

    persona.user_id = uuid.uuid4()
    assert not is_plan_persona_owned(plan, persona)  # type: ignore[arg-type]
    persona.user_id = plan.user_id
    persona.id = uuid.uuid4()
    assert not is_plan_persona_owned(plan, persona)  # type: ignore[arg-type]
    persona.id = plan.persona_id
    plan.ownership_quarantined_at = datetime.now(UTC)
    assert not is_plan_persona_owned(plan, persona)  # type: ignore[arg-type]


def test_typed_error_carries_only_safe_plan_identifiers() -> None:
    plan, persona = _rows()
    persona.user_id = uuid.uuid4()

    with pytest.raises(PlanPersonaOwnershipError) as caught:
        require_plan_persona_owned(plan, persona)  # type: ignore[arg-type]

    assert caught.value.plan_id == plan.id
    assert caught.value.plan_user_id == plan.user_id
    assert caught.value.persona_id == plan.persona_id
    assert str(persona.user_id) not in str(caught.value)


@pytest.mark.asyncio
async def test_async_loader_queries_link_and_owner_and_can_lock() -> None:
    plan, persona = _rows()
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(persona))

    loaded = await load_owned_plan_persona(  # type: ignore[arg-type]
        db,
        plan,
        for_update=True,
    )

    assert loaded is persona
    stmt = db.execute.await_args.args[0]
    compiled = str(stmt)
    assert "personas.id" in compiled
    assert "personas.user_id" in compiled
    assert "FOR UPDATE" in compiled.upper()
    assert stmt.get_execution_options()["populate_existing"] is True


@pytest.mark.asyncio
async def test_async_loader_fails_closed_for_missing_row() -> None:
    plan, _ = _rows()
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(None))

    with pytest.raises(PlanPersonaOwnershipError):
        await load_owned_plan_persona(db, plan)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_quarantine_fails_before_persona_query() -> None:
    plan, _ = _rows()
    plan.ownership_quarantined_at = datetime.now(UTC)
    db = AsyncMock()

    with pytest.raises(PlanPersonaOwnershipError):
        await load_owned_plan_persona(db, plan)  # type: ignore[arg-type]

    db.execute.assert_not_awaited()


def test_sync_loader_uses_the_same_compound_predicate() -> None:
    plan, persona = _rows()
    session = MagicMock()
    session.execute.return_value = _result(persona)

    loaded = load_owned_plan_persona_sync(  # type: ignore[arg-type]
        session,
        plan,
        for_update=True,
    )

    assert loaded is persona
    stmt = session.execute.call_args.args[0]
    compiled = str(stmt)
    assert "personas.id" in compiled
    assert "personas.user_id" in compiled
    assert "FOR UPDATE" in compiled.upper()
