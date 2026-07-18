from __future__ import annotations

import types
import uuid

import pytest

import app.routes.generative_jobs as gj
from app.config import settings


def _job(variants: list[dict] | None = None):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={"variants": variants or []},
        all_candidates={"clip_paths": ["slot-uploads/u/clip.mp4"]},
    )


def _variant(**extra) -> dict:
    return {
        "variant_id": "song_lyrics",
        "text_mode": "lyrics",
        "render_status": "ready",
        "music_track_id": "track-1",
        "lyrics_available": True,
        "video_path": "generative-jobs/j/v.mp4",
        **extra,
    }


def test_lyrics_capabilities_flag_off_preserves_existing_locks(monkeypatch) -> None:
    monkeypatch.setattr(gj, "_LYRICS_EDITOR_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "GENERATIVE_TIMELINE_EDITOR_ENABLED", True, raising=False)

    caps = gj._editor_capabilities(_job(), _variant())

    assert caps["text_elements"] is False
    assert caps["timeline"] is False
    assert caps["split_clips"] is False
    assert caps["reason"] == "lyrics_sync"
    assert caps["lyrics"]["editable"] is False
    assert caps["lyrics"]["reason"] == "disabled"


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        (
            _variant(),
            {"editable": True, "enabled": True, "can_toggle_on": True, "reason": None},
        ),
        (
            _variant(variant_id="song_text", text_mode="agent_text", lyrics_enabled=False),
            {"editable": False, "enabled": False, "can_toggle_on": True, "reason": None},
        ),
        (
            _variant(variant_id="original_text", text_mode="agent_text", music_track_id=None),
            {
                "editable": False,
                "enabled": False,
                "can_toggle_on": False,
                "reason": "no_track",
            },
        ),
        (
            _variant(lyrics_available=None),
            {
                "editable": False,
                "enabled": True,
                "can_toggle_on": False,
                "reason": "no_renderable_lyrics",
            },
        ),
    ],
)
def test_lyrics_capabilities_flag_on(monkeypatch, variant, expected) -> None:
    monkeypatch.setattr(gj, "_LYRICS_EDITOR_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "GENERATIVE_TIMELINE_EDITOR_ENABLED", True, raising=False)

    caps = gj._editor_capabilities(_job(), variant)

    if variant["text_mode"] == "lyrics":
        assert caps["text_elements"] is True
        assert caps["timeline"] is False
        assert caps["split_clips"] is False
        assert caps["reason"] == "lyrics_sync"
    assert caps["lyrics"] == expected
