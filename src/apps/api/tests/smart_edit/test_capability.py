from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.models import CreatorStyleAssignment
from app.services.smart_captions import (
    resolve_smart_captions_capability,
    resolve_smart_captions_context_sync,
)


@pytest.mark.asyncio
async def test_capability_fails_closed_before_query_when_flag_is_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "smart_captions_enabled", False)
    db = AsyncMock()

    result = await resolve_smart_captions_capability(
        user_id=uuid.uuid4(), edit_format="subtitled", db=db
    )

    assert result.available is False
    assert result.reason == "feature_disabled"
    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_capability_requires_supported_format_and_assignment(monkeypatch) -> None:
    monkeypatch.setattr(settings, "smart_captions_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    db = AsyncMock()

    unsupported = await resolve_smart_captions_capability(
        user_id=uuid.uuid4(), edit_format="montage", db=db
    )
    assert unsupported.reason == "unsupported_edit_format"
    db.get.assert_not_awaited()

    db.get.return_value = None
    unassigned = await resolve_smart_captions_capability(
        user_id=uuid.uuid4(), edit_format="subtitled", db=db
    )
    assert unassigned.reason == "not_assigned"


@pytest.mark.asyncio
async def test_capability_returns_server_owned_preset(monkeypatch) -> None:
    monkeypatch.setattr(settings, "smart_captions_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(
        enabled=True,
        preset_id="cigdem",
        preset_version="v1",
    )
    user_id = uuid.uuid4()

    result = await resolve_smart_captions_capability(
        user_id=user_id, edit_format="subtitled", db=db
    )

    assert result.available is True
    assert result.reason is None
    assert result.preset_id == "cigdem"
    db.get.assert_awaited_once_with(CreatorStyleAssignment, user_id)


@pytest.mark.asyncio
async def test_capability_fails_closed_when_the_base_renderer_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "smart_captions_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", False)
    db = AsyncMock()

    result = await resolve_smart_captions_capability(
        user_id=uuid.uuid4(), edit_format="subtitled", db=db
    )

    assert result.available is False
    assert result.reason == "base_renderer_disabled"
    db.get.assert_not_awaited()


def test_sync_dispatch_context_rechecks_assignment_and_pins_preset(monkeypatch) -> None:
    monkeypatch.setattr(settings, "smart_captions_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    user_id = uuid.uuid4()
    db = MagicMock()
    db.get.return_value = SimpleNamespace(
        enabled=True,
        preset_id="cigdem",
        preset_version="v1",
    )

    context = resolve_smart_captions_context_sync(
        user_id=user_id,
        edit_format="subtitled",
        requested=True,
        db=db,
    )

    assert context == {"preset_id": "cigdem", "preset_version": "v1"}
    db.get.assert_called_once_with(CreatorStyleAssignment, user_id)


def test_sync_dispatch_context_omits_unrequested_or_revoked_feature(monkeypatch) -> None:
    monkeypatch.setattr(settings, "smart_captions_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    db = MagicMock()
    assert (
        resolve_smart_captions_context_sync(
            user_id=uuid.uuid4(), edit_format="subtitled", requested=False, db=db
        )
        is None
    )
    db.get.assert_not_called()

    db.get.return_value = SimpleNamespace(enabled=False, preset_id="cigdem", preset_version="v1")
    assert (
        resolve_smart_captions_context_sync(
            user_id=uuid.uuid4(), edit_format="subtitled", requested=True, db=db
        )
        is None
    )
