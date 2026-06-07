"""Tests for StyleDerivationAgent.parse() (Creator Agent M1).

No real model calls — exercises the parse() validation + coercion logic
against raw JSON strings, confirming:
  - unknown style_set_id → coerced to 'default'.
  - unknown font_family → dropped (not in font_vibes catalog).
  - invalid instruction_level → defaults to 'full'.
  - extra/parity-unsafe knob keys (e.g. 'effect') → dropped.
  - valid JSON round-trips cleanly.
  - bad JSON → SchemaError.
  - footage_type_bias and preferred_edit_format_mix filtered to known enums.
  - position / text_anchor filtered to known values.
  - stroke_width / position_x_frac / position_y_frac parsed correctly.

Skia-free (no libEGL import path touched) — safe for CI evals job.
"""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import SchemaError
from app.agents.style_derivation import (
    FontEntry,
    StyleDerivationAgent,
    StyleDerivationInput,
    StyleSetEntry,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sets(*ids: str) -> list[StyleSetEntry]:
    return [StyleSetEntry(id=i, label=i.replace("_", " ").title()) for i in ids]


def _fonts(*names: str) -> list[FontEntry]:
    return [FontEntry(name=n, vibe="editorial", category="serif") for n in names]


def _input(
    set_ids=("editorial_serif", "film_mono"),
    font_names=("Playfair Display", "Bodoni Moda"),
):
    return StyleDerivationInput(
        persona_summary="City-walk aesthetic creator",
        persona_pillars=["urban photography"],
        persona_tone="calm and cinematic",
        persona_audience="travel fans",
        available_sets=_sets(*set_ids),
        font_vibes=_fonts(*font_names),
    )


def _raw(overrides: dict | None = None) -> str:
    base = {
        "style_set_id": "editorial_serif",
        "knobs": {"font_family": "Playfair Display", "text_size_px": 56},
        "footage_type_bias": ["broll"],
        "preferred_edit_format_mix": {"montage": 0.8},
        "instruction_level": "full",
        "rationale": "Fits the calm aesthetic.",
    }
    if overrides:
        base.update(overrides)
    return json.dumps(base)


def _agent() -> StyleDerivationAgent:
    # We call parse() directly so no real model client is needed.
    return StyleDerivationAgent(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------


class TestStyleDerivationAgentParseHappyPath:
    def test_valid_json_round_trips(self):
        out = _agent().parse(_raw(), _input())
        assert out.style.style_set_id == "editorial_serif"
        assert out.style.knobs is not None
        assert out.style.knobs.font_family == "Playfair Display"
        assert out.style.knobs.text_size_px == 56
        assert out.style.instruction_level == "full"
        assert out.style.status == "ready"

    def test_style_version_set(self):
        out = _agent().parse(_raw(), _input())
        assert out.style.style_version  # non-empty

    def test_footage_bias_preserved(self):
        out = _agent().parse(_raw(), _input())
        assert "broll" in out.style.footage_type_bias

    def test_edit_format_mix_clamped(self):
        raw = _raw({"preferred_edit_format_mix": {"montage": 1.5, "talking_head": -0.2}})
        out = _agent().parse(raw, _input())
        assert out.style.preferred_edit_format_mix["montage"] == 1.0
        assert out.style.preferred_edit_format_mix["talking_head"] == 0.0


# ---------------------------------------------------------------------------
# Unknown set → default
# ---------------------------------------------------------------------------


class TestStyleDerivationParseUnknownSet:
    def test_unknown_set_id_coerced_to_default(self):
        """If agent returns a set_id not in the catalog, it's coerced to 'default'."""
        raw = _raw({"style_set_id": "does_not_exist"})
        out = _agent().parse(raw, _input())
        assert out.style.style_set_id == "default"

    def test_empty_set_id_coerced_to_default(self):
        raw = _raw({"style_set_id": ""})
        out = _agent().parse(raw, _input())
        assert out.style.style_set_id == "default"

    def test_default_always_valid(self):
        """'default' is always a valid set_id even if not in available_sets."""
        raw = _raw({"style_set_id": "default"})
        out = _agent().parse(raw, _input(set_ids=("editorial_serif",)))
        assert out.style.style_set_id == "default"


# ---------------------------------------------------------------------------
# Unknown font → dropped
# ---------------------------------------------------------------------------


class TestStyleDerivationParseUnknownFont:
    def test_unknown_font_dropped_from_knobs(self):
        """Font not in font_vibes catalog must be silently dropped."""
        raw = _raw({"knobs": {"font_family": "Comic Sans MS", "text_size_px": 56}})
        out = _agent().parse(raw, _input())
        assert out.style.knobs is not None
        assert out.style.knobs.font_family is None

    def test_valid_font_retained(self):
        raw = _raw({"knobs": {"font_family": "Bodoni Moda"}})
        out = _agent().parse(raw, _input())
        assert out.style.knobs is not None
        assert out.style.knobs.font_family == "Bodoni Moda"


# ---------------------------------------------------------------------------
# Parity-unsafe knob keys → dropped
# ---------------------------------------------------------------------------


class TestStyleDerivationParseParityUnsafeKnobs:
    def test_effect_knob_dropped(self):
        """'effect' is NOT in StyleKnobs (parity-unsafe #296); must be silently dropped."""
        raw = _raw({"knobs": {"font_family": "Playfair Display", "effect": "glow"}})
        out = _agent().parse(raw, _input())
        knobs_dict = out.style.knobs.model_dump() if out.style.knobs else {}
        assert "effect" not in knobs_dict

    def test_arbitrary_unknown_key_dropped(self):
        """Any key not in StyleKnobs.model_fields is silently dropped."""
        raw = _raw({"knobs": {"font_family": "Playfair Display", "made_up_key": "value"}})
        out = _agent().parse(raw, _input())
        knobs_dict = out.style.knobs.model_dump() if out.style.knobs else {}
        assert "made_up_key" not in knobs_dict


# ---------------------------------------------------------------------------
# instruction_level coercion
# ---------------------------------------------------------------------------


class TestStyleDerivationParseInstructionLevel:
    def test_invalid_instruction_level_defaults_to_full(self):
        raw = _raw({"instruction_level": "ultra"})
        out = _agent().parse(raw, _input())
        assert out.style.instruction_level == "full"

    def test_valid_instruction_level_none(self):
        raw = _raw({"instruction_level": "none"})
        out = _agent().parse(raw, _input())
        assert out.style.instruction_level == "none"

    def test_valid_instruction_level_light(self):
        raw = _raw({"instruction_level": "light"})
        out = _agent().parse(raw, _input())
        assert out.style.instruction_level == "light"


# ---------------------------------------------------------------------------
# footage_type_bias and preferred_edit_format_mix filtering
# ---------------------------------------------------------------------------


class TestStyleDerivationParseEnumFiltering:
    def test_unknown_footage_type_filtered(self):
        raw = _raw({"footage_type_bias": ["broll", "aerial_drone", "talking_head"]})
        out = _agent().parse(raw, _input())
        # "aerial_drone" is not a valid footage type; it must be removed.
        assert "aerial_drone" not in out.style.footage_type_bias
        assert "broll" in out.style.footage_type_bias
        assert "talking_head" in out.style.footage_type_bias

    def test_unknown_edit_format_filtered(self):
        raw = _raw({"preferred_edit_format_mix": {"montage": 0.8, "cinematic_reel": 0.5}})
        out = _agent().parse(raw, _input())
        assert "cinematic_reel" not in out.style.preferred_edit_format_mix
        assert "montage" in out.style.preferred_edit_format_mix


# ---------------------------------------------------------------------------
# Knob field-level parsing
# ---------------------------------------------------------------------------


class TestStyleDerivationParseKnobFields:
    def test_position_filtered_to_valid_values(self):
        raw = _raw({"knobs": {"position": "sky"}})
        out = _agent().parse(raw, _input())
        assert out.style.knobs is not None
        assert out.style.knobs.position is None  # unknown → dropped

    def test_valid_position_retained(self):
        raw = _raw({"knobs": {"position": "bottom"}})
        out = _agent().parse(raw, _input())
        assert out.style.knobs is not None
        assert out.style.knobs.position == "bottom"

    def test_text_anchor_filtered(self):
        raw = _raw({"knobs": {"text_anchor": "justify"}})  # not a valid anchor
        out = _agent().parse(raw, _input())
        assert out.style.knobs is not None
        assert out.style.knobs.text_anchor is None

    def test_stroke_width_zero_parsed(self):
        raw = _raw({"knobs": {"stroke_width": 0}})
        out = _agent().parse(raw, _input())
        assert out.style.knobs is not None
        assert out.style.knobs.stroke_width == 0

    def test_position_x_frac_zero_parsed(self):
        raw = _raw({"knobs": {"position_x_frac": 0.0}})
        out = _agent().parse(raw, _input())
        assert out.style.knobs is not None
        assert out.style.knobs.position_x_frac == 0.0

    def test_position_y_frac_out_of_range_dropped(self):
        raw = _raw({"knobs": {"position_y_frac": 1.5}})
        out = _agent().parse(raw, _input())
        assert out.style.knobs is not None
        assert out.style.knobs.position_y_frac is None

    def test_text_color_invalid_hex_dropped(self):
        raw = _raw({"knobs": {"text_color": "blue"}})  # no '#'
        out = _agent().parse(raw, _input())
        assert out.style.knobs is not None
        assert out.style.knobs.text_color is None

    def test_text_color_valid_hex_retained(self):
        raw = _raw({"knobs": {"text_color": "#FFFFFF"}})
        out = _agent().parse(raw, _input())
        assert out.style.knobs is not None
        assert out.style.knobs.text_color == "#FFFFFF"

    def test_cycle_fonts_non_bool_dropped(self):
        raw = _raw({"knobs": {"cycle_fonts": "yes"}})  # string, not bool
        out = _agent().parse(raw, _input())
        assert out.style.knobs is not None
        assert out.style.knobs.cycle_fonts is None

    def test_cycle_fonts_bool_retained(self):
        raw = _raw({"knobs": {"cycle_fonts": True}})
        out = _agent().parse(raw, _input())
        assert out.style.knobs is not None
        assert out.style.knobs.cycle_fonts is True


# ---------------------------------------------------------------------------
# Bad JSON → SchemaError
# ---------------------------------------------------------------------------


class TestStyleDerivationParseBadInput:
    def test_invalid_json_raises_schema_error(self):
        with pytest.raises(SchemaError, match="invalid JSON"):
            _agent().parse("not json at all", _input())

    def test_json_array_raises_schema_error(self):
        with pytest.raises(SchemaError, match="not a JSON object"):
            _agent().parse("[1, 2, 3]", _input())

    def test_empty_object_produces_defaults(self):
        """An empty JSON object is technically valid — all fields default."""
        out = _agent().parse("{}", _input())
        # Unknown set → 'default'.
        assert out.style.style_set_id == "default"
        assert out.style.instruction_level == "full"
