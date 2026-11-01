import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.pipeline.phone_text_style import resolve_phone_text_style, text_role, text_style_defaults
from app.schemas.phone_text_style import TEXT_STYLE_ROLES
from app.schemas.text_style_intent import LABEL_ANCHORS

client = TestClient(app, raise_server_exceptions=False)

ROW_IDS = {
    "title": "guided-title",
    "label": "guided-label-1",
    "pinned": "guided-pinned-0",
    "caption": "caption-0",
    "lyric": "song-caption-0",
}


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _row(role: str, **overrides) -> dict:
    corner = {"x_frac": 0.92, "y_frac": 0.12, "alignment": "right"} if role == "pinned" else {}
    return {
        "id": ROW_IDS[role],
        "text": "A slow morning in Lisbon" if role == "title" else "Day 1",
        "role": "generative_intro",
        "start_s": 0,
        "end_s": 3,
        "position": "custom",
        "x_frac": 0.5,
        "y_frac": 0.78,
        "font_family": "Inter",
        "size_px": 60,
        "color": "#FFFFFF",
        "stroke_width": 4,
        "shadow_enabled": True,
        "alignment": "center",
        "max_width_frac": 0.8,
        "effect": "static",
        **corner,
        **overrides,
    }


def _zone(row: dict) -> tuple[bool, str]:
    return row["y_frac"] < 0.5, row["alignment"]


def _get_memory(*, memory_enabled: bool, styling_enabled: bool) -> dict:
    db = AsyncMock()
    db.execute.return_value = MagicMock(
        scalars=lambda: MagicMock(all=lambda: []),
        scalar_one_or_none=lambda: None,
    )
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=uuid.uuid4(), creator_edit_learning_enabled=False
    )
    app.dependency_overrides[get_db] = lambda: db
    snapshot = SimpleNamespace(enabled=True, revision=1)
    with (
        patch("app.routes.memory.settings.creator_memory_enabled", memory_enabled),
        patch("app.routes.memory.settings.phone_text_styling_enabled", styling_enabled),
        patch("app.routes.memory.resolver.snapshot", AsyncMock(return_value=snapshot)),
    ):
        response = client.get("/me/memory")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    ("memory_enabled", "styling_enabled", "advertised"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_defaults_are_advertised_only_when_text_preferences_are(
    memory_enabled, styling_enabled, advertised
):
    body = _get_memory(memory_enabled=memory_enabled, styling_enabled=styling_enabled)
    assert body["text_preferences_available"] is advertised
    assert ("text_style_defaults" in body) is advertised


def test_every_role_the_app_can_save_has_defaults():
    body = _get_memory(memory_enabled=True, styling_enabled=True)
    assert set(body["text_style_defaults"]["roles"]) == set(TEXT_STYLE_ROLES)


@pytest.mark.parametrize("role", sorted(ROW_IDS))
def test_unsaved_role_renders_what_the_endpoint_says(role):
    advertised = text_style_defaults()["roles"][role]
    row = _row(role)
    assert text_role(row) == role

    (resolved,) = resolve_phone_text_style([row], preferences={}, requested={})

    assert advertised["outline"] == "off" and resolved["stroke_width"] == 0
    assert advertised["shadow"] == "off" and resolved["shadow_enabled"] is False

    anchors = text_style_defaults()["position_anchors"]
    if advertised["placement"] == "auto":
        first = anchors[advertised["placement_order"][0]]
        assert resolved["alignment"] == first["alignment"]
        assert resolved["x_frac"] == pytest.approx(first["x"], abs=0.03)
        assert resolved["y_frac"] == pytest.approx(first["y"], abs=0.12)
    else:
        assert advertised["placement_order"] == []
        assert _zone(resolved) == _zone(row)
        assert resolved["alignment"] == row["alignment"]


def test_label_usual_spot_is_where_a_default_label_stays():
    spot = text_style_defaults()["roles"]["label"]["usual_position"]
    x, y, alignment = LABEL_ANCHORS[spot]
    row = _row("label", x_frac=x, y_frac=y, alignment=alignment)

    (resolved,) = resolve_phone_text_style([row])

    assert _zone(resolved) == _zone(row)
    assert resolved["alignment"] == alignment
    assert resolved["x_frac"] == pytest.approx(x)


@pytest.mark.parametrize("role", ["title", "caption", "lyric"])
def test_cascade_falls_to_next_spot_when_first_is_taken(role):
    advertised = text_style_defaults()["roles"][role]
    first, second = advertised["placement_order"][:2]
    row = _row(role, text="Short")
    anchors = text_style_defaults()["position_anchors"]
    taken = (0, anchors[first]["y"] - 0.08, 1, anchors[first]["y"] + 0.08)

    (resolved,) = resolve_phone_text_style([row], excluded_boxes=[taken])

    assert resolved["alignment"] == anchors[second]["alignment"]
    assert resolved["y_frac"] < anchors[first]["y"] - 0.05


def test_saved_choice_replaces_the_advertised_default():
    (resolved,) = resolve_phone_text_style(
        [_row("title")], preferences={"title": {"stroke_width": 3, "shadow_enabled": True}}
    )
    assert resolved["stroke_width"] == 3
    assert resolved["shadow_enabled"] is True
