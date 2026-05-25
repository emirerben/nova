"""Tests for routes/admin_generative.py — admin generative-edits overview.

Covers:
  - auth gate (X-Admin-Token)
  - tailored list shape: clip_count, per-variant summary
  - defensive variant filtering (malformed entries dropped)
  - empty/missing assembly_plan + all_candidates degrade gracefully
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app

VALID_TOKEN = "test-admin-token"


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _gen_job(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        status="variants_ready",
        error_detail=None,
        all_candidates={
            "clip_paths": ["slot-uploads/a.mp4", "slot-uploads/b.mp4"],
        },
        assembly_plan={
            "variants": [
                {
                    "variant_id": "song_lyrics",
                    "text_mode": "lyrics",
                    "track_title": "Some Song",
                    "render_status": "ready",
                    "ok": True,
                    "error": None,
                },
                {
                    "variant_id": "original_text",
                    "text_mode": "agent_text",
                    "track_title": None,
                    "render_status": "failed",
                    "ok": False,
                    "error": "boom",
                },
                # Malformed entries that must be dropped:
                {"text_mode": "agent_text"},  # no variant_id
                "not-a-dict",
            ]
        },
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _override_db_with(jobs):
    async def _gen():
        db = AsyncMock()
        rows_res = MagicMock()
        rows_res.scalars.return_value.all.return_value = jobs
        db.execute = AsyncMock(return_value=rows_res)
        yield db

    return _gen


class TestAuth:
    def test_missing_token(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            res = client.get("/admin/generative")
        assert res.status_code in (401, 422)

    def test_wrong_token(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            res = client.get("/admin/generative", headers={"X-Admin-Token": "nope"})
        assert res.status_code == 401


class TestList:
    def test_tailored_shape(self, client):
        job = _gen_job()
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db_with([job])
            try:
                res = client.get("/admin/generative", headers={"X-Admin-Token": VALID_TOKEN})
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["job_id"] == str(job.id)
        assert item["status"] == "variants_ready"
        assert item["clip_count"] == 2
        assert "target_duration_s" not in item  # slider removed; length is derived
        # Malformed variant entries dropped; valid ones preserved in order.
        assert [v["variant_id"] for v in item["variants"]] == [
            "song_lyrics",
            "original_text",
        ]
        assert item["variants"][1]["error"] == "boom"
        assert item["variants"][1]["ok"] is False

    def test_missing_jsonb_degrades(self, client):
        job = _gen_job(assembly_plan=None, all_candidates=None)
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db_with([job])
            try:
                res = client.get("/admin/generative", headers={"X-Admin-Token": VALID_TOKEN})
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        item = res.json()["items"][0]
        assert item["clip_count"] == 0
        assert item["variants"] == []

    def test_empty(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db_with([])
            try:
                res = client.get("/admin/generative", headers={"X-Admin-Token": VALID_TOKEN})
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 200
        body = res.json()
        assert body["items"] == []
        assert body["total"] == 0
