"""Tests for routes/admin_review.py — the dev-loop video-review surface (T6).

The read side (GET /admin/review) filters the grader's AgentRun rows to the
`escalate` band, signs thumbnails, and reports which jobs are already labeled.
The write side (POST /admin/review/{run_id}/label) persists a calibration label
as a sibling AgentRun row.

Offline by construction: `sync_session` is monkeypatched to a fake session that
returns canned rows, and `signed_get_url` / `persist_agent_run` are stubbed —
no Postgres, no GCS, no network. This exercises the route's band-filter,
thumbnail-join, labeled-flag, and verdict-normalization logic directly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.config as config_mod
import app.routes.admin_review as review_mod
from app.main import app

VALID_TOKEN = "test-admin-token"


# ── Fakes ────────────────────────────────────────────────────────────────────


@dataclass
class FakeAgentRun:
    id: uuid.UUID
    job_id: uuid.UUID | None
    agent_name: str
    output_json: dict[str, Any] | None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class _Result:
    """Minimal stand-in for a SQLAlchemy Result."""

    def __init__(self, rows: list[Any], *, scalar_rows: list[Any] | None = None):
        self._rows = rows
        self._scalar_rows = scalar_rows if scalar_rows is not None else rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._scalar_rows[0] if self._scalar_rows else None


class FakeSession:
    """A scripted session: each execute() pops the next canned Result.

    The route issues, in order: grader-rows query, label-job-ids query, then one
    clip-paths query per escalate row. The list of results is supplied per test.
    For the label endpoint it issues a single grade-lookup query.
    """

    def __init__(self, results: list[_Result]):
        self._results = list(results)
        self.executed = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _stmt):
        self.executed += 1
        if self._results:
            return self._results.pop(0)
        return _Result([])


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "admin_api_key", VALID_TOKEN, raising=False)
    return TestClient(app, raise_server_exceptions=False)


def _hdr():
    return {"X-Admin-Token": VALID_TOKEN}


def _grade_row(band: str, job_id: uuid.UUID | None = None, **out) -> FakeAgentRun:
    base = {
        "band": band,
        "avg": 3.2,
        "confidence": 0.55,
        "risk_tag": "borderline",
        "reasoning": "hook is slow to land",
        "summary_line": f"[{band}] avg=3.20",
        "scores": {"hook": 3.0, "legibility": 4.0, "filmed_not_templated": 2.5},
    }
    base.update(out)
    return FakeAgentRun(
        id=uuid.uuid4(),
        job_id=job_id,
        agent_name=review_mod.GRADER_AGENT_NAME,
        output_json=base,
    )


def _install_session(monkeypatch, results: list[_Result]) -> FakeSession:
    sess = FakeSession(results)

    def _factory():
        return sess

    import app.database as db_mod

    monkeypatch.setattr(db_mod, "sync_session", _factory, raising=False)
    return sess


# ── Auth ─────────────────────────────────────────────────────────────────────


class TestAuth:
    def test_missing_token_rejected(self, client):
        res = client.get("/admin/review")
        assert res.status_code in (401, 422)

    def test_wrong_token_rejected(self, client):
        res = client.get("/admin/review", headers={"X-Admin-Token": "nope"})
        assert res.status_code == 401


# ── GET /admin/review (the render test) ──────────────────────────────────────


class TestListReview:
    def test_only_escalate_band_is_surfaced(self, client, monkeypatch):
        """auto_pass / auto_reject grades are filtered out; only escalate shows."""
        job_a, job_b, job_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        grader_rows = [
            _grade_row("auto_pass", job_a),
            _grade_row("escalate", job_b),
            _grade_row("auto_reject", job_c),
        ]
        # results: grader rows, label-job-ids (none), then one clip-path query
        # for the single escalate row.
        _install_session(
            monkeypatch,
            [
                _Result(grader_rows),
                _Result([]),  # no labels yet
                _Result([("music-jobs/b/thumb.jpg", "music-jobs/b/final.mp4")]),
            ],
        )
        monkeypatch.setattr(
            "app.storage.signed_get_url",
            lambda path, expiration_minutes=60: f"https://signed/{path}",
        )

        res = client.get("/admin/review", headers=_hdr())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["band"] == "escalate"
        assert item["job_id"] == str(job_b)
        # per-dimension scores survive the round-trip
        assert item["scores"]["hook"] == 3.0
        assert item["reasoning"] == "hook is slow to land"
        assert item["risk_tag"] == "borderline"
        # thumbnail + video signed from the rendered clip paths
        assert item["thumbnail_url"] == "https://signed/music-jobs/b/thumb.jpg"
        assert item["video_url"] == "https://signed/music-jobs/b/final.mp4"
        assert item["labeled"] is False

    def test_labeled_flag_set_when_label_exists(self, client, monkeypatch):
        job = uuid.uuid4()
        _install_session(
            monkeypatch,
            [
                _Result([_grade_row("escalate", job)]),
                _Result([(job,)]),  # a label row already references this job
                _Result([(None, None)]),  # no rendered clip
            ],
        )
        monkeypatch.setattr(
            "app.storage.signed_get_url",
            lambda path, expiration_minutes=60: f"https://signed/{path}",
        )
        res = client.get("/admin/review", headers=_hdr())
        assert res.status_code == 200, res.text
        item = res.json()["items"][0]
        assert item["labeled"] is True
        # no clip → no signed URLs, but the card still renders
        assert item["thumbnail_url"] is None
        assert item["video_url"] is None

    def test_empty_queue_returns_no_items(self, client, monkeypatch):
        _install_session(monkeypatch, [_Result([]), _Result([])])
        res = client.get("/admin/review", headers=_hdr())
        assert res.status_code == 200
        assert res.json() == {"items": [], "total": 0}

    def test_thumbnail_sign_failure_does_not_500(self, client, monkeypatch):
        """A storage hiccup on one thumbnail yields None, not a 500 on the queue."""
        job = uuid.uuid4()
        _install_session(
            monkeypatch,
            [
                _Result([_grade_row("escalate", job)]),
                _Result([]),
                _Result([("music-jobs/x/thumb.jpg", None)]),
            ],
        )

        def _boom(path, expiration_minutes=60):
            raise RuntimeError("GCS down")

        monkeypatch.setattr("app.storage.signed_get_url", _boom)
        res = client.get("/admin/review", headers=_hdr())
        assert res.status_code == 200, res.text
        assert res.json()["items"][0]["thumbnail_url"] is None


# ── POST /admin/review/{run_id}/label (the calibration write) ─────────────────


class TestLabelReview:
    def _install_label_capture(self, monkeypatch, grade_row: FakeAgentRun | None):
        captured: dict[str, Any] = {}

        def _fake_persist(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(review_mod, "_normalize_verdict", review_mod._normalize_verdict)
        # session returns the grade row for the lookup
        _install_session(monkeypatch, [_Result([grade_row] if grade_row else [])])
        monkeypatch.setattr("app.agents._persistence.persist_agent_run", _fake_persist)
        return captured

    def test_label_persists_calibration_row(self, client, monkeypatch):
        job = uuid.uuid4()
        grade = _grade_row("escalate", job)
        captured = self._install_label_capture(monkeypatch, grade)

        res = client.post(
            f"/admin/review/{grade.id}/label",
            json={"verdict": "auto_pass", "note": "actually fine"},
            headers=_hdr(),
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["verdict"] == "auto_pass"
        assert body["job_id"] == str(job)
        # a sibling AgentRun row was written under the LABEL agent name
        assert captured["agent_name"] == review_mod.GRADER_LABEL_AGENT_NAME
        assert captured["job_id"] == str(job)
        assert captured["output_dict"]["verdict"] == "auto_pass"
        assert captured["output_dict"]["note"] == "actually fine"
        assert captured["input_dict"]["grade_run_id"] == str(grade.id)

    def test_label_auto_reject_round_trips(self, client, monkeypatch):
        job = uuid.uuid4()
        grade = _grade_row("escalate", job)
        captured = self._install_label_capture(monkeypatch, grade)
        res = client.post(
            f"/admin/review/{grade.id}/label",
            json={"verdict": "auto_reject"},
            headers=_hdr(),
        )
        assert res.status_code == 201, res.text
        assert res.json()["verdict"] == "auto_reject"
        assert captured["output_dict"]["verdict"] == "auto_reject"

    def test_label_unknown_run_is_404(self, client, monkeypatch):
        self._install_label_capture(monkeypatch, None)
        res = client.post(
            f"/admin/review/{uuid.uuid4()}/label",
            json={"verdict": "auto_pass"},
            headers=_hdr(),
        )
        assert res.status_code == 404

    def test_label_non_uuid_run_is_404(self, client, monkeypatch):
        self._install_label_capture(monkeypatch, None)
        res = client.post(
            "/admin/review/not-a-uuid/label",
            json={"verdict": "auto_pass"},
            headers=_hdr(),
        )
        assert res.status_code == 404

    def test_label_invalid_verdict_is_422(self, client, monkeypatch):
        job = uuid.uuid4()
        grade = _grade_row("escalate", job)
        self._install_label_capture(monkeypatch, grade)
        res = client.post(
            f"/admin/review/{grade.id}/label",
            json={"verdict": "maybe"},
            headers=_hdr(),
        )
        assert res.status_code == 422


# ── _normalize_verdict (pure) ────────────────────────────────────────────────


class TestNormalizeVerdict:
    def test_explicit_pass_reject_pass_through(self):
        assert review_mod._normalize_verdict("auto_pass", "escalate") == "auto_pass"
        assert review_mod._normalize_verdict("auto_reject", "escalate") == "auto_reject"

    def test_agree_on_escalate_records_escalate(self):
        # An agree/disagree on an abstain carries no side.
        assert review_mod._normalize_verdict("agree", "escalate") == "escalate"
        assert review_mod._normalize_verdict("disagree", "escalate") == "escalate"

    def test_relative_against_decided_band(self):
        assert review_mod._normalize_verdict("agree", "auto_pass") == "auto_pass"
        assert review_mod._normalize_verdict("disagree", "auto_pass") == "auto_reject"
        assert review_mod._normalize_verdict("agree", "auto_reject") == "auto_reject"
        assert review_mod._normalize_verdict("disagree", "auto_reject") == "auto_pass"
