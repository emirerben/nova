"""GET .../lyric-seeds dispatch (lyrics-as-optional-elements instant materialize).

House style of test_lyrics_dispatch.py: fake SimpleNamespace job + AsyncMock db,
no real DB/TestClient.
"""

from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app.routes.generative_jobs as gj
from app.config import settings


def _job(variant: dict):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={"variants": [variant]},
        status="variants_ready",
    )


def _variant(**extra) -> dict:
    return {
        "variant_id": "song_lyrics",
        "text_mode": "lyrics",
        "render_status": "ready",
        "music_track_id": "track-1",
        "style_set_id": None,
        **extra,
    }


def _lyrics_cache() -> dict:
    return {
        "source": "lrclib_synced+whisper",
        "language": "en",
        "lines": [
            {
                "text": "Hello bright world",
                "start_s": 0.0,
                "end_s": 2.0,
                "words": [
                    {"text": "Hello", "start_s": 0.0, "end_s": 0.6},
                    {"text": "bright", "start_s": 0.6, "end_s": 1.2},
                    {"text": "world", "start_s": 1.2, "end_s": 2.0},
                ],
            }
        ],
    }


_DEFAULT_LYRICS = object()


def _track(*, lyrics_cached: object = _DEFAULT_LYRICS) -> types.SimpleNamespace:
    if lyrics_cached is _DEFAULT_LYRICS:
        lyrics_cached = _lyrics_cache()
    return types.SimpleNamespace(
        id="track-1",
        track_config={"best_start_s": 0.0, "best_end_s": 6.0},
        best_sections=None,
        section_version=None,
        lyrics_cached=lyrics_cached,
    )


def _db(track=None):
    db = AsyncMock()
    db.get = AsyncMock(return_value=track)
    return db


@pytest.mark.asyncio
async def test_lyric_seeds_404_when_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "lyrics_optional_enabled", False, raising=False)
    job = _job(_variant())

    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_get_lyric_seeds(job, "song_lyrics", _db(_track()))

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_lyric_seeds_404_when_variant_missing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "lyrics_optional_enabled", True, raising=False)
    job = _job(_variant())

    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_get_lyric_seeds(job, "no_such_variant", _db(_track()))

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_lyric_seeds_422_no_matched_track_when_variant_has_none(monkeypatch) -> None:
    monkeypatch.setattr(settings, "lyrics_optional_enabled", True, raising=False)
    job = _job(_variant(music_track_id=None))

    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_get_lyric_seeds(job, "song_lyrics", _db(None))

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "no_matched_track"


@pytest.mark.asyncio
async def test_lyric_seeds_422_no_matched_track_when_db_lookup_misses(monkeypatch) -> None:
    monkeypatch.setattr(settings, "lyrics_optional_enabled", True, raising=False)
    job = _job(_variant())

    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_get_lyric_seeds(job, "song_lyrics", _db(None))

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "no_matched_track"


@pytest.mark.asyncio
async def test_lyric_seeds_422_no_renderable_lyrics(monkeypatch) -> None:
    monkeypatch.setattr(settings, "lyrics_optional_enabled", True, raising=False)
    job = _job(_variant())
    track = _track(lyrics_cached=None)

    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_get_lyric_seeds(job, "song_lyrics", _db(track))

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "no_renderable_lyrics"


@pytest.mark.asyncio
async def test_lyric_seeds_derives_from_cache_when_no_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(settings, "lyrics_optional_enabled", True, raising=False)
    job = _job(_variant())
    track = _track()

    result = await gj.dispatch_get_lyric_seeds(job, "song_lyrics", _db(track))

    assert len(result["elements"]) == 1
    elem = result["elements"][0]
    assert elem["id"] == "lyr-L0"
    assert elem["role"] == "lyric_line"
    assert elem["text"] == "Hello bright world"
    assert elem["effect"] == "karaoke-line"
    assert elem["word_timings"]


@pytest.mark.asyncio
async def test_lyric_seeds_prefers_snapshot_when_present(monkeypatch) -> None:
    """An anchored `lyric_overlay_snapshot` is used as-is (no re-derivation
    from the lyrics cache, no track_config lookup needed for timing)."""
    monkeypatch.setattr(settings, "lyrics_optional_enabled", True, raising=False)
    snapshot = [
        {
            "line_key": "L3",
            "text": "Anchored line",
            "start_s": 4.5,
            "end_s": 6.0,
            "font_family": None,
            "size_px": None,
            "color": "#FFFFFF",
            "highlight_color": None,
            "y_frac": 0.8,
            "effect": "static",
        }
    ]
    job = _job(_variant(lyric_overlay_snapshot=snapshot))
    track = _track()

    result = await gj.dispatch_get_lyric_seeds(job, "song_lyrics", _db(track))

    assert len(result["elements"]) == 1
    elem = result["elements"][0]
    assert elem["id"] == "lyr-L3"
    assert elem["text"] == "Anchored line"
    assert elem["start_s"] == 4.5
    assert elem["end_s"] == 6.0
