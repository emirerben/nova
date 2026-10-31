"""KRI-1093: the Lip-sync card in the chat's "What are we making?" picker.

It is a montage underneath (``edit_format: "montage"``), offered only where an uploaded song
can be used and only to a native client. Offering == accepting: ``select_format`` validates
against the same list the capabilities manifest shows.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.requests import Request

import app.routes.creation_threads as routes
from app.config import settings
from app.services import phone_rollout

USER_ID = uuid.UUID("0b6b1c52-3f0e-4c7a-9d51-6f1e2a3b4c5d")


@pytest.fixture
def song_lane(monkeypatch):
    monkeypatch.setattr(settings, "user_song_montage_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", ["musicBed", "audioMix"])


@pytest.mark.parametrize("native_mode", ["pilot", "strict"])
def test_native_picker_lists_lipsync_last(song_lane, native_mode) -> None:
    formats = routes._available_formats(native_mode=native_mode)

    assert list(formats)[-1] == "lipsync"
    assert formats["lipsync"] == "montage"
    assert routes._format_clip_limit(formats["lipsync"]) == routes._format_clip_limit(
        formats["montage"]
    )


@pytest.mark.parametrize("native_mode", ["pilot", "strict"])
def test_lipsync_is_hidden_from_a_client_without_the_song_affordance(
    song_lane, native_mode
) -> None:
    """/capabilities passes its own song gate (web in device-only mode reports `strict`)."""
    assert "lipsync" in routes._available_formats(native_mode=native_mode, song_lane=True)
    assert "lipsync" not in routes._available_formats(native_mode=native_mode, song_lane=False)


def test_the_web_picker_never_lists_lipsync(song_lane) -> None:
    assert "lipsync" not in routes._available_formats()


@pytest.mark.parametrize("native_mode", ["pilot", "strict"])
def test_lipsync_needs_the_song_lane(monkeypatch, song_lane, native_mode) -> None:
    monkeypatch.setattr(settings, "user_song_montage_enabled", False)
    assert "lipsync" not in routes._available_formats(native_mode=native_mode)

    monkeypatch.setattr(settings, "user_song_montage_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", ["musicBed"])
    assert "lipsync" not in routes._available_formats(native_mode=native_mode)


def test_the_native_phone_format_filter_keeps_lipsync_with_montage(song_lane, monkeypatch) -> None:
    """Lip-sync is a montage underneath, so the phone filter treats it like Montage."""
    monkeypatch.setattr(
        phone_rollout, "phone_render_supported_formats", lambda: frozenset({"montage"})
    )
    assert routes._available_formats(native_mode="strict") == {
        "montage": "montage",
        "lipsync": "montage",
        "slides": "slides",
    }
    monkeypatch.setattr(phone_rollout, "phone_render_supported_formats", lambda: frozenset())
    assert "lipsync" not in routes._available_formats(native_mode="strict")


def test_the_montage_card_and_the_web_list_are_unchanged_by_the_song_lane(
    song_lane, monkeypatch
) -> None:
    on = routes._available_formats()
    monkeypatch.setattr(settings, "user_song_montage_enabled", False)
    off = routes._available_formats()
    assert on == off
    assert on["montage"] == "montage"


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": (f"test-{uuid.uuid4()}", 0),
        }
    )


def _thread(state: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=USER_ID,
        status="active",
        revision=0,
        runtime_version=1,
        active_plan_item_id=None,
        active_job_id=None,
        state=state,
    )


async def _select(monkeypatch, thread, fmt: str):
    db = Mock()
    db.get = AsyncMock(return_value=None)
    db.add = Mock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_reject_input_mutation_while_rendering", AsyncMock())
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    return await routes.action_thread(
        _request(),
        str(thread.id),
        routes.ActionBody(
            action="select_format",
            payload={"format": fmt},
            client_action_id=f"select-{fmt}-{uuid.uuid4()}",
            expected_revision=0,
        ),
        SimpleNamespace(id=USER_ID),
        db,
    )


@pytest.mark.asyncio
async def test_selecting_lipsync_stores_the_card_and_a_montage_edit_format(
    monkeypatch, song_lane
) -> None:
    thread = _thread({"device_intent": "native_device_only"})
    monkeypatch.setattr(routes, "has_native_device_only_intent", lambda state: True)

    await _select(monkeypatch, thread, "lipsync")

    assert thread.state["format"] == "lipsync"
    assert thread.state["edit_format"] == "montage"


@pytest.mark.asyncio
async def test_selecting_montage_after_lipsync_drops_the_card(monkeypatch, song_lane) -> None:
    thread = _thread({"format": "lipsync", "edit_format": "montage"})
    monkeypatch.setattr(routes, "has_native_device_only_intent", lambda state: True)

    await _select(monkeypatch, thread, "montage")

    assert thread.state["format"] == "montage"
    assert thread.state["edit_format"] == "montage"


@pytest.mark.asyncio
async def test_selecting_lipsync_where_it_is_not_offered_is_refused(monkeypatch, song_lane) -> None:
    thread = _thread({})
    with pytest.raises(routes.HTTPException) as web:
        await _select(monkeypatch, thread, "lipsync")
    assert web.value.status_code == 409

    with pytest.raises(routes.HTTPException) as unknown:
        await _select(monkeypatch, thread, "lipsink")
    assert unknown.value.status_code == 422
