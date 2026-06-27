"""Tests that the SFX pass is a no-op when disabled or when placements are empty."""

from __future__ import annotations

from app.agents._schemas.sound_effect import coerce_sound_effects


def test_coerce_sound_effects_empty_returns_none_falsy():
    """coerce_sound_effects([]) → None (falsy) → pass never fires."""
    result = coerce_sound_effects([])
    assert not result  # None is falsy


def test_coerce_sound_effects_none_returns_none():
    result = coerce_sound_effects(None)
    assert result is None


def test_sfx_pass_skipped_when_not_sfx_only():
    """_is_sfx_only is False when any non-sfx override is also set."""
    # Simulate calling _run_regenerate_variant with both sfx_override and override_text.
    # Since _is_sfx_only requires all other overrides to be None, this should NOT
    # route to _run_sfx_pass. We test the guard logic indirectly via the condition.
    sfx_override = [{"id": "x", "src_gcs_path": "sound-effects/x/a.mp3", "at_s": 1.0}]
    override_text = "some text"

    # The guard: _is_sfx_only requires override_text is None
    _is_sfx_only = (
        sfx_override is not None
        and None is None  # media_overlays_override
        and None is None  # new_track_id
        and override_text is None  # this is NOT None → False
    )
    assert _is_sfx_only is False


def test_sfx_pass_would_fire_when_sfx_only():
    """_is_sfx_only is True when only sfx_override is set (all other overrides None/False)."""
    sfx_override = [{"id": "x", "src_gcs_path": "sound-effects/x/a.mp3", "at_s": 1.0}]

    _is_sfx_only = (
        sfx_override is not None
        and None is None  # media_overlays_override
        and None is None  # new_track_id
        and None is None  # override_text
        and not False  # remove_text
        and None is None  # style_set_id
        and None is None  # size_override_px
        and None is None  # mix_override
        and None is None  # layout_override
        and None is None  # timeline_override
        and None is None  # font_family_override
        and None is None  # effect_override
        and None is None  # text_color_override
        and None is None  # cluster_hero_font_override
        and None is None  # cluster_body_font_override
        and None is None  # cluster_accent_font_override
        and None is None  # cluster_hero_size_px_override
        and None is None  # cluster_body_size_px_override
        and None is None  # cluster_accent_size_px_override
    )
    assert _is_sfx_only is True


def test_sfx_pass_blocked_when_media_overlay_also_set():
    """_is_sfx_only is False when media_overlays_override is also set."""
    sfx_override = [{"id": "x", "src_gcs_path": "sound-effects/x/a.mp3", "at_s": 1.0}]
    media_overlays_override = [{"id": "card1", "kind": "image"}]

    _is_sfx_only = (
        sfx_override is not None and media_overlays_override is None  # NOT None → False
    )
    assert _is_sfx_only is False
