from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import app.routes.generative_jobs as gj


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
        "lyrics_available": True,
        "render_generation_id": "old-gen",
        **extra,
    }


_DEFAULT_LYRICS = object()


def _track(lyrics_cached: object = _DEFAULT_LYRICS):
    if lyrics_cached is _DEFAULT_LYRICS:
        lyrics_cached = {"language": "en"}
    return types.SimpleNamespace(id="track-1", lyrics_cached=lyrics_cached)


def _result(value):
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=value)
    return res


def _db(job, track=None):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(job))
    db.get = AsyncMock(return_value=track)
    db.commit = AsyncMock()
    return db


def _valid_override(text: str = "New line") -> dict:
    return {
        "L1": {
            "text": text,
            "orig_text": "Old line",
            "orig_start_s": 1.25,
            "style": {
                "color": "#FFFFFF",
                "highlight_color": "#FFD24A",
                "font_family": "Inter-Regular",
                "size_px": 72,
            },
        }
    }


@pytest.mark.asyncio
async def test_dispatch_set_lyrics_404_when_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(gj, "_LYRICS_EDITOR_ENABLED", False, raising=False)
    job = _job(_variant())

    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_set_lyrics(_db(job, _track()), job, "song_lyrics", enabled=False)

    assert exc.value.status_code == 404


@pytest.mark.parametrize(
    ("variant", "track", "code"),
    [
        (_variant(music_track_id=None), None, "no_track"),
        (_variant(), _track(lyrics_cached=None), "no_renderable_lyrics"),
    ],
)
def test_validate_enable_on_422_codes(monkeypatch, variant, track, code) -> None:
    monkeypatch.setattr(gj, "_LYRICS_EDITOR_ENABLED", True, raising=False)

    with pytest.raises(HTTPException) as exc:
        gj.validate_lyrics_section(variant, {"enabled": True}, music_track=track)

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == code


@pytest.mark.parametrize(
    "line_overrides",
    [
        {"bad": {"orig_text": "x", "orig_start_s": 0.0}},
        {f"L{i}": {"orig_text": "x", "orig_start_s": 0.0} for i in range(101)},
        {"L1": {"orig_text": "x", "orig_start_s": 0.0, "start_s": 1.0}},
        {"L1": {"orig_text": "x", "orig_start_s": 0.0, "style": {"color": "white"}}},
        {"L1": {"orig_text": "x", "orig_start_s": 0.0, "style": {"font_family": "Bad"}}},
        {"L1": {"orig_text": "x", "orig_start_s": 0.0, "text": ""}},
        {"L1": {"orig_text": "x", "orig_start_s": 0.0, "text": "x" * 201}},
        {"L1": {"text": "x", "orig_start_s": 0.0}},
        {"L1": {"text": "x", "orig_text": "x"}},
    ],
)
def test_line_override_validation(monkeypatch, line_overrides) -> None:
    monkeypatch.setattr(gj, "_LYRICS_EDITOR_ENABLED", True, raising=False)

    with pytest.raises(HTTPException) as exc:
        gj.validate_lyrics_section(
            _variant(), {"line_overrides": line_overrides}, music_track=_track()
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "invalid_line_override"


@pytest.mark.asyncio
async def test_dispatch_set_lyrics_happy_path_persists_and_enqueues(monkeypatch) -> None:
    monkeypatch.setattr(gj, "_LYRICS_EDITOR_ENABLED", True, raising=False)
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **kwargs: calls.append(kwargs)),
        raising=False,
    )
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args: None)
    job = _job(_variant())
    db = _db(job, _track())

    await gj.dispatch_set_lyrics(
        db,
        job,
        "song_lyrics",
        enabled=True,
        line_overrides=_valid_override(),
    )

    variant = job.assembly_plan["variants"][0]
    assert variant["lyrics_enabled"] is True
    assert variant["lyric_line_overrides"]["L1"]["text"] == "New line"
    assert variant["render_generation_id"] != "old-gen"
    assert variant["render_status"] == "rendering"
    db.commit.assert_awaited_once()
    assert len(calls) == 1
    assert calls[0]["args"] == [str(job.id), "song_lyrics"]
    assert calls[0]["kwargs"]["render_gen_id"] == variant["render_generation_id"]
    # REGRESSION (2026-07-18 E2E): without force_full_render the regen picks the
    # fast-reburn path (no override kwargs present) and skips lyric re-injection.
    assert calls[0]["kwargs"]["force_full_render"] is True
    assert "queue" not in calls[0]


@pytest.mark.asyncio
async def test_dispatch_set_lyrics_line_overrides_null_wipes(monkeypatch) -> None:
    monkeypatch.setattr(gj, "_LYRICS_EDITOR_ENABLED", True, raising=False)
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **_kwargs: None),
        raising=False,
    )
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args: None)
    job = _job(_variant(lyric_line_overrides=_valid_override()))

    await gj.dispatch_set_lyrics(_db(job, _track()), job, "song_lyrics", line_overrides=None)

    assert job.assembly_plan["variants"][0]["lyric_line_overrides"] is None
