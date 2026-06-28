"""Unit tests for StyleObservationAgent.parse() — coercion discipline and edge cases.

These tests run in CI without Gemini (parse() is a pure data transform).
Vision evaluation (live Gemini call over a real video) lives in
``tests/evals/test_style_observation_evals.py`` and is gated behind --eval-mode=live.
"""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import SchemaError
from app.agents.style_observation import (
    StyleObservationAgent,
    StyleObservationInput,
    VideoStyleObservation,
    _coerce_confidence,
    _coerce_font_feel,
    _coerce_hex_color,
    _coerce_optional_literal,
    _FONT_FEELS,
    _POSITIONS,
    _SIZE_CLASSES,
    _LAYOUTS,
    _STROKES,
    _ANCHORS,
)


def _make_input(file_uri: str = "files/abc") -> StyleObservationInput:
    return StyleObservationInput(file_uri=file_uri, caption="test caption", view_index=None)


def _agent() -> StyleObservationAgent:
    """parse() doesn't touch the model client."""
    return StyleObservationAgent(model_client=None)  # type: ignore[arg-type]


# ── Coercion helpers ──────────────────────────────────────────────────────────

class TestCoercionHelpers:
    def test_coerce_font_feel_valid(self):
        for ff in _FONT_FEELS:
            assert _coerce_font_feel(ff) == ff

    def test_coerce_font_feel_unknown(self):
        assert _coerce_font_feel("Times New Roman") == "none"
        assert _coerce_font_feel(None) == "none"
        assert _coerce_font_feel(42) == "none"

    def test_coerce_font_feel_case_insensitive(self):
        assert _coerce_font_feel("Serif_Editorial") == "serif_editorial"
        assert _coerce_font_feel("BOLD_DISPLAY") == "bold_display"

    def test_coerce_hex_color_valid(self):
        assert _coerce_hex_color("#ffffff") == "#ffffff"
        assert _coerce_hex_color("#FFD24A") == "#ffd24a"  # normalized to lowercase
        assert _coerce_hex_color("#000") == "#000"

    def test_coerce_hex_color_invalid(self):
        assert _coerce_hex_color("white") is None
        assert _coerce_hex_color("rgb(255,255,255)") is None
        assert _coerce_hex_color(None) is None
        assert _coerce_hex_color("") is None
        assert _coerce_hex_color("#gggggg") is None

    def test_coerce_optional_literal_valid(self):
        for p in _POSITIONS:
            assert _coerce_optional_literal(p, _POSITIONS) == p

    def test_coerce_optional_literal_invalid(self):
        assert _coerce_optional_literal("middle", _POSITIONS) is None
        assert _coerce_optional_literal(None, _POSITIONS) is None

    def test_coerce_confidence_clamped(self):
        assert _coerce_confidence(1.5) == pytest.approx(1.0)
        assert _coerce_confidence(-0.1) == pytest.approx(0.0)
        assert _coerce_confidence(0.7) == pytest.approx(0.7)
        assert _coerce_confidence("oops") == pytest.approx(0.5)


# ── parse() — happy paths ─────────────────────────────────────────────────────

class TestStyleObservationParse:
    def test_no_text(self):
        """A video with no on-screen text returns early with defaults."""
        raw = json.dumps({"has_on_screen_text": False, "confidence": 0.95})
        out = _agent().parse(raw, _make_input())
        assert out.has_on_screen_text is False
        assert out.font_feel == "none"
        assert out.text_color_hex is None
        assert out.confidence == pytest.approx(0.95)

    def test_full_observation(self):
        """All fields present — all coerced correctly."""
        raw = json.dumps({
            "has_on_screen_text": True,
            "font_feel": "serif_editorial",
            "text_color_hex": "#FFFFFF",
            "highlight_color_hex": "#FFD24A",
            "position": "center",
            "size_class": "medium",
            "layout": "linear",
            "stroke": "none",
            "text_anchor": "center",
            "confidence": 0.9,
        })
        out = _agent().parse(raw, _make_input())
        assert out.has_on_screen_text is True
        assert out.font_feel == "serif_editorial"
        assert out.text_color_hex == "#ffffff"  # normalized lowercase
        assert out.highlight_color_hex == "#ffd24a"
        assert out.position == "center"
        assert out.size_class == "medium"
        assert out.layout == "linear"
        assert out.stroke == "none"
        assert out.text_anchor == "center"
        assert out.confidence == pytest.approx(0.9)

    def test_unknown_font_feel_coerced_to_none(self):
        """An unknown font name must coerce to 'none', NOT raise."""
        raw = json.dumps({"has_on_screen_text": True, "font_feel": "Times New Roman"})
        out = _agent().parse(raw, _make_input())
        assert out.font_feel == "none"

    def test_unknown_position_coerced_to_none(self):
        raw = json.dumps({"has_on_screen_text": True, "position": "middle", "font_feel": "clean_sans"})
        out = _agent().parse(raw, _make_input())
        assert out.position is None

    def test_invalid_hex_color_coerced_to_none(self):
        raw = json.dumps({
            "has_on_screen_text": True,
            "font_feel": "bold_display",
            "text_color_hex": "yellow",
            "highlight_color_hex": "rgb(255,0,0)",
        })
        out = _agent().parse(raw, _make_input())
        assert out.text_color_hex is None
        assert out.highlight_color_hex is None

    def test_no_text_ignores_other_fields(self):
        """When has_on_screen_text=False, other fields should not be surfaced."""
        raw = json.dumps({
            "has_on_screen_text": False,
            "font_feel": "bold_display",   # present but should be ignored
            "text_color_hex": "#ff0000",
        })
        out = _agent().parse(raw, _make_input())
        assert out.has_on_screen_text is False
        assert out.font_feel == "none"      # reset to 'none', not 'bold_display'
        assert out.text_color_hex is None

    def test_partial_fields_ok(self):
        """Only font_feel present for a text video — other fields should be None."""
        raw = json.dumps({"has_on_screen_text": True, "font_feel": "handwritten"})
        out = _agent().parse(raw, _make_input())
        assert out.font_feel == "handwritten"
        assert out.position is None
        assert out.size_class is None
        assert out.layout is None
        assert out.stroke is None
        assert out.text_anchor is None

    def test_confidence_clamped(self):
        raw = json.dumps({"has_on_screen_text": True, "font_feel": "clean_sans", "confidence": 2.5})
        out = _agent().parse(raw, _make_input())
        assert out.confidence == pytest.approx(1.0)

    def test_cluster_layout(self):
        raw = json.dumps({
            "has_on_screen_text": True,
            "font_feel": "bold_display",
            "layout": "cluster",
            "size_class": "large",
        })
        out = _agent().parse(raw, _make_input())
        assert out.layout == "cluster"
        assert out.size_class == "large"


