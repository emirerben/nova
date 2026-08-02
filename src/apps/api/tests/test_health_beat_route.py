"""Tests for GET /health/beat.

External uptime monitors typically alert on a non-2xx status alone without
parsing the body — the status code is the actionable signal, so it's
asserted explicitly alongside the JSON body in every case.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_healthy_returns_200():
    with patch("app.services.beat_heartbeat.beat_heartbeat_status", return_value=(True, 45.0)):
        resp = _client().get("/health/beat")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["age_seconds"] == 45.0


def test_unhealthy_returns_503():
    with patch("app.services.beat_heartbeat.beat_heartbeat_status", return_value=(False, 1200.0)):
        resp = _client().get("/health/beat")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "stale"
    assert body["age_seconds"] == 1200.0


def test_never_recorded_returns_503_with_null_age():
    with patch("app.services.beat_heartbeat.beat_heartbeat_status", return_value=(False, None)):
        resp = _client().get("/health/beat")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "stale"
    assert body["age_seconds"] is None
