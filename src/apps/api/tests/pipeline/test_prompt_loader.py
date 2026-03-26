"""Tests for prompt_loader.py — lazy loading, fallback, and substitution."""

import os
from unittest.mock import patch

from app.pipeline.prompt_loader import _PROMPTS_DIR, clear_cache, load_prompt


class TestLoadPrompt:
    def setup_method(self):
        clear_cache()

    def test_load_existing_prompt_file(self):
        """Loading a known prompt returns non-empty content from disk."""
        result = load_prompt("analyze_template_pass1")
        assert len(result) > 50
        assert "editing style" in result

    def test_fallback_on_missing_file(self):
        """A prompt name with no file and no inline default returns empty string."""
        result = load_prompt("totally_nonexistent_prompt_xyz")
        assert result == ""

    def test_safe_substitute_with_dollar_variables(self):
        """$variable placeholders are substituted correctly."""
        result = load_prompt("analyze_clip", segment_instruction="Analyze 0.0s to 5.0s.")
        assert "Analyze 0.0s to 5.0s." in result
        assert "$segment_instruction" not in result

    def test_missing_variable_uses_safe_substitute(self):
        """Missing variables are left as-is (not KeyError)."""
        result = load_prompt("analyze_clip")
        # $segment_instruction was not provided, should remain as literal
        assert "$segment_instruction" in result

    def test_inline_default_used_when_file_unreadable(self):
        """If the prompt file can't be read, falls back to inline default."""
        from pathlib import Path

        clear_cache()
        with patch("app.pipeline.prompt_loader._PROMPTS_DIR", Path("/nonexistent/dir")):
            from app.pipeline.prompt_loader import _get_raw

            clear_cache()
            result = _get_raw("analyze_clip")
            assert "transcript" in result  # inline default has this

    def test_cache_returns_same_result(self):
        """Second call returns cached value."""
        r1 = load_prompt("analyze_template_pass1")
        r2 = load_prompt("analyze_template_pass1")
        assert r1 == r2

    def test_schema_file_loads(self):
        """The shared schema file loads and contains key fields."""
        result = load_prompt("analyze_template_schema")
        assert "shot_count" in result
        assert "creative_direction" in result
        assert "transition_in" in result
        assert "color_hint" in result
        assert "speed_factor" in result
        assert "sync_style" in result