# ── parse() — error paths ─────────────────────────────────────────────────────

class TestStyleObservationParseErrors:
    def test_invalid_json(self):
        with pytest.raises(SchemaError, match="invalid JSON"):
            _agent().parse("not json", _make_input())

    def test_non_dict_response(self):
        with pytest.raises(SchemaError, match="not a JSON object"):
            _agent().parse(json.dumps(["list", "not", "dict"]), _make_input())

    def test_missing_required_field(self):
        """has_on_screen_text is the only required field."""
        with pytest.raises(SchemaError, match="missing required field"):
            _agent().parse(json.dumps({"font_feel": "clean_sans"}), _make_input())

    def test_null_has_on_screen_text(self):
        """Explicit null should raise, not silently treat as False."""
        with pytest.raises(SchemaError, match="missing required field"):
            _agent().parse(json.dumps({"has_on_screen_text": None}), _make_input())


# ── Vocabulary exhaustiveness ──────────────────────────────────────────────────

class TestVocabularyExhaustiveness:
    """Guard: every value in the Literal vocabulary must survive a parse round-trip."""

    @pytest.mark.parametrize("ff", _FONT_FEELS)
    def test_all_font_feels_survive(self, ff: str):
        raw = json.dumps({"has_on_screen_text": True, "font_feel": ff})
        out = _agent().parse(raw, _make_input())
        assert out.font_feel == ff

    @pytest.mark.parametrize("pos", _POSITIONS)
    def test_all_positions_survive(self, pos: str):
        raw = json.dumps({"has_on_screen_text": True, "position": pos})
        out = _agent().parse(raw, _make_input())
        assert out.position == pos

    @pytest.mark.parametrize("sc", _SIZE_CLASSES)
    def test_all_size_classes_survive(self, sc: str):
        raw = json.dumps({"has_on_screen_text": True, "size_class": sc})
        out = _agent().parse(raw, _make_input())
        assert out.size_class == sc

    @pytest.mark.parametrize("ly", _LAYOUTS)
    def test_all_layouts_survive(self, ly: str):
        raw = json.dumps({"has_on_screen_text": True, "layout": ly})
        out = _agent().parse(raw, _make_input())
        assert out.layout == ly

    @pytest.mark.parametrize("st", _STROKES)
    def test_all_strokes_survive(self, st: str):
        raw = json.dumps({"has_on_screen_text": True, "stroke": st})
        out = _agent().parse(raw, _make_input())
        assert out.stroke == st

    @pytest.mark.parametrize("anchor", _ANCHORS)
    def test_all_anchors_survive(self, anchor: str):
        raw = json.dumps({"has_on_screen_text": True, "text_anchor": anchor})
        out = _agent().parse(raw, _make_input())
        assert out.text_anchor == anchor


# ── Input schema ──────────────────────────────────────────────────────────────

class TestStyleObservationInput:
    def test_defaults(self):
        inp = StyleObservationInput(file_uri="files/xyz")
        assert inp.file_mime == "video/mp4"
        assert inp.caption == ""
        assert inp.view_index is None

    def test_with_view_index(self):
        inp = StyleObservationInput(file_uri="files/xyz", view_index=3.2)
        assert inp.view_index == pytest.approx(3.2)
