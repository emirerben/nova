"""Unit tests for the offline stale-lyrics backfill sweep."""

from __future__ import annotations

from app.tasks import lyrics_backfill


def test_backfill_refreshes_only_stale_tracks(monkeypatch) -> None:
    monkeypatch.setattr(lyrics_backfill, "_stale_track_ids", lambda: ["a", "b", "c"])
    monkeypatch.setattr(lyrics_backfill, "_sleep", lambda *_a, **_k: None)

    refreshed: list[str] = []

    def fake_refresh(*, track_id, reason):
        assert reason == "backfill"
        if track_id == "b":
            raise lyrics_backfill.TransientLyricsCacheRefreshError("lrclib down")
        refreshed.append(track_id)
        return {"prompt_version": "live"}

    monkeypatch.setattr(lyrics_backfill, "refresh_track_lyrics_cache", fake_refresh)

    summary = lyrics_backfill.backfill_stale_lyrics.apply(kwargs={"dry_run": False}).get()

    assert summary["candidates"] == 3
    assert summary["refreshed"] == 2
    assert summary["transient"] == 1
    assert summary["failed"] == 0
    assert refreshed == ["a", "c"]


def test_backfill_terminal_failure_is_counted_not_fatal(monkeypatch) -> None:
    monkeypatch.setattr(lyrics_backfill, "_stale_track_ids", lambda: ["a", "b"])
    monkeypatch.setattr(lyrics_backfill, "_sleep", lambda *_a, **_k: None)

    def fake_refresh(*, track_id, reason):  # noqa: ARG001
        if track_id == "a":
            raise lyrics_backfill.LyricsCacheRefreshError("no synced lyrics")
        return {"prompt_version": "live"}

    monkeypatch.setattr(lyrics_backfill, "refresh_track_lyrics_cache", fake_refresh)

    summary = lyrics_backfill.backfill_stale_lyrics.apply(kwargs={"dry_run": False}).get()

    assert summary["candidates"] == 2
    assert summary["refreshed"] == 1
    assert summary["failed"] == 1


def test_backfill_dry_run_does_not_refresh(monkeypatch) -> None:
    monkeypatch.setattr(lyrics_backfill, "_stale_track_ids", lambda: ["a", "b"])

    def fail(*_a, **_k):
        raise AssertionError("dry run must not refresh")

    monkeypatch.setattr(lyrics_backfill, "refresh_track_lyrics_cache", fail)

    summary = lyrics_backfill.backfill_stale_lyrics.apply(kwargs={"dry_run": True}).get()

    assert summary == {
        "candidates": 2,
        "refreshed": 0,
        "transient": 0,
        "failed": 0,
        "dry_run": True,
    }
