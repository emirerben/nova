"""Tests for render-time lyric cache freshness guard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import lyrics_cache_refresh
from app.services.lyrics_cache_refresh import (
    LyricsCacheRefreshError,
    TransientLyricsCacheRefreshError,
    ensure_fresh_lyrics_cached_for_render,
    lyrics_cache_is_stale,
)


def test_lyrics_cache_is_stale_compares_live_prompt_version(monkeypatch) -> None:
    monkeypatch.setattr(
        lyrics_cache_refresh,
        "LyricsExtractionAgent",
        SimpleNamespace(spec=SimpleNamespace(prompt_version="live")),
    )

    assert not lyrics_cache_is_stale(None)
    assert not lyrics_cache_is_stale({"prompt_version": "live"})
    assert lyrics_cache_is_stale({"prompt_version": "old"})
    assert lyrics_cache_is_stale({"source": "lrclib_synced+whisper", "lines": []})


def test_ensure_fresh_noops_when_lyrics_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        lyrics_cache_refresh,
        "LyricsExtractionAgent",
        SimpleNamespace(spec=SimpleNamespace(prompt_version="live")),
    )

    def fail_download(*_args, **_kwargs):
        raise AssertionError("download should not run when lyrics are disabled")

    monkeypatch.setattr(lyrics_cache_refresh, "download_to_file", fail_download)

    stale = {"prompt_version": "old", "source": "lrclib_synced+whisper", "lines": []}
    assert (
        ensure_fresh_lyrics_cached_for_render(
            track_id="t1",
            lyrics_cached=stale,
            lyrics_config={"enabled": False},
            reason="test",
        )
        is stale
    )


def test_ensure_fresh_refreshes_and_persists_stale_enabled_cache(monkeypatch) -> None:
    monkeypatch.setattr(lyrics_cache_refresh.settings, "openai_api_key", "key")

    track = SimpleNamespace(
        id="t1",
        audio_gcs_path="music/t1.m4a",
        track_config={"best_start_s": 223.0, "best_end_s": 237.0},
        title="Again",
        artist="Roger Sanchez",
        duration_s=240.0,
        lyrics_cached={"prompt_version": "old"},
        lyrics_status="ready",
        lyrics_whisper_draft={"old": "draft"},
        lyrics_source="lrclib_synced+whisper",
        lyrics_error_detail=None,
        lyrics_diagnostic=None,
        lyrics_extracted_at=None,
    )
    commits = 0
    seen: dict[str, object] = {}

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def get(self, _model, track_id):
            assert track_id == "t1"
            return track

        def commit(self):
            nonlocal commits
            commits += 1

    fresh = {
        "prompt_version": "live",
        "source": "lrclib_synced+whisper",
        "lines": [{"text": "I swear to God", "start_s": 231.28, "end_s": 232.32}],
    }

    class FakeOutput:
        is_empty = False
        source = "lrclib_synced+whisper"
        lines = [object()]
        lyrics_diagnostic = {"fallback_path": "ready_synced"}

        def model_dump(self):
            return fresh

    class FakeAgent:
        spec = SimpleNamespace(prompt_version="live")

        def __init__(self, model_client):
            assert model_client is None

        def run(self, input, ctx):
            seen["input"] = input
            seen["ctx"] = ctx
            return FakeOutput()

    monkeypatch.setattr(lyrics_cache_refresh, "_sync_session", lambda: FakeSession())
    monkeypatch.setattr(lyrics_cache_refresh, "LyricsExtractionAgent", FakeAgent)
    monkeypatch.setattr(
        lyrics_cache_refresh,
        "download_to_file",
        lambda gcs, local: seen.update({"download": (gcs, local)}),
    )

    out = ensure_fresh_lyrics_cached_for_render(
        track_id="t1",
        lyrics_cached={"prompt_version": "old", "source": "lrclib_synced+whisper", "lines": []},
        lyrics_config={"enabled": True},
        reason="music_job",
    )

    assert out == fresh
    assert track.lyrics_cached == fresh
    assert track.lyrics_whisper_draft is None
    assert track.lyrics_status == "ready"
    assert commits == 1
    assert seen["download"][0] == "music/t1.m4a"
    assert seen["input"].track_title == "Again"
    assert seen["input"].best_start_s == 223.0


def test_ensure_fresh_preserves_cache_when_refresh_hits_lrclib_error(
    monkeypatch,
) -> None:
    monkeypatch.setattr(lyrics_cache_refresh.settings, "openai_api_key", "key")

    stale = {
        "prompt_version": "old",
        "source": "lrclib_synced+whisper",
        "lines": [{"text": "Well, you can tell", "start_s": 14.08, "end_s": 17.0}],
    }
    track = SimpleNamespace(
        id="t1",
        audio_gcs_path="music/t1.m4a",
        track_config={},
        title="Bee Gees - Stayin' Alive",
        artist="beegees",
        duration_s=249.0,
        lyrics_cached=stale,
        lyrics_status="ready",
        lyrics_whisper_draft=None,
        lyrics_source="lrclib_synced+whisper",
        lyrics_error_detail=None,
        lyrics_diagnostic={"fallback_path": "ready_synced"},
        lyrics_extracted_at=None,
    )
    commits = 0

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def get(self, _model, _track_id):
            return track

        def commit(self):
            nonlocal commits
            commits += 1

    class FakeOutput:
        is_empty = False
        source = "whisper_only"
        lines = [object()]
        lyrics_diagnostic = {
            "get_status": "error",
            "search_status": "error",
            "lrclib_error": "lrclib network error: The read operation timed out",
            "fallback_path": "needs_manual_lyrics",
        }

        def model_dump(self):
            return {"prompt_version": "live", "source": "whisper_only", "lines": []}

    class FakeAgent:
        spec = SimpleNamespace(prompt_version="live")

        def __init__(self, model_client):
            pass

        def run(self, input, ctx):  # noqa: ARG002
            return FakeOutput()

    monkeypatch.setattr(lyrics_cache_refresh, "_sync_session", lambda: FakeSession())
    monkeypatch.setattr(lyrics_cache_refresh, "LyricsExtractionAgent", FakeAgent)
    monkeypatch.setattr(lyrics_cache_refresh, "download_to_file", lambda _gcs, _local: None)

    with pytest.raises(TransientLyricsCacheRefreshError, match="LRCLIB lookup failed"):
        ensure_fresh_lyrics_cached_for_render(
            track_id="t1",
            lyrics_cached=stale,
            lyrics_config={"enabled": True},
            reason="lyrics_preview",
        )

    assert commits == 0
    assert track.lyrics_status == "ready"
    assert track.lyrics_cached is stale
    assert track.lyrics_whisper_draft is None
    assert track.lyrics_source == "lrclib_synced+whisper"
    assert track.lyrics_error_detail is None
    assert track.lyrics_diagnostic == {"fallback_path": "ready_synced"}


def test_ensure_fresh_refuses_to_render_stale_cache_when_refresh_is_not_publishable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(lyrics_cache_refresh.settings, "openai_api_key", "key")

    track = SimpleNamespace(
        id="t1",
        audio_gcs_path="music/t1.m4a",
        track_config={},
        title="Again",
        artist="Roger Sanchez",
        duration_s=240.0,
        lyrics_cached={"prompt_version": "old"},
        lyrics_status="ready",
        lyrics_whisper_draft=None,
        lyrics_source="lrclib_synced+whisper",
        lyrics_error_detail=None,
        lyrics_diagnostic=None,
        lyrics_extracted_at=None,
    )
    commits = 0

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def get(self, _model, _track_id):
            return track

        def commit(self):
            nonlocal commits
            commits += 1

    class FakeOutput:
        is_empty = False
        source = "whisper_only"
        lines = [object()]
        lyrics_diagnostic = {}

        def model_dump(self):
            return {"prompt_version": "live", "source": "whisper_only", "lines": []}

    class FakeAgent:
        spec = SimpleNamespace(prompt_version="live")

        def __init__(self, model_client):
            pass

        def run(self, input, ctx):  # noqa: ARG002
            return FakeOutput()

    monkeypatch.setattr(lyrics_cache_refresh, "_sync_session", lambda: FakeSession())
    monkeypatch.setattr(lyrics_cache_refresh, "LyricsExtractionAgent", FakeAgent)
    monkeypatch.setattr(lyrics_cache_refresh, "download_to_file", lambda _gcs, _local: None)

    with pytest.raises(LyricsCacheRefreshError):
        ensure_fresh_lyrics_cached_for_render(
            track_id="t1",
            lyrics_cached={"prompt_version": "old"},
            lyrics_config={"enabled": True},
            reason="music_job",
        )

    assert commits == 1
    assert track.lyrics_status == "needs_manual_lyrics"
    assert track.lyrics_cached is None
    assert track.lyrics_whisper_draft == {
        "prompt_version": "live",
        "source": "whisper_only",
        "lines": [],
    }
    assert track.lyrics_source == "whisper_only"
    assert track.lyrics_error_detail == "LRCLIB lookup failed; paste a row ID to recover"
