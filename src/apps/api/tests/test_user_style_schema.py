"""Guards for the UserStyle / StyleKnobs schema (Creator Agent M1).

Validates two invariants:
  1. StyleKnobs only contains fields that are parity-safe (confirmed to work
     in BOTH the Pillow and Skia renderers). This prevents a future PR from
     adding an 'effect' knob without acknowledging the #296 risk.
  2. Extra fields on StyleKnobs raise (extra="forbid") — the schema is the
     parity-safe allowlist; a field not in StyleKnobs must never reach the DB.

No DB / GPU required (parallel-safe, skia-free).
"""

from __future__ import annotations

import pytest

from app.agents._schemas.user_style import (
    StyleKnobs,
    UserStyle,
    coerce_user_style,
    user_style_knobs_dict,
)

# The parity-safe set: fields confirmed to work in both renderers (#296).
# 'effect' is deliberately EXCLUDED — effect parity across Pillow+Skia is
# unconfirmed and the template DECISIONS.md #296 invariant requires explicit
# sign-off before adding it.
_PARITY_SAFE_KNOB_FIELDS = frozenset(
    {
        "font_family",
        "text_size_px",
        "position",
        "position_x_frac",
        "position_y_frac",
        "text_anchor",
        "text_color",
        "highlight_color",
        "stroke_width",
        "cycle_fonts",
    }
)

# Fields that are NOT parity-safe and must never appear in StyleKnobs.
_PARITY_UNSAFE_FIELDS = frozenset({"effect"})


class TestStyleKnobaParitySafety:
    def test_knobs_fields_subset_of_parity_safe(self) -> None:
        """All fields on StyleKnobs must be in the parity-safe allowlist."""
        knob_fields = set(StyleKnobs.model_fields.keys())
        unsafe = knob_fields - _PARITY_SAFE_KNOB_FIELDS
        assert not unsafe, (
            f"StyleKnobs contains non-parity-safe fields: {unsafe}. "
            "Any field added to StyleKnobs MUST be confirmed to work in BOTH "
            "the Pillow and Skia renderers (CLAUDE.md #296). "
            "Add it to _PARITY_SAFE_KNOB_FIELDS in this test once confirmed."
        )

    def test_parity_safe_set_covers_known_knobs(self) -> None:
        """All parity-safe fields must be present in StyleKnobs (no silent omission)."""
        knob_fields = set(StyleKnobs.model_fields.keys())
        missing = _PARITY_SAFE_KNOB_FIELDS - knob_fields
        assert not missing, (
            f"Parity-safe fields not present in StyleKnobs: {missing}. "
            "Either add them to StyleKnobs or remove from _PARITY_SAFE_KNOB_FIELDS."
        )

    def test_effect_not_in_knobs(self) -> None:
        """'effect' must NEVER appear in StyleKnobs — parity unconfirmed (#296)."""
        assert "effect" not in StyleKnobs.model_fields, (
            "'effect' added to StyleKnobs. Before shipping, confirm that effect "
            "works byte-identically in both Pillow and Skia renderers, then "
            "update this test to allow it and document in agents/DECISIONS.md."
        )

    def test_extra_key_raises(self) -> None:
        """StyleKnobs must have extra='forbid' — unknown keys never reach the DB."""
        with pytest.raises(Exception):  # pydantic ValidationError
            StyleKnobs(effect="glow")  # type: ignore[call-arg]

    def test_extra_key_raises_via_dict(self) -> None:
        with pytest.raises(Exception):
            StyleKnobs.model_validate({"font_family": "Playfair Display", "effect": "glow"})


