"""Route tests for POST /personas/agent/start and POST /personas/agent/turn.

Tests:
- flag off (style_agent_enabled=False) → 404 from both routes
- start: returns greeting + suggestion chips
- turn: style_edit intent → _apply_style_edit called, applied=true
- turn: persona_preference intent → retune task queued, applied=true
- turn: clarify intent → no write, applied=false
- turn: unknown intent → no write, applied=false, helpful reply
- turn: forbidden knob (extra field not in parity-safe list) → no write, applied=false
- turn: needs_clarification from agent → no write, applied=false
- unauthenticated requests → 401

Settings are imported lazily inside each route function, so we patch app.config.settings.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app

# ── Helpers ──────────────────────────────────────────────────────────────────


def _fake_user(uid: uuid.UUID | None = None) -> MagicMock:
    u = MagicMock()
    u.id = uid or uuid.uuid4()
    u.onboarding_status = "ready"
    return u


def _persona_row(
    user_id: uuid.UUID,
    *,
    style: dict | None = None,
    questionnaire: dict | None = None,
) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.user_id = user_id
    row.persona_status = "ready"
    row.style = style
    row.questionnaire = questionnaire or {}
    row.error_detail = None
    return row


def _async_db(scalar_result=None) -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    db.get = AsyncMock(return_value=None)
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=scalar_result))
    )
    return db


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _make_intent_result(
    intent: str,
    fields: dict | None = None,
    confidence: float = 0.9,
    reply: str = "Done.",
    suggestions: list[str] | None = None,
    needs_clarification: bool = False,
) -> MagicMock:
    """Build a mock StyleIntentOutput for patching the agent."""
    result = MagicMock()
    result.intent = intent
    result.fields = fields or {}
    result.confidence = confidence
    result.reply = reply
    result.suggestions = suggestions or ["Change font", "Adjust size"]
    result.needs_clarification = needs_clarification
    return result


def _settings(style_agent_enabled: bool = True, user_style_enabled: bool = True) -> MagicMock:
    """Build a mock settings object."""
    cfg = MagicMock()
    cfg.style_agent_enabled = style_agent_enabled
    cfg.user_style_enabled = user_style_enabled
    return cfg


# ── Auth gate ─────────────────────────────────────────────────────────────────


def test_agent_start_requires_auth(client: TestClient) -> None:
    """No auth → 401."""
    app.dependency_overrides[get_db] = lambda: _async_db()
    with patch("app.config.settings", _settings()):
        resp = client.post("/personas/agent/start")
    assert resp.status_code == 401


def test_agent_turn_requires_auth(client: TestClient) -> None:
    """No auth → 401."""
    app.dependency_overrides[get_db] = lambda: _async_db()
    with patch("app.config.settings", _settings()):
        resp = client.post("/personas/agent/turn", json={"answer": "bigger font"})
    assert resp.status_code == 401


# ── Kill-switch ───────────────────────────────────────────────────────────────


def test_agent_start_404_when_flag_off(client: TestClient) -> None:
    """style_agent_enabled=False → 404 from /agent/start."""
    user = _fake_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db()

    with patch("app.config.settings", _settings(style_agent_enabled=False)):
        resp = client.post("/personas/agent/start")

    assert resp.status_code == 404
    assert "style_agent_not_enabled" in resp.json().get("detail", "")


def test_agent_turn_404_when_flag_off(client: TestClient) -> None:
    """style_agent_enabled=False → 404 from /agent/turn."""
    user = _fake_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db()

    with patch("app.config.settings", _settings(style_agent_enabled=False)):
        resp = client.post("/personas/agent/turn", json={"answer": "bigger font"})

    assert resp.status_code == 404
    assert "style_agent_not_enabled" in resp.json().get("detail", "")


# ── /agent/start ─────────────────────────────────────────────────────────────


def test_agent_start_returns_greeting_no_style(client: TestClient) -> None:
    """Start without a style → generic greeting + opening chips."""
    user = _fake_user()
    row = _persona_row(user.id, style=None)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=row)

    with patch("app.config.settings", _settings()):
        resp = client.post("/personas/agent/start")

    assert resp.status_code == 200
    body = resp.json()
    assert "reply" in body
    assert isinstance(body["suggestions"], list)
    assert len(body["suggestions"]) > 0
    assert body["applied"] is False
    assert body["intent"] == "greeting"


def test_agent_start_returns_personalised_greeting_with_style(client: TestClient) -> None:
    """Start WITH a style snapshot → greeting mentions the style_set_id."""
    user = _fake_user()
    row = _persona_row(
        user.id,
        style={"status": "ready", "style_set_id": "travel_editorial", "knobs": {}},
    )
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=row)

    with patch("app.config.settings", _settings()):
        resp = client.post("/personas/agent/start")

    assert resp.status_code == 200
    body = resp.json()
    assert "travel_editorial" in body["reply"]


def test_agent_start_404_no_persona(client: TestClient) -> None:
    """No persona row → 404."""
    user = _fake_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=None)

    with patch("app.config.settings", _settings()):
        resp = client.post("/personas/agent/start")

    assert resp.status_code == 404


# ── /agent/turn — style_edit intent ──────────────────────────────────────────


def test_agent_turn_style_edit_applies_and_returns_applied_true(client: TestClient) -> None:
    """style_edit intent → _apply_style_edit called, response has applied=True."""
    user = _fake_user()
    row = _persona_row(user.id, style={"status": "ready", "style_set_id": "default", "knobs": {}})
    db = _async_db(scalar_result=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    intent_result = _make_intent_result(
        intent="style_edit",
        fields={"knobs": {"text_size_px": 60}},
        reply="Done — your text is now larger.",
    )

    with (
        patch("app.config.settings", _settings()),
        patch("app.routes.personas.asyncio") as mock_asyncio,
        patch("app.routes.personas._apply_style_edit", new_callable=AsyncMock) as mock_apply,
    ):
        mock_asyncio.to_thread = AsyncMock(return_value=intent_result)
        mock_apply.return_value = {"status": "edited", "style_set_id": "default"}

        resp = client.post("/personas/agent/turn", json={"answer": "make text bigger"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["intent"] == "style_edit"
    mock_apply.assert_called_once()


def test_agent_turn_scope_reduction_applies(client: TestClient) -> None:
    """scope_reduction intent → _apply_style_edit called, applied=True."""
    user = _fake_user()
    row = _persona_row(user.id, style={"status": "ready", "knobs": {}})
    db = _async_db(scalar_result=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    intent_result = _make_intent_result(
        intent="scope_reduction",
        fields={"footage_type_bias": ["broll"]},
        reply="Got it — I'll reduce indoor footage.",
    )

    with (
        patch("app.config.settings", _settings()),
        patch("app.routes.personas.asyncio") as mock_asyncio,
        patch("app.routes.personas._apply_style_edit", new_callable=AsyncMock) as mock_apply,
    ):
        mock_asyncio.to_thread = AsyncMock(return_value=intent_result)
        mock_apply.return_value = {"status": "edited"}

        resp = client.post("/personas/agent/turn", json={"answer": "less indoor stuff"})

    assert resp.status_code == 200
    assert resp.json()["applied"] is True


# ── /agent/turn — persona_preference intent ──────────────────────────────────


def test_agent_turn_persona_preference_queues_retune(client: TestClient) -> None:
    """persona_preference → retune task queued, applied=True."""
    user = _fake_user()
    row = _persona_row(user.id, style={"status": "ready"})
    row.persona_status = "ready"
    db = _async_db(scalar_result=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    intent_result = _make_intent_result(
        intent="persona_preference",
        fields={"free_text": "I want more outdoor content"},
        reply="Got it — I'll update your content focus.",
    )

    with (
        patch("app.config.settings", _settings()),
        patch("app.routes.personas.asyncio") as mock_asyncio,
        patch("app.tasks.persona_build.retune_persona_from_feedback") as mock_task,
    ):
        mock_asyncio.to_thread = AsyncMock(return_value=intent_result)
        mock_task.delay = MagicMock()

        resp = client.post(
            "/personas/agent/turn",
            json={"answer": "I want more outdoor content"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["intent"] == "persona_preference"


# ── /agent/turn — clarify / unknown / no-write paths ─────────────────────────


def test_agent_turn_clarify_intent_no_write(client: TestClient) -> None:
    """clarify intent → no write, applied=False."""
    user = _fake_user()
    row = _persona_row(user.id, style={"status": "ready"})
    db = _async_db(scalar_result=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    intent_result = _make_intent_result(
        intent="clarify",
        needs_clarification=True,
        reply="Did you mean font size or font style?",
    )

    with (
        patch("app.config.settings", _settings()),
        patch("app.routes.personas.asyncio") as mock_asyncio,
        patch("app.routes.personas._apply_style_edit", new_callable=AsyncMock) as mock_apply,
    ):
        mock_asyncio.to_thread = AsyncMock(return_value=intent_result)

        resp = client.post("/personas/agent/turn", json={"answer": "change my font"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is False
    assert body["intent"] == "clarify"
    mock_apply.assert_not_called()


def test_agent_turn_needs_clarification_no_write(client: TestClient) -> None:
    """Low-confidence (needs_clarification=True) → no write, applied=False."""
    user = _fake_user()
    row = _persona_row(user.id, style={"status": "ready"})
    db = _async_db(scalar_result=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    intent_result = _make_intent_result(
        intent="style_edit",
        fields={"knobs": {"font_family": "Playfair Display"}},
        confidence=0.4,  # below threshold → needs_clarification forced true
        needs_clarification=True,
        reply="Could you be more specific?",
    )

    with (
        patch("app.config.settings", _settings()),
        patch("app.routes.personas.asyncio") as mock_asyncio,
        patch("app.routes.personas._apply_style_edit", new_callable=AsyncMock) as mock_apply,
    ):
        mock_asyncio.to_thread = AsyncMock(return_value=intent_result)

        resp = client.post("/personas/agent/turn", json={"answer": "something vague"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is False
    mock_apply.assert_not_called()


def test_agent_turn_unknown_intent_no_write(client: TestClient) -> None:
    """unknown intent → no write, applied=False, helpful reply."""
    user = _fake_user()
    row = _persona_row(user.id, style={"status": "ready"})
    db = _async_db(scalar_result=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    intent_result = _make_intent_result(
        intent="unknown",
        reply="I can change your style or content focus.",
    )

    with (
        patch("app.config.settings", _settings()),
        patch("app.routes.personas.asyncio") as mock_asyncio,
        patch("app.routes.personas._apply_style_edit", new_callable=AsyncMock) as mock_apply,
    ):
        mock_asyncio.to_thread = AsyncMock(return_value=intent_result)

        resp = client.post("/personas/agent/turn", json={"answer": "what is the weather today"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is False
    assert body["intent"] == "unknown"
    mock_apply.assert_not_called()


# ── /agent/turn — parity guard (forbidden knob) ──────────────────────────────


def test_agent_turn_forbidden_knob_no_write(client: TestClient) -> None:
    """A knob key not in the parity-safe list → no write, applied=False.

    StyleKnobs has extra='forbid' — 'effect' is not a parity-safe key.
    The route validates knobs through StyleKnobs before any DB write.
    """
    user = _fake_user()
    row = _persona_row(user.id, style={"status": "ready"})
    db = _async_db(scalar_result=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    # "effect" is NOT a parity-safe knob key
    intent_result = _make_intent_result(
        intent="style_edit",
        fields={"knobs": {"effect": "glitch", "text_size_px": 60}},
        reply="Done.",
    )

    with (
        patch("app.config.settings", _settings()),
        patch("app.routes.personas.asyncio") as mock_asyncio,
    ):
        # Agent returns effect + text_size_px; the route must catch effect via StyleKnobs
        mock_asyncio.to_thread = AsyncMock(return_value=intent_result)

        resp = client.post("/personas/agent/turn", json={"answer": "add glitch effect"})

    assert resp.status_code == 200
    body = resp.json()
    # "effect" is not parity-safe → StyleKnobs extra="forbid" rejects it → no write
    assert body["applied"] is False


# ── GET /personas/style — lazy-trigger ───────────────────────────────────────


def test_get_style_no_style_returns_deriving(client: TestClient) -> None:
    """When persona exists but style is NULL, GET /personas/style returns
    status='deriving' (not 'absent') so the StyleCard shows the loading skeleton
    rather than the set-up CTA."""
    user = _fake_user()
    row = _persona_row(user.id, style=None)
    db = _async_db(scalar_result=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    # The route lazily imports + calls derive_user_style.delay inside try/except,
    # so we patch the whole module import to return a no-op task.
    fake_task = MagicMock()
    fake_task.delay = MagicMock()
    fake_style_build = MagicMock()
    fake_style_build.derive_user_style = fake_task

    with (
        patch("app.config.settings", _settings()),
        patch.dict("sys.modules", {"app.tasks.style_build": fake_style_build}),
    ):
        resp = client.get("/personas/style")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "deriving"
    assert body["style"] is None


# ── /agent/turn — regression: px-clamp + false-Done + read-back (2026-06-07) ─


def test_agent_turn_style_edit_small_px_clamped_to_40(client: TestClient) -> None:
    """Agent emits text_size_px=14 (CSS scale) → route clamps to 40, applied=True.

    Root cause of the 2026-06-07 prod 500: StyleKnobs._clamp_px silently clamps
    14→40 (passes), but StyleKnobsEdit(ge=40) was constructed from the RAW value
    and raised outside any try/except → unhandled 500.
    Fix: build StyleKnobsEdit from the clamped output, never from raw_knobs.
    Pinned by this test — must fail on the unfixed route.
    """
    user = _fake_user()
    row = _persona_row(user.id, style={"status": "ready", "knobs": {}})
    db = _async_db(scalar_result=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    intent_result = _make_intent_result(
        intent="style_edit",
        fields={"knobs": {"text_size_px": 14}},  # CSS px — below 40px floor
        reply="Done — your text is now smaller.",
    )

    with (
        patch("app.config.settings", _settings()),
        patch("app.routes.personas.asyncio") as mock_asyncio,
        patch("app.routes.personas._apply_style_edit", new_callable=AsyncMock) as mock_apply,
    ):
        mock_asyncio.to_thread = AsyncMock(return_value=intent_result)
        mock_apply.return_value = {"status": "edited", "knobs": {"text_size_px": 40}}

        resp = client.post("/personas/agent/turn", json={"answer": "I like using small texts"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    mock_apply.assert_called_once()
    # Reply must contain an honest clamp note.
    assert "40" in body["reply"], f"Clamp note missing from reply: {body['reply']!r}"


def test_agent_turn_style_edit_free_text_only_no_write(client: TestClient) -> None:
    """style_edit with only free_text and no structured field → applied=False, no DB write.

    Root cause of the 2026-06-07 prod false 'Done': the agent returned only
    free_text (no knobs/style_set_id/etc.), _apply_style_edit was called anyway,
    wrote nothing except status='edited', and applied=True was set → frontend
    showed 'Done' when nothing size-related was stored.
    Fix: materiality check before _apply_style_edit.
    """
    user = _fake_user()
    row = _persona_row(user.id, style={"status": "ready", "knobs": {}})
    db = _async_db(scalar_result=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    # Only free_text — no structured knob or field.
    intent_result = _make_intent_result(
        intent="style_edit",
        fields={"free_text": "aesthetic and not to cover the content"},
        reply="Got it — I'll keep it aesthetic.",
    )

    with (
        patch("app.config.settings", _settings()),
        patch("app.routes.personas.asyncio") as mock_asyncio,
        patch("app.routes.personas._apply_style_edit", new_callable=AsyncMock) as mock_apply,
    ):
        mock_asyncio.to_thread = AsyncMock(return_value=intent_result)

        resp = client.post(
            "/personas/agent/turn",
            json={"answer": "I want it to look aesthetic and not to cover the content"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False, "Expected applied=False for a free_text-only style_edit"
    mock_apply.assert_not_called()


def test_agent_turn_describe_intent_no_write(client: TestClient) -> None:
    """describe intent → agent reply is passed through, no DB write, applied=False."""
    user = _fake_user()
    row = _persona_row(
        user.id,
        style={
            "status": "edited",
            "knobs": {"text_size_px": 40, "font_family": "Playfair Display"},
        },
    )
    db = _async_db(scalar_result=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    intent_result = _make_intent_result(
        intent="describe",
        reply="Your text size is 40px and font is Playfair Display.",
        fields={},
    )

    with (
        patch("app.config.settings", _settings()),
        patch("app.routes.personas.asyncio") as mock_asyncio,
        patch("app.routes.personas._apply_style_edit", new_callable=AsyncMock) as mock_apply,
    ):
        mock_asyncio.to_thread = AsyncMock(return_value=intent_result)

        resp = client.post("/personas/agent/turn", json={"answer": "What is it set to right now?"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert body["intent"] == "describe"
    assert "40px" in body["reply"]
    mock_apply.assert_not_called()


def test_validator_parity_text_size_px_boundary_values() -> None:
    """StyleKnobs.model_validate + StyleKnobsEdit must agree on ALL boundary values.

    Any raw px that passes StyleKnobs.model_validate (after clamping) MUST be
    constructable via StyleKnobsEdit without raising. If this breaks, the route
    will 500 on out-of-range agent values.
    """
    from app.agents._schemas.user_style import StyleKnobs
    from app.routes.personas import StyleKnobsEdit

    for raw_px in [1, 14, 39, 40, 80, 81, 200]:
        validated = StyleKnobs.model_validate({"text_size_px": raw_px})
        clamped = validated.model_dump(exclude_none=True)
        # Must not raise — if it does, the route would 500.
        knobs_edit = StyleKnobsEdit(**clamped)
        assert knobs_edit.text_size_px is not None, f"raw={raw_px}: clamped px lost"
        assert 40 <= knobs_edit.text_size_px <= 80, (
            f"raw={raw_px}: clamped to {knobs_edit.text_size_px}, out of legal range"
        )
