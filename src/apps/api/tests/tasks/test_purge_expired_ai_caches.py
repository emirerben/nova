from __future__ import annotations

from unittest.mock import MagicMock, patch

import app.database as database_mod
from app.tasks import maintenance
from app.tasks.maintenance import (
    _AI_CACHE_DELETE_BATCH,
    _AI_CACHE_DELETE_MAX_BATCHES,
    purge_expired_ai_caches,
)


class _FakeConn:
    def __init__(self, rowcounts: list[int]) -> None:
        self.rowcounts = list(rowcounts)
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, statement, params):  # noqa: ANN001
        self.calls.append((str(statement), dict(params)))
        result = MagicMock()
        result.rowcount = self.rowcounts.pop(0) if self.rowcounts else 0
        return result


def _run(rowcounts: list[int]):
    conn = _FakeConn(rowcounts)
    engine = MagicMock()
    engine.begin.return_value = conn
    with (
        patch.object(database_mod, "sync_engine", engine),
        patch("app.tasks.maintenance._purge_media_analysis_redis", return_value=4),
    ):
        result = purge_expired_ai_caches()
    return result, conn


def test_purge_expired_ai_caches_is_bounded_and_covers_both_tables() -> None:
    result, conn = _run([_AI_CACHE_DELETE_BATCH, 2, 3])

    assert result == {
        "director_review_cache": _AI_CACHE_DELETE_BATCH + 2,
        "media_analysis_cache": 3,
        "media_analysis_redis": 4,
    }
    assert len(conn.calls) == 3
    director_sql, params = conn.calls[0]
    media_sql, _ = conn.calls[-1]
    assert "DELETE FROM director_review_cache" in director_sql
    assert "DELETE FROM media_analysis_cache" in media_sql
    assert "expires_at <= now()" in director_sql
    assert "ORDER BY expires_at, id" in director_sql
    assert params == {"batch": _AI_CACHE_DELETE_BATCH}


def test_purge_expired_ai_caches_has_a_per_table_batch_fuse() -> None:
    full_batches = [_AI_CACHE_DELETE_BATCH] * (_AI_CACHE_DELETE_MAX_BATCHES * 2)
    result, conn = _run(full_batches)

    assert result == {
        "director_review_cache": _AI_CACHE_DELETE_BATCH * _AI_CACHE_DELETE_MAX_BATCHES,
        "media_analysis_cache": _AI_CACHE_DELETE_BATCH * _AI_CACHE_DELETE_MAX_BATCHES,
        "media_analysis_redis": 4,
    }
    assert len(conn.calls) == _AI_CACHE_DELETE_MAX_BATCHES * 2


def test_redis_purge_covers_legacy_and_current_transcript_namespaces(monkeypatch) -> None:
    client = MagicMock()
    client.scan_iter.side_effect = [
        iter([b"clip_analysis:old"]),
        iter([b"media_analysis:new"]),
    ]
    client.delete.side_effect = lambda *keys: len(keys)
    monkeypatch.setattr("app.pipeline.clip_cache._get_redis", lambda: client)

    assert maintenance._purge_media_analysis_redis() == 2
    assert [call.kwargs["match"] for call in client.scan_iter.call_args_list] == [
        "clip_analysis:*",
        "media_analysis:*",
    ]
    client.delete.assert_called_once_with(b"clip_analysis:old", b"media_analysis:new")
