from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

# Registered once at import; TestClient instances share the app, matching the
# pattern in test_cors_origins.py. The route exists only in tests — it gives a
# deterministic >1KB body without touching the DB.
_LARGE_BODY = {"payload": "x" * 4096}


@app.get("/_test/gzip-large", include_in_schema=False)
def _gzip_large() -> dict[str, str]:
    return _LARGE_BODY


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_large_json_is_gzipped_when_client_accepts() -> None:
    # Regression pin for the job-status poll payloads (~130KB every 2s per
    # client): the API must compress large JSON bodies itself — nothing between
    # uvicorn and the Vercel proxy does it otherwise.
    resp = _client().get("/_test/gzip-large", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"
    assert resp.json() == _LARGE_BODY


def test_small_body_stays_identity() -> None:
    resp = _client().get("/health", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers


def test_no_accept_encoding_stays_identity() -> None:
    resp = _client().get("/_test/gzip-large", headers={"Accept-Encoding": "identity"})

    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers
