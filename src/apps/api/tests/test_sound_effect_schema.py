"""Tests for the SoundEffectPlacement schema."""

import pytest

from app.agents._schemas.sound_effect import (
    SoundEffectPlacement,
    coerce_sound_effects,
    validate_sfx_gcs_path,
)


def test_gain_clamped_above_max():
    p = SoundEffectPlacement(id="abc", src_gcs_path="users/u1/sfx/x.mp3", gain=5.0)
    assert p.gain == 2.0


def test_gain_clamped_below_zero():
    p = SoundEffectPlacement(id="abc", src_gcs_path="users/u1/sfx/x.mp3", gain=-1.0)
    assert p.gain == 0.0


def test_gain_default():
    p = SoundEffectPlacement(id="abc", src_gcs_path="users/u1/sfx/x.mp3")
    assert p.gain == 1.0


def test_at_s_clamped_below_zero():
    p = SoundEffectPlacement(id="abc", src_gcs_path="users/u1/sfx/x.mp3", at_s=-5.0)
    assert p.at_s == 0.0


def test_at_s_default():
    p = SoundEffectPlacement(id="abc", src_gcs_path="users/u1/sfx/x.mp3")
    assert p.at_s == 0.0


def test_validate_sfx_gcs_path_accepts_sound_effects_prefix():
    # Should not raise
    validate_sfx_gcs_path("sound-effects/abc123/audio.mp3")


def test_validate_sfx_gcs_path_accepts_users_prefix():
    # Should not raise
    validate_sfx_gcs_path("users/uid/plan/item/sfx/file.mp3")


def test_validate_sfx_gcs_path_rejects_other_prefix():
    with pytest.raises(ValueError, match="SFX asset must be under"):
        validate_sfx_gcs_path("music/track123/audio.m4a")


def test_validate_sfx_gcs_path_rejects_dev_user():
    with pytest.raises(ValueError):
        validate_sfx_gcs_path("dev-user/job123/raw.mp4")


def test_coerce_sound_effects_returns_none_for_empty():
    assert coerce_sound_effects([]) is None
    assert coerce_sound_effects(None) is None


def test_coerce_sound_effects_drops_bad_entries():
    raw = [
        {"id": "a", "src_gcs_path": "users/u/sfx/ok.mp3"},
        "this is not a dict",
        {"id": "b", "src_gcs_path": "users/u/sfx/ok2.mp3"},
    ]
    result = coerce_sound_effects(raw)
    assert result is not None
    assert len(result) == 2


def test_coerce_sound_effects_returns_none_when_all_bad():
    result = coerce_sound_effects(["not", "a", "dict"])
    assert result is None


def test_coerce_sound_effects_valid_entry():
    raw = [{"id": "abc", "src_gcs_path": "sound-effects/x/audio.mp3", "at_s": 3.5, "gain": 0.8}]
    result = coerce_sound_effects(raw)
    assert result is not None
    assert len(result) == 1
    assert result[0].at_s == 3.5
    assert result[0].gain == 0.8
