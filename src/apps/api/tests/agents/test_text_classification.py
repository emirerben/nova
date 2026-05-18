"""Unit tests for TextClassificationAgent.

Covers:
  - happy path (3 phrases, all valid classifications)
  - per-enum coverage (effect, role, size_class each flow through correctly)
  - invalid effect from LLM → clamped to 'none', warning logged
  - invalid hex from LLM → clamped to '#FFFFFF', warning logged
  - malformed JSON → SchemaError raised
  - empty input (0 phrases) → 0 classified out, no LLM call
  - partial coverage (Gemini returns 2 of 3 classifications) → missing phrase
    gets default values, no crash

Testing conventions match test_template_text_parse.py:
  - parse() is tested in isolation (model client = None)
  - run() with MockModelClient for end-to-end flow
  - no real LLM or filesystem access
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.agents._runtime import SchemaError
from app.agents._schemas.text_classification import (
    TextClassificationInput,
    TextClassificationOutput,
)
from app.agents._schemas.text_overlay_pipeline import Phrase
from app.agents.text_classification import TextClassificationAgent
from tests.agents.conftest import MockModelClient

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _phrase(
    lines: list[str],
    start: float = 0.0,
    end: float = 2.0,
    aabb: tuple[float, float, float, float] = (0.2, 0.1, 0.8, 0.3),
) -> Phrase:
    return Phrase(
        lines=lines,
        start_t_s=start,
        end_t_s=end,
        aabb=aabb,
        mean_confidence=1.0,
    )


def _three_phrases() -> list[Phrase]:
    return [
        _phrase(["It's not just luck"], start=0.3, end=2.0),
        _phrase(["if you", "put in"], start=2.3, end=4.5),
        _phrase(["The work to get there"], start=4.8, end=6.3),
    ]


def _make_input(
    phrases: list[Phrase] | None = None,
    frame_paths: dict[int, Path] | None = None,
) -> TextClassificationInput:
    return TextClassificationInput(
        phrases=phrases if phrases is not None else _three_phrases(),
        frame_paths=frame_paths or {},
    )


def _agent() -> TextClassificationAgent:
    """Agent with no model client — parse() can be exercised directly."""
    return TextClassificationAgent(None)  # type: ignore[arg-type]


def _valid_classifications(n: int = 3) -> dict[str, Any]:
    items = []
    for i in range(n):
        items.append(
            {
                "phrase_index": i,
                "effect": "typewriter",
                "role": "hook",
                "size_class": "large",
                "font_color_hex": "#FFFFFF",
            }
        )
    return {"classifications": items}


# ── Happy path ────────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_parse_three_phrases_all_classified(self) -> None:
        raw = json.dumps(_valid_classifications(3))
        out = _agent().parse(raw, _make_input())

        assert len(out.classified) == 3
        assert all(cp.effect == "typewriter" for cp in out.classified)
        assert all(cp.role == "hook" for cp in out.classified)
        assert all(cp.size_class == "large" for cp in out.classified)
        assert all(cp.font_color_hex == "#FFFFFF" for cp in out.classified)

    def test_parse_phrase_objects_preserved(self) -> None:
        phrases = _three_phrases()
        raw = json.dumps(_valid_classifications(3))
        out = _agent().parse(raw, _make_input(phrases=phrases))

        assert out.classified[0].phrase.sample_text == "It's not just luck"
        assert out.classified[1].phrase.sample_text == "if you\nput in"
        assert out.classified[2].phrase.sample_text == "The work to get there"

    def test_run_with_mock_client_happy_path(self) -> None:
        client = MockModelClient()
        client.queue("gemini-2.5-flash", _valid_classifications(3))
        agent = TextClassificationAgent(client)
        out = agent.run(_make_input())

        assert isinstance(out, TextClassificationOutput)
        assert len(out.classified) == 3


# ── Per-enum coverage ─────────────────────────────────────────────────────────


class TestPerEnumCoverage:
    @pytest.mark.parametrize(
        "effect",
        [
            "pop-in",
            "fade-in",
            "scale-up",
            "font-cycle",
            "typewriter",
            "glitch",
            "bounce",
            "slide-in",
            "slide-up",
            "static",
            "none",
        ],
    )
    def test_valid_effect_passes_through(self, effect: str) -> None:
        raw = json.dumps(
            {
                "classifications": [
                    {
                        "phrase_index": 0,
                        "effect": effect,
                        "role": "label",
                        "size_class": "medium",
                        "font_color_hex": "#000000",
                    }
                ]
            }
        )
        phrases = [_phrase(["test"])]
        out = _agent().parse(raw, _make_input(phrases=phrases))
        assert out.classified[0].effect == effect

    @pytest.mark.parametrize("role", ["hook", "reaction", "cta", "label"])
    def test_valid_role_passes_through(self, role: str) -> None:
        raw = json.dumps(
            {
                "classifications": [
                    {
                        "phrase_index": 0,
                        "effect": "none",
                        "role": role,
                        "size_class": "medium",
                        "font_color_hex": "#000000",
                    }
                ]
            }
        )
        phrases = [_phrase(["test"])]
        out = _agent().parse(raw, _make_input(phrases=phrases))
        assert out.classified[0].role == role

    @pytest.mark.parametrize("size_class", ["small", "medium", "large", "jumbo"])
    def test_valid_size_class_passes_through(self, size_class: str) -> None:
        raw = json.dumps(
            {
                "classifications": [
                    {
                        "phrase_index": 0,
                        "effect": "none",
                        "role": "label",
                        "size_class": size_class,
                        "font_color_hex": "#000000",
                    }
                ]
            }
        )
        phrases = [_phrase(["test"])]
        out = _agent().parse(raw, _make_input(phrases=phrases))
        assert out.classified[0].size_class == size_class


# ── Invalid enum from LLM → clamp ────────────────────────────────────────────


class TestInvalidEnumClamping:
    def test_invalid_effect_clamped_to_none(self) -> None:
        """Gemini returns effect='bouncing' (not in VALID_EFFECTS) → clamp to 'none'."""
        raw = json.dumps(
            {
                "classifications": [
                    {
                        "phrase_index": 0,
                        "effect": "bouncing",
                        "role": "hook",
                        "size_class": "large",
                        "font_color_hex": "#FFFFFF",
                    }
                ]
            }
        )
        phrases = [_phrase(["test"])]
        out = _agent().parse(raw, _make_input(phrases=phrases))

        assert out.classified[0].effect == "none", "out-of-enum effect must be clamped to 'none'"
        # Phrase is NOT dropped.
        assert len(out.classified) == 1

    def test_invalid_role_clamped_to_label(self) -> None:
        raw = json.dumps(
            {
                "classifications": [
                    {
                        "phrase_index": 0,
                        "effect": "none",
                        "role": "subtitle",
                        "size_class": "medium",
                        "font_color_hex": "#FFFFFF",
                    }
                ]
            }
        )
        phrases = [_phrase(["test"])]
        out = _agent().parse(raw, _make_input(phrases=phrases))
        assert out.classified[0].role == "label"

    def test_invalid_size_class_clamped_to_medium(self) -> None:
        raw = json.dumps(
            {
                "classifications": [
                    {
                        "phrase_index": 0,
                        "effect": "none",
                        "role": "label",
                        "size_class": "huge",
                        "font_color_hex": "#FFFFFF",
                    }
                ]
            }
        )
        phrases = [_phrase(["test"])]
        out = _agent().parse(raw, _make_input(phrases=phrases))
        assert out.classified[0].size_class == "medium"

    def test_invalid_hex_clamped_to_white(self) -> None:
        """Gemini returns font_color_hex='red' (not regex-valid) → clamp to '#FFFFFF'."""
        raw = json.dumps(
            {
                "classifications": [
                    {
                        "phrase_index": 0,
                        "effect": "none",
                        "role": "label",
                        "size_class": "medium",
                        "font_color_hex": "red",
                    }
                ]
            }
        )
        phrases = [_phrase(["test"])]
        out = _agent().parse(raw, _make_input(phrases=phrases))

        # Invalid hex must be clamped to #FFFFFF.
        assert out.classified[0].font_color_hex == "#FFFFFF"
        # Phrase is NOT dropped.
        assert len(out.classified) == 1

    def test_hex_missing_hash_normalised(self) -> None:
        """Gemini returns 'FFFFFF' (no leading #) → normalised to '#FFFFFF'."""
        raw = json.dumps(
            {
                "classifications": [
                    {
                        "phrase_index": 0,
                        "effect": "none",
                        "role": "label",
                        "size_class": "medium",
                        "font_color_hex": "FFFFFF",
                    }
                ]
            }
        )
        phrases = [_phrase(["test"])]
        out = _agent().parse(raw, _make_input(phrases=phrases))
        assert out.classified[0].font_color_hex == "#FFFFFF"

    def test_all_phrases_survive_invalid_enums(self) -> None:
        """Clamping fires per-field, per-phrase; no phrase is dropped."""
        raw = json.dumps(
            {
                "classifications": [
                    {
                        "phrase_index": 0,
                        "effect": "ZOOM",
                        "role": "narrator",
                        "size_class": "xl",
                        "font_color_hex": "not-a-color",
                    },
                    {
                        "phrase_index": 1,
                        "effect": "typewriter",
                        "role": "hook",
                        "size_class": "large",
                        "font_color_hex": "#F4D03F",
                    },
                ]
            }
        )
        phrases = [_phrase(["A"]), _phrase(["B"])]
        out = _agent().parse(raw, _make_input(phrases=phrases))

        assert len(out.classified) == 2
        assert out.classified[0].effect == "none"
        assert out.classified[0].role == "label"
        assert out.classified[0].size_class == "medium"
        assert out.classified[0].font_color_hex == "#FFFFFF"
        # Second phrase is correct.
        assert out.classified[1].effect == "typewriter"
        assert out.classified[1].font_color_hex == "#F4D03F"


# ── Malformed JSON ────────────────────────────────────────────────────────────


class TestMalformedInput:
    def test_invalid_json_raises_schema_error(self) -> None:
        with pytest.raises(SchemaError, match="invalid JSON"):
            _agent().parse("not-json{", _make_input())

    def test_non_object_json_raises_schema_error(self) -> None:
        with pytest.raises(SchemaError, match="not a JSON object"):
            _agent().parse(json.dumps(["a", "b"]), _make_input())

    def test_classifications_not_list_raises_schema_error(self) -> None:
        with pytest.raises(SchemaError, match="'classifications' is not a list"):
            _agent().parse(json.dumps({"classifications": "nope"}), _make_input())


# ── Empty input ───────────────────────────────────────────────────────────────


class TestEmptyInput:
    def test_zero_phrases_returns_empty_output_no_llm_call(self) -> None:
        """Early return: 0 phrases → 0 classified, no model client invoked."""
        client = MockModelClient()
        agent = TextClassificationAgent(client)
        out = agent.run(_make_input(phrases=[]))

        assert isinstance(out, TextClassificationOutput)
        assert out.classified == []
        # No queued response consumed → no LLM call was made.
        assert len(client.invocations) == 0

    def test_parse_empty_phrases_returns_empty_output(self) -> None:
        raw = json.dumps({"classifications": []})
        out = _agent().parse(raw, _make_input(phrases=[]))
        assert out.classified == []


# ── Partial coverage (Gemini returns fewer than N classifications) ─────────────


class TestPartialCoverage:
    def test_missing_phrase_gets_default_values(self) -> None:
        """Gemini returns classifications for phrases 0 and 2 but not 1.

        Policy: phrase 1 is NOT dropped — it gets all-default enum values
        (effect='none', role='label', size_class='medium', font_color_hex='#FFFFFF').
        This matches the design doc's 'degraded but not crashed' principle.
        """
        raw = json.dumps(
            {
                "classifications": [
                    {
                        "phrase_index": 0,
                        "effect": "typewriter",
                        "role": "hook",
                        "size_class": "large",
                        "font_color_hex": "#FFFFFF",
                    },
                    {
                        "phrase_index": 2,
                        "effect": "static",
                        "role": "label",
                        "size_class": "small",
                        "font_color_hex": "#000000",
                    },
                ]
            }
        )
        phrases = _three_phrases()
        out = _agent().parse(raw, _make_input(phrases=phrases))

        # All 3 phrases are in the output.
        assert len(out.classified) == 3

        # Phrase 0 — as returned by Gemini.
        assert out.classified[0].effect == "typewriter"
        assert out.classified[0].role == "hook"

        # Phrase 1 — missing from Gemini response → defaults.
        assert out.classified[1].effect == "none"
        assert out.classified[1].role == "label"
        assert out.classified[1].size_class == "medium"
        assert out.classified[1].font_color_hex == "#FFFFFF"
        # The phrase object itself is preserved.
        assert "if you" in out.classified[1].phrase.sample_text

        # Phrase 2 — as returned by Gemini.
        assert out.classified[2].effect == "static"
        assert out.classified[2].font_color_hex == "#000000"

    def test_gemini_returns_zero_classifications_all_get_defaults(self) -> None:
        """Gemini returns empty classifications list for non-empty input.

        All phrases survive with default values — no SchemaError.
        """
        raw = json.dumps({"classifications": []})
        phrases = _three_phrases()
        out = _agent().parse(raw, _make_input(phrases=phrases))

        assert len(out.classified) == 3
        assert all(cp.effect == "none" for cp in out.classified)
        assert all(cp.role == "label" for cp in out.classified)
        assert all(cp.font_color_hex == "#FFFFFF" for cp in out.classified)

    def test_phrase_objects_correct_even_when_response_out_of_order(self) -> None:
        """Gemini returns classifications in reverse order — keyed by phrase_index."""
        raw = json.dumps(
            {
                "classifications": [
                    {
                        "phrase_index": 2,
                        "effect": "fade-in",
                        "role": "cta",
                        "size_class": "small",
                        "font_color_hex": "#FF0000",
                    },
                    {
                        "phrase_index": 0,
                        "effect": "pop-in",
                        "role": "hook",
                        "size_class": "jumbo",
                        "font_color_hex": "#00FF00",
                    },
                    {
                        "phrase_index": 1,
                        "effect": "slide-up",
                        "role": "reaction",
                        "size_class": "medium",
                        "font_color_hex": "#0000FF",
                    },
                ]
            }
        )
        phrases = _three_phrases()
        out = _agent().parse(raw, _make_input(phrases=phrases))

        assert out.classified[0].effect == "pop-in"
        assert out.classified[1].effect == "slide-up"
        assert out.classified[2].effect == "fade-in"


# ── Registry ──────────────────────────────────────────────────────────────────


class TestRegistryRegistration:
    def test_agent_registered_in_registry(self) -> None:
        from app.agents._registry import get_agent

        cls = get_agent("nova.compose.text_classification")
        assert cls is TextClassificationAgent
