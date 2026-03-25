"""Tests for pipeline/ass_utils.py — shared ASS text sanitization and time formatting."""

from app.pipeline.ass_utils import format_ass_time, sanitize_ass_text


class TestSanitizeAssText:
    def test_strips_override_tags(self):
        assert sanitize_ass_text(r"{\b1}hello{\i1}") == "hello"

    def test_preserves_clean_text(self):
        assert sanitize_ass_text("hello world") == "hello world"

    def test_empty_string(self):
        assert sanitize_ass_text("") == ""

    def test_strips_brace_blocks(self):
        # Matched braces are treated as override blocks and stripped
        assert sanitize_ass_text("text { leftover }") == "text"

    def test_strips_orphan_braces(self):
        assert sanitize_ass_text("text { orphan") == "text  orphan"

    def test_strips_backslashes(self):
        assert sanitize_ass_text("back\\slash") == "backslash"

    def test_converts_newlines(self):
        assert sanitize_ass_text("line1\nline2") == "line1\\Nline2"

    def test_whitespace_only_returns_empty(self):
        assert sanitize_ass_text("   ") == ""


class TestFormatAssTime:
    def test_zero(self):
        assert format_ass_time(0.0) == "0:00:00.00"

    def test_simple_seconds(self):
        assert format_ass_time(5.5) == "0:00:05.50"

    def test_minutes_and_seconds(self):
        assert format_ass_time(90.5) == "0:01:30.50"

    def test_hours(self):
        assert format_ass_time(3661.25) == "1:01:01.25"

    def test_negative_clamped_to_zero(self):
        assert format_ass_time(-5.0) == "0:00:00.00"
