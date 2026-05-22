"""Integration tests for PATCH /admin/templates/{id}/overlays.

The endpoint is the escape hatch when the Layer-2 cumulative-reveal
pipeline produces wrong text. Admin sends a list of edits, each pointing
at a specific overlay in `recipe_cached.slots[].text_overlays[]`. The
endpoint validates the entire list against the recipe, then applies all
edits atomically.

Coverage:
  - Happy path: single + multi-edit, sample_text + text both updated
  - Empty sample_text allowed (hides the overlay)
  - Validation: out-of-range slot, out-of-range overlay, no slots,
    missing recipe_cached, empty edit list
  - Auth: rejects bad token, rejects missing token
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import VideoTemplate

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture(autouse=True)
def _patch_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_TOKEN)
    from app.config import settings

    settings.admin_api_key = ADMIN_TOKEN


class _StubSession:
    """Minimal AsyncSession stand-in. The endpoint awaits commit/refresh
    and calls execute() once for the agent_run history query at the end.
    Every method is a no-op; the test verifies the route's mutation logic
    by reading recipe_cached on the (mock) template AFTER the endpoint
    runs, since the endpoint writes back to `template.recipe_cached`
    in place before commit.
    """

    async def commit(self) -> None:
        return None

    async def refresh(self, _obj) -> None:
        return None

    async def execute(self, _stmt):
        return _StubResult()


class _StubResult:
    def scalars(self):
        return self

    def all(self):
        return []


async def _stub_get_db() -> AsyncGenerator[_StubSession, None]:
    """FastAPI expects an async generator from get_db. A plain async fn
    or a sync lambda returning a session does NOT satisfy the contract —
    that's how an earlier iteration ended up calling refresh on a real
    sqlalchemy session and tripping UnmappedInstanceError on the
    MagicMock template.
    """
    yield _StubSession()


@pytest.fixture()
def client() -> TestClient:
    app.dependency_overrides[get_db] = _stub_get_db
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _headers() -> dict:
    return {"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"}


def _bad_token_headers() -> dict:
    return {"X-Admin-Token": "wrong-token", "Content-Type": "application/json"}


def _template_with_overlays() -> VideoTemplate:
    """Build a template whose recipe_cached has 2 slots, each with 2
    overlays. Slot 1's overlays carry both `sample_text` and `text` so
    the test can verify the legacy-field-sync behavior; slot 2's
    overlays carry only `sample_text` so the test can verify the route
    doesn't introduce a spurious `text` key when none existed.
    """
    t = MagicMock(spec=VideoTemplate)
    t.id = "tpl-overlay-001"
    t.name = "Overlay Test"
    t.gcs_path = "templates/test/video.mp4"
    t.template_type = "standard"
    t.is_agentic = True
    t.analysis_status = "ready"
    t.audio_gcs_path = None
    t.music_track_id = None
    t.error_detail = None
    t.recipe_cached_at = datetime.now(UTC)
    t.created_at = datetime.now(UTC)
    t.recipe_cached = {
        "slots": [
            {
                "target_duration_s": 2.0,
                "text_overlays": [
                    {"sample_text": "It's", "text": "It's", "start_s": 0.0, "end_s": 0.5},
                    {"sample_text": "not", "text": "not", "start_s": 0.5, "end_s": 1.0},
                ],
            },
            {
                "target_duration_s": 2.0,
                "text_overlays": [
                    {"sample_text": 'luck"', "start_s": 0.0, "end_s": 0.5},
                    {"sample_text": "W", "start_s": 0.5, "end_s": 1.0},
                ],
            },
        ],
    }
    return t


def _patch_get_template(template):
    return patch("app.routes.admin.get_template_or_404", new=AsyncMock(return_value=template))


# ── Auth ─────────────────────────────────────────────────────────────────────


def test_missing_admin_token_is_rejected(client: TestClient) -> None:
    """No X-Admin-Token header → FastAPI returns 422 (Header(...) required)."""
    res = client.patch(
        "/admin/templates/tpl-overlay-001/overlays",
        json={"edits": [{"slot_index": 0, "overlay_index": 0, "sample_text": "hello"}]},
    )
    assert res.status_code in (401, 422), res.text


def test_bad_admin_token_is_rejected(client: TestClient) -> None:
    """Wrong token value → 401."""
    res = client.patch(
        "/admin/templates/tpl-overlay-001/overlays",
        headers=_bad_token_headers(),
        json={"edits": [{"slot_index": 0, "overlay_index": 0, "sample_text": "hello"}]},
    )
    assert res.status_code == 401, res.text


# ── Happy path ───────────────────────────────────────────────────────────────


def test_single_edit_updates_sample_text_and_text(client: TestClient) -> None:
    """A single edit rewrites both `sample_text` AND the legacy `text`
    field (when the overlay carries one). The renderer's fallback at
    template_orchestrate._resolve_overlay_text reads `text` as a
    fallback for `sample_text`, so the two MUST stay in sync after edit.
    """
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={
                "edits": [
                    {
                        "slot_index": 0,
                        "overlay_index": 0,
                        "sample_text": "It's not just luck",
                    }
                ]
            },
        )
    assert res.status_code == 200, res.text
    overlay = res.json()["recipe_cached"]["slots"][0]["text_overlays"][0]
    assert overlay["sample_text"] == "It's not just luck"
    assert overlay["text"] == "It's not just luck"
    # Untouched neighbor still carries its original text.
    assert res.json()["recipe_cached"]["slots"][0]["text_overlays"][1]["sample_text"] == "not"


def test_edit_does_not_introduce_text_key_when_absent(client: TestClient) -> None:
    """Slot 1's overlays have only `sample_text`. Editing must NOT add a
    new `text` key — that would pollute the JSONB shape and downstream
    consumers expecting the canonical sample_text-only shape would see
    a phantom field.
    """
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={"edits": [{"slot_index": 1, "overlay_index": 0, "sample_text": "luck"}]},
        )
    assert res.status_code == 200, res.text
    overlay = res.json()["recipe_cached"]["slots"][1]["text_overlays"][0]
    assert overlay["sample_text"] == "luck"
    assert "text" not in overlay, f"unexpected legacy `text` key added: {overlay!r}"


def test_multiple_edits_apply_atomically(client: TestClient) -> None:
    """A 3-edit request rewrites three different overlays in one
    transaction. Validates the JSONB change detection works for nested
    list mutations (the route copies dicts at each nesting level so
    SQLAlchemy sees a new object reference).
    """
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={
                "edits": [
                    {"slot_index": 0, "overlay_index": 0, "sample_text": "It's"},
                    {"slot_index": 0, "overlay_index": 1, "sample_text": "It's not"},
                    {
                        "slot_index": 1,
                        "overlay_index": 0,
                        "sample_text": "It's not just luck",
                    },
                ]
            },
        )
    assert res.status_code == 200, res.text
    slots = res.json()["recipe_cached"]["slots"]
    assert slots[0]["text_overlays"][0]["sample_text"] == "It's"
    assert slots[0]["text_overlays"][1]["sample_text"] == "It's not"
    assert slots[1]["text_overlays"][0]["sample_text"] == "It's not just luck"


def test_empty_sample_text_is_allowed(client: TestClient) -> None:
    """Empty `sample_text` is a legitimate edit — effectively hides the
    overlay because the renderer skips empty strings. The schema
    validator must not reject the empty string at parse time.
    """
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={"edits": [{"slot_index": 1, "overlay_index": 1, "sample_text": ""}]},
        )
    assert res.status_code == 200, res.text
    assert res.json()["recipe_cached"]["slots"][1]["text_overlays"][1]["sample_text"] == ""


# ── Validation (422 / 409) ───────────────────────────────────────────────────


def test_out_of_range_slot_index_rejected(client: TestClient) -> None:
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={"edits": [{"slot_index": 99, "overlay_index": 0, "sample_text": "x"}]},
        )
    assert res.status_code == 422
    assert "slot_index=99 out of range" in res.text


def test_out_of_range_overlay_index_rejected(client: TestClient) -> None:
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={"edits": [{"slot_index": 0, "overlay_index": 99, "sample_text": "x"}]},
        )
    assert res.status_code == 422
    assert "overlay_index=99 out of range" in res.text


def test_validation_failure_on_one_edit_leaves_recipe_untouched(client: TestClient) -> None:
    """Atomicity: when ANY edit fails validation, NO edits apply. The
    pre-validation pass collects all mutations first, then commits — a
    bad edit at index 2 must not let edits 0-1 land in the DB.
    """
    template = _template_with_overlays()
    original_first = template.recipe_cached["slots"][0]["text_overlays"][0]["sample_text"]
    original_second = template.recipe_cached["slots"][0]["text_overlays"][1]["sample_text"]
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={
                "edits": [
                    {"slot_index": 0, "overlay_index": 0, "sample_text": "FIRST"},
                    {"slot_index": 0, "overlay_index": 1, "sample_text": "SECOND"},
                    {"slot_index": 99, "overlay_index": 0, "sample_text": "OOPS"},
                ]
            },
        )
    assert res.status_code == 422
    # template.recipe_cached on the mock was never reassigned because the
    # route raises BEFORE the mutation loop runs.
    assert template.recipe_cached["slots"][0]["text_overlays"][0]["sample_text"] == original_first
    assert template.recipe_cached["slots"][0]["text_overlays"][1]["sample_text"] == original_second


def test_recipe_with_no_slots_rejected(client: TestClient) -> None:
    """Editing a template whose recipe has no slots returns 409, not a
    silent no-op. This catches the "tried to edit an unanalyzed template"
    case where the admin UI shouldn't have offered the editor anyway."""
    template = _template_with_overlays()
    template.recipe_cached = {"slots": []}
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={"edits": [{"slot_index": 0, "overlay_index": 0, "sample_text": "x"}]},
        )
    assert res.status_code == 409
    assert "no slots" in res.text


def test_missing_recipe_cached_rejected(client: TestClient) -> None:
    template = _template_with_overlays()
    template.recipe_cached = None
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={"edits": [{"slot_index": 0, "overlay_index": 0, "sample_text": "x"}]},
        )
    assert res.status_code == 409
    assert "no recipe" in res.text


def test_empty_edit_list_rejected_by_validator(client: TestClient) -> None:
    """Zero-edit requests are a schema violation (min_length=1) — return
    422 before the route runs. Avoids spurious commits on accidental
    empty requests from the admin UI.
    """
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={"edits": []},
        )
    assert res.status_code == 422
