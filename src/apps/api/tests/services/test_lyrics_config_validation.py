"""Tests for the shared lyrics_config validator + resolution helper.

The resolution matrix lives here (not in test_template_orchestrate) because
the helper is the unit under test — spinning up the full orchestrator just
to exercise four lines of resolution logic is overkill.
"""

from __future__ import annotations

import pytest

from app.services.lyrics_config_validation import (
    is_valid_hex_color,
    resolve_effective_lyrics_config,
    validate_lyrics_config_dict,
)


class TestResolveEffectiveLyricsConfig:
    """The truthiness pitfall regression set.

    Resolution rule: template's override wins when explicitly set
    (``is not None``), including the empty dict. ``None`` on the template
    falls back to the track. The empty dict ``{}`` is a legit "lyrics
    explicitly off" state — it must NOT fall back to the track.
    """

    def test_template_none_falls_back_to_track(self):
        track = {"enabled": True, "style": "karaoke"}
        assert resolve_effective_lyrics_config(None, track) == track

    def test_template_override_wins_over_track(self):
        tmpl = {"enabled": True, "style": "per-word-pop"}
        track = {"enabled": True, "style": "karaoke"}
        assert resolve_effective_lyrics_config(tmpl, track) == tmpl

    def test_empty_dict_template_overrides_enabled_track(self):
        """The bug this guards against: `or` would treat {} as falsy and
        pick the track. We must use `is not None` instead, so {} stays.
        """
        track = {"enabled": True, "style": "karaoke"}
        assert resolve_effective_lyrics_config({}, track) == {}

    def test_template_disabled_wins_over_enabled_track(self):
        tmpl = {"enabled": False}
        track = {"enabled": True, "style": "karaoke"}
        assert resolve_effective_lyrics_config(tmpl, track) == tmpl

    def test_both_none_returns_none(self):
        assert resolve_effective_lyrics_config(None, None) is None


class TestValidateLyricsConfigDict:
    def test_accepts_minimal_enabled(self):
        validate_lyrics_config_dict({"enabled": True})

    def test_accepts_full_config(self):
        validate_lyrics_config_dict(
            {
                "enabled": True,
                "style": "karaoke",
                "position": "bottom",
                "text_color": "#FFFFFF",
                "highlight_color": "#FFFF00",
            }
        )

    def test_accepts_empty_dict(self):
        """{} is a legal "explicit off" sentinel; the resolver picks it
        because of `is not None`, and the validator must let it through.
        """
        validate_lyrics_config_dict({})

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError, match="object"):
            validate_lyrics_config_dict("not a dict")

    def test_rejects_bad_style(self):
        with pytest.raises(ValueError, match="style"):
            validate_lyrics_config_dict({"style": "bouncing-pickles"})

    def test_rejects_bad_position(self):
        with pytest.raises(ValueError, match="position"):
            validate_lyrics_config_dict({"position": "elsewhere"})

    def test_rejects_bad_hex(self):
        with pytest.raises(ValueError, match="text_color"):
            validate_lyrics_config_dict({"text_color": "not-hex"})

    def test_accepts_per_word_pop_style(self):
        validate_lyrics_config_dict({"style": "per-word-pop"})

    def test_accepts_line_style(self):
        validate_lyrics_config_dict({"style": "line"})

    def test_accepts_all_known_positions(self):
        for pos in ("top", "bottom", "center", "center-above", "center-below", "center-label"):
            validate_lyrics_config_dict({"position": pos})


class TestIsValidHexColor:
    # The validator strips a leading '#' before checking, so both '#FFFFFF'
    # and bare 'FFFFFF' are accepted. The lyrics config UI always sends the
    # '#'-prefixed form, but inputs that drift through other paths still
    # validate correctly.
    @pytest.mark.parametrize(
        "s",
        ["#FFFFFF", "#000000", "#abcdef", "#ABCDEF", "#FF00FF", "FFFFFF"],
    )
    def test_valid(self, s):
        assert is_valid_hex_color(s) is True

    @pytest.mark.parametrize("s", ["#FFF", "#GGGGGG", "", "not-hex", None, 123, "#FFFFFFFF"])
    def test_invalid(self, s):
        assert is_valid_hex_color(s) is False