class TestStyleKnobsValidation:
    def test_text_size_px_clamped_low(self) -> None:
        """Out-of-range value is silently clamped to MIN, not raised."""
        k = StyleKnobs(text_size_px=10)
        from app.pipeline.overlay_sizing import MIN_INTRO_PX

        assert k.text_size_px == MIN_INTRO_PX

    def test_text_size_px_clamped_high(self) -> None:
        """Out-of-range value is silently clamped to MAX, not raised."""
        k = StyleKnobs(text_size_px=200)
        from app.pipeline.overlay_sizing import MAX_INTRO_PX

        assert k.text_size_px == MAX_INTRO_PX

    def test_text_size_px_boundary_ok(self) -> None:
        from app.pipeline.overlay_sizing import MAX_INTRO_PX, MIN_INTRO_PX

        k = StyleKnobs(text_size_px=MIN_INTRO_PX)
        assert k.text_size_px == MIN_INTRO_PX
        k2 = StyleKnobs(text_size_px=MAX_INTRO_PX)
        assert k2.text_size_px == MAX_INTRO_PX

    def test_all_none_ok(self) -> None:
        k = StyleKnobs()
        assert k.font_family is None
        assert k.text_size_px is None


class TestUserStyleCoercion:
    def test_coerce_valid_style(self) -> None:
        raw = {
            "style_set_id": "default",
            "knobs": {"font_family": "Playfair Display", "text_size_px": 56},
            "status": "ready",
            "style_version": "2026-06-07",
        }
        result = coerce_user_style(raw)
        assert result is not None
        assert result.style_set_id == "default"
        assert result.knobs is not None
        assert result.knobs.font_family == "Playfair Display"
        assert result.knobs.text_size_px == 56

    def test_coerce_none_returns_none(self) -> None:
        assert coerce_user_style(None) is None

    def test_coerce_empty_dict_returns_none(self) -> None:
        # Empty dict is falsy → coerce_user_style treats it as "no style" → None
        # (same as None; both mean "style not yet derived")
        result = coerce_user_style({})
        assert result is None

    def test_coerce_invalid_knob_dropped(self) -> None:
        """effect key in knobs should not be parseable through UserStyle."""
        raw = {"knobs": {"effect": "glow"}}
        # coerce_user_style is non-raising; bad knob may raise or return None
        result = coerce_user_style(raw)
        # If it coerces successfully, the result should not have 'effect' in knobs
        if result is not None and result.knobs is not None:
            assert not hasattr(result.knobs, "effect")


class TestUserStyleKnobsDict:
    def test_returns_only_non_none(self) -> None:
        style = UserStyle(
            knobs=StyleKnobs(font_family="Playfair Display", text_size_px=56),
            status="ready",
        )
        d = user_style_knobs_dict(style)
        assert d["font_family"] == "Playfair Display"
        assert d["text_size_px"] == 56
        # None fields must not appear
        assert "position" not in d or d["position"] is not None

    def test_no_knobs_returns_empty(self) -> None:
        style = UserStyle(status="ready")
        d = user_style_knobs_dict(style)
        assert isinstance(d, dict)
        assert len(d) == 0

    def test_none_style_returns_empty(self) -> None:
        assert user_style_knobs_dict(None) == {}


class TestByteIdentityContract:
    """No user_style key in all_candidates → build_generative_job output is baseline."""

    def test_build_generative_job_no_user_style_key_when_disabled(self) -> None:
        """When user_style_enabled=False, 'user_style' key absent from all_candidates."""
        from unittest.mock import patch

        from app.services.generative_jobs import build_generative_job

        with patch("app.config.settings") as mock_settings:
            mock_settings.user_style_enabled = False
            job = build_generative_job(
                user_id=__import__("uuid").uuid4(),
                clip_paths=["music-uploads/test.mp4"],
                user_style={"style_set_id": "editorial", "status": "ready"},
            )
        assert "user_style" not in (job.all_candidates or {}), (
            "user_style must not appear in all_candidates when user_style_enabled=False"
        )

    def test_build_generative_job_no_user_style_key_when_style_none(self) -> None:
        """When user_style is None, 'user_style' key absent from all_candidates."""
        from unittest.mock import patch

        from app.services.generative_jobs import build_generative_job

        with patch("app.config.settings") as mock_settings:
            mock_settings.user_style_enabled = True
            job = build_generative_job(
                user_id=__import__("uuid").uuid4(),
                clip_paths=["music-uploads/test.mp4"],
                user_style=None,
            )
        assert "user_style" not in (job.all_candidates or {}), (
            "user_style must not appear in all_candidates when style is None"
        )
