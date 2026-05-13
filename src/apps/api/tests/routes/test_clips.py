"""Integration tests for routes/clips.py — POST /clips/prefetch-analyze.

We stub schedule_prefetch out so these tests cover only the endpoint's
contract: validation, template lookup, and the response shape. The
service-layer behaviour (dedup, cache check, Gemini calls) is covered
in tests/services/test_clip_prefetch.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import VideoTemplate

_VALID_PATH = "00000000-0000-0000-0000-000000000001/batch-abc123def456/clip_001.mp4"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _db_with_template(template: object | None):
    """get_db override returning a session whose `.execute().scalar_one_or_none()`
    yields the given template (or None for 'not found' paths)."""

    async def _gen():
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = template
        db.execute = AsyncMock(return_value=result)
        yield db

    return _gen


def _template(
    *,
    recipe: dict | None = None,
) -> MagicMock:
    """A minimal VideoTemplate mock with the fields the prefetch route reads."""
    t = MagicMock(spec=VideoTemplate)
    t.id = "tmpl-abc"
    t.recipe_cached = recipe
    return t


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_invalid_path_returns_422(client: TestClient) -> None:
    # Don't even need a template override — the path validator fires before
    # the DB lookup.
    app.dependency_overrides[get_db] = _db_with_template(_template())
    res = client.post(
        "/clips/prefetch-analyze",
        json={"gcs_path": "../../etc/passwd", "template_id": "tmpl-abc"},
    )
    assert res.status_code == 422


def test_missing_template_returns_404(client: TestClient) -> None:
    app.dependency_overrides[get_db] = _db_with_template(None)
    res = client.post(
        "/clips/prefetch-analyze",
        json={"gcs_path": _VALID_PATH, "template_id": "does-not-exist"},
    )
    assert res.status_code == 404


def test_happy_path_returns_202_enqueued(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.dependency_overrides[get_db] = _db_with_template(
        _template(recipe={"clip_filter_hint": "ball in frame", "slots": []}),
    )
    # Stub the service so the test doesn't accidentally start a real Gemini
    # upload in the background.
    schedule_stub = AsyncMock(return_value=True)
    monkeypatch.setattr("app.routes.clips.schedule_prefetch", schedule_stub)

    res = client.post(
        "/clips/prefetch-analyze",
        json={"gcs_path": _VALID_PATH, "template_id": "tmpl-abc"},
    )
    assert res.status_code == 202
    body = res.json()
    assert body["status"] == "enqueued"
    assert body["duplicate"] is False
    # Filter hint must be forwarded to the service — that's what makes the
    # eventual orchestrator cache hit possible.
    schedule_stub.assert_called_once()
    args, _ = schedule_stub.call_args
    assert args[0] == _VALID_PATH
    assert args[1] == "ball in frame"


def test_duplicate_returns_202_with_duplicate_flag(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-firing the same gcs_path doesn't error; the response just flags
    it so the frontend can suppress retries."""
    app.dependency_overrides[get_db] = _db_with_template(
        _template(recipe={"clip_filter_hint": "", "slots": []}),
    )
    monkeypatch.setattr(
        "app.routes.clips.schedule_prefetch", AsyncMock(return_value=False),
    )
    res = client.post(
        "/clips/prefetch-analyze",
        json={"gcs_path": _VALID_PATH, "template_id": "tmpl-abc"},
    )
    assert res.status_code == 202
    assert res.json() == {"status": "duplicate", "duplicate": True}


def test_mixed_media_template_skips_without_scheduling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Photo slots bypass Gemini in the orchestrator. Prefetching them
    would burn quota for nothing — the route must short-circuit before
    calling the service."""
    app.dependency_overrides[get_db] = _db_with_template(
        _template(
            recipe={
                "clip_filter_hint": "",
                "slots": [{"media_type": "photo"}, {"media_type": "video"}],
            },
        ),
    )
    schedule_stub = AsyncMock(return_value=True)
    monkeypatch.setattr("app.routes.clips.schedule_prefetch", schedule_stub)
    res = client.post(
        "/clips/prefetch-analyze",
        json={"gcs_path": _VALID_PATH, "template_id": "tmpl-abc"},
    )
    assert res.status_code == 202
    assert res.json()["status"] == "skipped_mixed_media"
    schedule_stub.assert_not_called()


def test_template_without_recipe_uses_empty_filter_hint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template that hasn't been analysed yet has recipe_cached=None.
    Treat it like an empty filter_hint — the orchestrator falls back the
    same way. This matters during the brief window after template creation
    but before analyze_template_task finishes."""
    app.dependency_overrides[get_db] = _db_with_template(_template(recipe=None))
    schedule_stub = AsyncMock(return_value=True)
    monkeypatch.setattr("app.routes.clips.schedule_prefetch", schedule_stub)
    res = client.post(
        "/clips/prefetch-analyze",
        json={"gcs_path": _VALID_PATH, "template_id": "tmpl-abc"},
    )
    assert res.status_code == 202
    args, _ = schedule_stub.call_args
    assert args[1] == ""  # default filter hint
