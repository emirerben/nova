"""Regression tests for the lyric-cache backfill helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[5]
_BACKFILL_PATH = _REPO_ROOT / "scripts" / "backfill_lyrics.py"
_SPEC = importlib.util.spec_from_file_location("backfill_lyrics", _BACKFILL_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
backfill_lyrics = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backfill_lyrics)


def test_http_request_uses_admin_token_header(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class FakeResponse:
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    def fake_urlopen(req, timeout):  # noqa: ANN001
        seen["timeout"] = timeout
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return FakeResponse()

    monkeypatch.setattr(backfill_lyrics.urllib.request, "urlopen", fake_urlopen)

    status, body = backfill_lyrics.http_request(
        method="GET",
        url="https://example.test/admin/music-tracks",
        token="secret-token",
        timeout_s=12.0,
    )

    assert status == 200
    assert body == {"ok": True}
    assert seen["timeout"] == 12.0
    assert seen["headers"]["x-admin-token"] == "secret-token"
    assert "authorization" not in seen["headers"]


def test_find_stale_tracks_uses_detail_cache_versions(monkeypatch) -> None:
    calls: list[str] = []
    base = "https://api.example.test"

    list_body = {
        "tracks": [
            {
                "id": "old",
                "title": "Old list title",
                "analysis_status": "ready",
                "published_at": "2026-05-01T00:00:00Z",
                "archived_at": None,
            },
            {
                "id": "current",
                "title": "Current list title",
                "analysis_status": "ready",
                "published_at": "2026-05-01T00:00:00Z",
                "archived_at": None,
            },
            {
                "id": "draft",
                "title": "Draft",
                "analysis_status": "ready",
                "published_at": None,
                "archived_at": None,
            },
            {
                "id": "queued",
                "title": "Queued",
                "analysis_status": "queued",
                "published_at": "2026-05-01T00:00:00Z",
                "archived_at": None,
            },
        ],
        "total": 4,
    }

    def fake_http_request(*, method, url, token, body=None, timeout_s=30.0):  # noqa: ANN001, ARG001
        calls.append(f"{method} {url}")
        if url == f"{base}/admin/music-tracks?limit=100&offset=0":
            return 200, list_body
        if url == f"{base}/admin/music-tracks/old":
            return 200, {
                "id": "old",
                "title": "Old detail title",
                "lyrics_cached": {"source": "lrclib_synced+whisper", "lines": []},
            }
        if url == f"{base}/admin/music-tracks/current":
            return 200, {
                "id": "current",
                "title": "Current detail title",
                "lyrics_cached": {"prompt_version": "target-version", "lines": []},
            }
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(backfill_lyrics, "http_request", fake_http_request)

    stale, total = backfill_lyrics.find_stale_tracks(
        base,
        token="secret-token",
        target="target-version",
    )

    assert total == 4
    assert stale == [("old", "Old detail title", None)]
    assert f"GET {base}/admin/music-tracks/draft" not in calls
    assert f"GET {base}/admin/music-tracks/queued" not in calls
