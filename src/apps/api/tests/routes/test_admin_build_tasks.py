"""Tests for routes/admin_build_tasks.py — the build_task admin API.

The endpoints the GitHub Actions builder calls (claim / checkpoint / complete /
release / fail) plus the mint endpoint that enforces the v1 security invariant
(untrusted provenance → 403). Uses a real Postgres test DB so the SKIP-LOCKED
claim and the repo transitions run for real end-to-end through the HTTP layer.
Skips when Postgres is unreachable.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.main import app
from app.models import BuildTask

VALID_TOKEN = "test-admin-token"

_DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nova_test"
).replace("postgresql+asyncpg://", "postgresql://")


def _engine_or_skip():
    try:
        eng = create_engine(_DB_URL, pool_pre_ping=True)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return eng
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable for admin_build_tasks tests: {exc!r}")


@pytest.fixture(scope="module")
def engine():
    eng = _engine_or_skip()
    BuildTask.__table__.create(bind=eng, checkfirst=True)
    return eng


@pytest.fixture(autouse=True)
def _truncate(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE build_task"))
    yield


@pytest.fixture()
def client(engine, monkeypatch):
    # The route handlers open their own sync_session via app.database; point its
    # sync engine at the test DB and set the admin token.
    import app.config as config_mod
    import app.database as db_mod

    monkeypatch.setattr(config_mod.settings, "admin_api_key", VALID_TOKEN, raising=False)
    # Both the admin route module and the music admin module read settings; the
    # _require_admin gate compares against settings.admin_api_key.
    monkeypatch.setattr(db_mod, "sync_engine", engine, raising=False)

    def _test_session() -> Session:
        return Session(engine, expire_on_commit=False)

    monkeypatch.setattr(db_mod, "sync_session", _test_session, raising=False)
    return TestClient(app, raise_server_exceptions=False)


def _hdr():
    return {"X-Admin-Token": VALID_TOKEN}


# ── Auth ──────────────────────────────────────────────────────────────────────


class TestAuth:
    def test_missing_token_rejected(self, client):
        res = client.get("/admin/build-tasks")
        assert res.status_code in (401, 422)

    def test_wrong_token_rejected(self, client):
        res = client.get("/admin/build-tasks", headers={"X-Admin-Token": "nope"})
        assert res.status_code == 401


# ── Mint + security invariant ─────────────────────────────────────────────────


class TestMintSecurity:
    def test_trusted_mint_creates_queued_task(self, client):
        res = client.post(
            "/admin/build-tasks",
            json={"title": "fix overlay clip", "provenance": "trusted"},
            headers=_hdr(),
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["status"] == "queued"
        assert body["provenance"] == "trusted"
        assert body["title"] == "fix overlay clip"

    def test_untrusted_mint_is_forbidden(self, client):
        """CEO D3 at the HTTP boundary: untrusted provenance → 403, no row."""
        res = client.post(
            "/admin/build-tasks",
            json={"title": "from a reddit comment", "provenance": "untrusted"},
            headers=_hdr(),
        )
        assert res.status_code == 403, res.text
        # And the listing shows nothing was minted.
        listing = client.get("/admin/build-tasks", headers=_hdr())
        assert listing.json()["items"] == []

    def test_default_provenance_is_trusted(self, client):
        res = client.post(
            "/admin/build-tasks", json={"title": "no provenance given"}, headers=_hdr()
        )
        assert res.status_code == 201
        assert res.json()["provenance"] == "trusted"


# ── Claim / checkpoint / complete / release lifecycle ─────────────────────────


class TestLifecycle:
    def _mint(self, client, title="t", priority=100):
        res = client.post(
            "/admin/build-tasks",
            json={"title": title, "priority": priority},
            headers=_hdr(),
        )
        assert res.status_code == 201
        return res.json()

    def test_claim_empty_queue_returns_null(self, client):
        res = client.post("/admin/build-tasks/claim", json={"claimed_by": "run-1"}, headers=_hdr())
        assert res.status_code == 200
        assert res.json() is None

    def test_claim_returns_oldest_and_marks_in_progress(self, client):
        self._mint(client, title="only")
        res = client.post("/admin/build-tasks/claim", json={"claimed_by": "run-1"}, headers=_hdr())
        assert res.status_code == 200
        body = res.json()
        assert body is not None
        assert body["status"] == "in_progress"
        assert body["claimed_by"] == "run-1"

    def test_checkpoint_persists_progress(self, client):
        t = self._mint(client, title="resumable")
        client.post("/admin/build-tasks/claim", json={"claimed_by": "run-1"}, headers=_hdr())
        res = client.patch(
            f"/admin/build-tasks/{t['id']}",
            json={
                "action": "checkpoint",
                "stage": "stage-E",
                "progress_note": "3/5 done",
                "branch": "builder/x",
            },
            headers=_hdr(),
        )
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "in_progress"  # unchanged
        assert body["stage"] == "stage-E"
        assert body["progress_note"] == "3/5 done"
        assert body["branch"] == "builder/x"

    def test_release_soft_exit_keeps_task_resumable(self, client):
        """Soft-exit-on-limit through the API: release → queued, attempt NOT bumped."""
        t = self._mint(client, title="limited")
        client.post("/admin/build-tasks/claim", json={"claimed_by": "run-1"}, headers=_hdr())
        res = client.patch(
            f"/admin/build-tasks/{t['id']}",
            json={"action": "release", "progress_note": "paused on usage limit"},
            headers=_hdr(),
        )
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "queued"  # NOT failed/blocked
        assert body["attempt_count"] == 0  # NOT bumped
        # Immediately re-claimable.
        again = client.post(
            "/admin/build-tasks/claim", json={"claimed_by": "run-2"}, headers=_hdr()
        )
        assert again.json()["id"] == t["id"]

    def test_complete_marks_done_and_is_idempotent(self, client):
        t = self._mint(client, title="finish-me")
        client.post("/admin/build-tasks/claim", json={"claimed_by": "run-1"}, headers=_hdr())
        res = client.patch(
            f"/admin/build-tasks/{t['id']}",
            json={"action": "complete", "progress_note": "PR ready"},
            headers=_hdr(),
        )
        assert res.json()["status"] == "done"
        # A done task is not handed out again.
        claim = client.post(
            "/admin/build-tasks/claim", json={"claimed_by": "run-2"}, headers=_hdr()
        )
        assert claim.json() is None

    def test_fail_bumps_attempt_count(self, client):
        t = self._mint(client, title="flaky")
        client.post("/admin/build-tasks/claim", json={"claimed_by": "run-1"}, headers=_hdr())
        res = client.patch(
            f"/admin/build-tasks/{t['id']}",
            json={"action": "fail", "progress_note": "claude exited 1"},
            headers=_hdr(),
        )
        body = res.json()
        assert body["attempt_count"] == 1
        assert body["status"] == "queued"  # below cap → retryable

    def test_patch_unknown_task_404(self, client):
        res = client.patch(
            "/admin/build-tasks/00000000-0000-0000-0000-000000000000",
            json={"action": "checkpoint", "progress_note": "x"},
            headers=_hdr(),
        )
        assert res.status_code == 404

    def test_list_filters_by_status(self, client):
        self._mint(client, title="a")
        b = self._mint(client, title="b")
        client.post("/admin/build-tasks/claim", json={"claimed_by": "run-1"}, headers=_hdr())
        # 'a' was minted first (priority tie, created earlier) → it's the claimed one.
        queued = client.get("/admin/build-tasks?status=queued", headers=_hdr()).json()["items"]
        in_prog = client.get("/admin/build-tasks?status=in_progress", headers=_hdr()).json()[
            "items"
        ]
        assert len(queued) == 1
        assert len(in_prog) == 1
        assert {queued[0]["title"], in_prog[0]["title"]} == {"a", "b"}
        assert in_prog[0]["title"] == "a"  # oldest claimed
        _ = b
