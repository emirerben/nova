from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routes.me import TrainingConsentRequest, set_training_consent


def _result(value):  # noqa: ANN001
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _refresh(row):  # noqa: ANN001
    row.id = row.id or uuid.uuid4()
    row.effective_at = datetime.now(UTC)


@pytest.mark.asyncio
async def test_creator_consent_grant_is_explicit_and_append_only(monkeypatch) -> None:
    creator = SimpleNamespace(id=uuid.uuid4())
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(None))
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=_refresh)
    monkeypatch.setattr("app.routes.me._latest_training_consent", AsyncMock(return_value=None))

    response = await set_training_consent(
        TrainingConsentRequest(
            action="grant",
            terms_version="training-v1",
            idempotency_key="grant-1",
        ),
        creator,
        db,
    )

    event = db.add.call_args.args[0]
    assert event.action == "grant"
    assert event.policy_version == "training-v1"
    assert event.source == "creator_settings"
    assert response.active is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_creator_revoke_excludes_grant_and_dispatches_exact_purge(monkeypatch) -> None:
    creator = SimpleNamespace(id=uuid.uuid4())
    grant = SimpleNamespace(
        id=uuid.uuid4(),
        action="grant",
        policy_version="training-v1",
        effective_at=datetime.now(UTC),
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(None))
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=_refresh)
    monkeypatch.setattr("app.routes.me._latest_training_consent", AsyncMock(return_value=grant))

    with patch("app.tasks.edit_training_artifacts.purge_edit_training_artifacts.delay") as purge:
        response = await set_training_consent(
            TrainingConsentRequest(
                action="revoke",
                terms_version="training-v1",
                idempotency_key="revoke-1",
            ),
            creator,
            db,
        )

    event = db.add.call_args.args[0]
    assert event.action == "revoke"
    assert event.revokes_consent_id == grant.id
    assert response.active is False
    purge.assert_called_once_with(str(creator.id), str(grant.id))
