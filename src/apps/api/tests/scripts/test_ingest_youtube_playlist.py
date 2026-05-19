"""Unit tests for scripts.ingest_youtube_playlist.

Focuses on the parts that don't touch the network or the DB: canonical
URL normalization and the dry-run / idempotency planning.
"""

from __future__ import annotations

from unittest.mock import patch

from scripts.ingest_youtube_playlist import (
    PlaylistEntry,
    _existing_source_urls,
    _list_playlist,
)


def test_canonical_url_strips_playlist_params() -> None:
    e = PlaylistEntry("abc123", "Track Title", "Some Artist", 180.0)
    assert e.canonical_url == "https://www.youtube.com/watch?v=abc123"


def test_canonical_url_for_youtu_be_style() -> None:
    # PlaylistEntry only stores the id; whatever url style yt-dlp came
    # from is normalized away — the canonical form is identical.
    e1 = PlaylistEntry("xyz789", "A", None, None)
    e2 = PlaylistEntry("xyz789", "Different title still same video", "X", 100.0)
    assert e1.canonical_url == e2.canonical_url


def test_list_playlist_skips_entries_without_id() -> None:
    fake_info = {
        "entries": [
            {"id": "a1", "title": "track 1", "uploader": "X", "duration": 100},
            {"id": "", "title": "track no id"},  # skipped
            {"title": "track no id field"},  # skipped
            {"id": "a2", "title": "track 2"},
        ]
    }

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download):  # noqa: ARG002
            return fake_info

    with patch("scripts.ingest_youtube_playlist.yt_dlp.YoutubeDL", FakeYDL):
        entries = _list_playlist("https://www.youtube.com/playlist?list=PLfake")

    assert len(entries) == 2
    assert entries[0].video_id == "a1"
    assert entries[1].video_id == "a2"
    assert entries[0].duration_s == 100.0


def test_list_playlist_unwraps_unicode_title() -> None:
    fake_info = {
        "entries": [
            {"id": "u1", "title": "Tëst Tïtle — with 🎵 emoji", "duration": 60},
        ]
    }

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download):  # noqa: ARG002
            return fake_info

    with patch("scripts.ingest_youtube_playlist.yt_dlp.YoutubeDL", FakeYDL):
        entries = _list_playlist("https://www.youtube.com/playlist?list=PLfake")

    assert entries[0].title == "Tëst Tïtle — with 🎵 emoji"


def test_existing_source_urls_empty_input_short_circuits() -> None:
    # Empty list should never hit the DB.
    assert _existing_source_urls([]) == set()
