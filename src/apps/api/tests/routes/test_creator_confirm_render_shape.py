"""KRI-306 (runtime v1): the confirm body's output-shape choice."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.agents._schemas.creator_agent import CreativeStrategy
from app.routes.creation_threads import _ACTION_PAYLOAD_KEYS
from app.routes.creator_agent import ConfirmBody, _confirm_render_shape


def _body(**extra) -> ConfirmBody:
    return ConfirmBody(
        session_id=uuid.uuid4(),
        expected_revision=1,
        plan_version=1,
        plan_hash="a" * 64,
        client_event_id="evt-1",
        **extra,
    )


def _args(edit_format: str = "montage"):
    item = SimpleNamespace(landscape_fit="fit", clip_assignments=[], current_job_id=None)
    plan = SimpleNamespace(strategy=CreativeStrategy(edit_format=edit_format))
    return AsyncMock(), item, plan, uuid.uuid4()


@pytest.fixture(autouse=True)
def _device(monkeypatch):
    monkeypatch.setattr("app.config.settings.ios_device_only_mode", True)
    monkeypatch.setenv("LANDSCAPE_OUTPUT_ENABLED", "true")


@pytest.mark.asyncio
async def test_no_choice_is_a_no_op():
    db, item, plan, user = _args("narrated")
    assert await _confirm_render_shape(db, item, plan, _body(), user, strict=True) is None


@pytest.mark.asyncio
async def test_a_valid_choice_resolves():
    db, item, plan, user = _args()
    shape = await _confirm_render_shape(
        db, item, plan, _body(output_orientation="landscape"), user, strict=True
    )
    assert shape == {"output_orientation": "landscape", "landscape_fit": "fill"}


@pytest.mark.asyncio
async def test_an_unavailable_choice_422s_a_fresh_confirm_but_not_a_resume():
    db, item, plan, user = _args("subtitled")
    body = _body(output_orientation="landscape")
    with pytest.raises(HTTPException) as error:
        await _confirm_render_shape(db, item, plan, body, user, strict=True)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "render_shape_unsupported"
    assert await _confirm_render_shape(db, item, plan, body, user, strict=False) is None


def test_the_chat_actions_pass_the_shape_keys_through():
    for action in ("generate", "confirm_generation"):
        assert {"output_orientation", "landscape_fit"} <= _ACTION_PAYLOAD_KEYS[action]


def test_confirm_body_rejects_unknown_shape_values():
    with pytest.raises(ValueError):
        _body(output_orientation="square")
