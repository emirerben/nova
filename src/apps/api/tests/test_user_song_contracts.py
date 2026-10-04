"""KRI-374 shared contract invariants every lane relies on."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import settings
from app.schemas.user_song import (
    SongAlignment,
    TakeAlignment,
    UserSongPlan,
)
from app.services.phone_rollout import phone_user_song_supported


def test_confident_take_requires_delta():
    with pytest.raises(ValidationError):
        TakeAlignment(media_id="m1", status="confident")
    ok = TakeAlignment(media_id="m1", status="confident", delta_s=12.5, confidence=0.9)
    assert ok.delta_s == 12.5
    # Ambiguous / unmatched takes may be positionless.
    assert TakeAlignment(media_id="m2", status="unmatched").delta_s is None


def test_song_alignment_round_trips():
    a = SongAlignment(
        song_generation=3,
        takes={"m1": TakeAlignment(media_id="m1", status="confident", delta_s=4.0)},
    )
    assert SongAlignment.model_validate(a.model_dump()) == a


def test_user_song_plan_window_must_fit_song():
    base = dict(mode="lipsync", plan_item_id="p", generation=1, duration_s=60.0)
    UserSongPlan(window_start_s=10.0, window_end_s=40.0, **base)
    with pytest.raises(ValidationError):
        UserSongPlan(window_start_s=40.0, window_end_s=40.0, **base)
    with pytest.raises(ValidationError):
        UserSongPlan(window_start_s=10.0, window_end_s=61.0, **base)


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        TakeAlignment(media_id="m1", status="unmatched", surprise=1)


def test_phone_user_song_supported_needs_flag_and_capabilities(monkeypatch):
    monkeypatch.setattr(settings, "user_song_montage_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", ["musicBed", "audioMix"])
    assert phone_user_song_supported() is True

    monkeypatch.setattr(settings, "phone_render_verified_features", ["musicBed"])
    assert phone_user_song_supported() is False

    monkeypatch.setattr(settings, "phone_render_verified_features", ["musicBed", "audioMix"])
    monkeypatch.setattr(settings, "user_song_montage_enabled", False)
    assert phone_user_song_supported() is False
