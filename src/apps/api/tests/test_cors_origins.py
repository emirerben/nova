from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_cors_allows_canonical_kria_origin() -> None:
    resp = _client().options(
        "/health",
        headers={
            "Origin": "https://usekria.com",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "https://usekria.com"


def test_cors_allows_temporary_legacy_vercel_origin() -> None:
    resp = _client().options(
        "/health",
        headers={
            "Origin": "https://nova-video.vercel.app",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "https://nova-video.vercel.app"


def test_cors_allows_vercel_preview_origin() -> None:
    resp = _client().options(
        "/health",
        headers={
            "Origin": "https://nova-git-branch-emirerbens-projects.vercel.app",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert resp.status_code == 200
    assert (
        resp.headers["access-control-allow-origin"]
        == "https://nova-git-branch-emirerbens-projects.vercel.app"
    )


def test_cors_rejects_unknown_origin() -> None:
    resp = _client().options(
        "/health",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert resp.status_code == 400
    assert "access-control-allow-origin" not in resp.headers
