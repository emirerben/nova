"""Qendresa item 18: the device poll tells an old build to update for a focus recipe."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.client_protocol import clear_request_context, set_client_protocol


@pytest.fixture(autouse=True)
def _clean_context():
    clear_request_context()
    yield
    clear_request_context()


def _detail(uses_focus: bool, protocol: int):
    from app.routes import device_render

    layer = SimpleNamespace(
        uses_windows=False,
        uses_full_screen=False,
        uses_color=False,
        uses_focus=uses_focus,
    )
    recipe = SimpleNamespace(speaker_background=layer)
    status = SimpleNamespace(request=SimpleNamespace(recipe=recipe))
    set_client_protocol(protocol)
    return device_render._client_update_detail(SimpleNamespace(assembly_plan={}), {}, status)


def test_pinned_focus_recipe_is_426_below_39_and_fine_from_39():
    assert _detail(True, 41) == {
        "code": "client_update_required",
        "feature": "background_focus",
        "minimum_client_protocol": 42,
    }
    assert _detail(True, 42) is None


def test_a_recipe_without_a_focus_is_not_asked_to_update():
    assert _detail(False, 41) is None
