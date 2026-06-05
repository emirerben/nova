"""Drop-trailing-line rule regression.

`_select_section_lines` drops the LAST emitted line when both:

  - its clamped `start_s` lands in the last `_TRAILING_LINE_DROP_TAIL_S`
    of the section, AND
  - its clamped duration is below `_TRAILING_LINE_DROP_MIN_DUR_S`.

Use case: a 20s lyrics-preview window where the last line's text barely
overlaps the section tail, but no word actually starts inside that tail.
Better to drop that flash entirely. If a word does start in the tail, keep
the line so the preview can render the whole started word.

The rule fires ONLY on the last emitted line by tail position — a
legitimately short fully-contained ad-lib mid-section ("yeah!", etc.)
is unaffected.
"""

from __future__ import annotations

import pytest

from app.pipeline.lyric_injector import (
    _TRAILING_LINE_DROP_MIN_DUR_S,
    _TRAILING_LINE_DROP_TAIL_S,
    _select_section_lines,
)


def _mk_line(text: str, start_s: float, end_s: float) -> dict:
    """Minimal lyrics_cached line shape that `_select_section_lines` accepts."""
    return {
        "text": text,
        "start_s": start_s,
        "end_s": end_s,
        "words": [{"text": text.split()[0], "start_s": start_s, "end_s": end_s}],
    }


class TestTrailingDropRule:
    def test_drops_trailing_flash_at_section_tail_without_started_word(self) -> None:
        """A line whose clamped duration is < 1.0s AND whose clamped start
        is in the last 1.0s of the section gets dropped when no word starts
        inside the renderable section."""
        # Section [10, 10.75]. The line overlaps for 0.75s, but its only word
        # started at 9.0 — before the preview audio. This is a visual tail, not
        # a started lyric word.
        lines = [_mk_line("Already started", 9.0, 10.75)]
        out = _select_section_lines(lines, best_start_s=10.0, best_end_s=10.75)
        assert out == []

    def test_keeps_trailing_line_when_word_starts_in_tail(self) -> None:
        """A short tail line is kept once a word starts before the section cut."""
        lines = [
            _mk_line("normal line", 5.0, 9.0),
            _mk_line("Marvellous", 19.45, 21.27),
        ]

        out = _select_section_lines(lines, best_start_s=0.0, best_end_s=20.0)

        assert len(out) == 2
        assert out[-1]["text"] == "Marvellous"

    def test_does_not_drop_mid_section_short_ad_lib(self) -> None:
        """A short line mid-section (not the last line) must NOT be dropped.
        The rule only fires on the trailing line by tail position."""
        lines = [
            _mk_line("normal line", 1.0, 5.0),
            _mk_line("yeah!", 10.0, 10.3),  # short ad-lib mid-section
            _mk_line("another normal line", 14.0, 18.0),
        ]
        out = _select_section_lines(lines, best_start_s=0.0, best_end_s=20.0)
        # Three lines preserved: the short ad-lib is not the last line,
        # and the actual last line is comfortably above the min-dur threshold.
        assert len(out) == 3
        assert [ln["text"] for ln in out] == [
            "normal line",
            "yeah!",
            "another normal line",
        ]

    def test_does_not_drop_long_trailing_line(self) -> None:
        """A line that lands in the section tail but has plenty of clamped
        duration (e.g. the line starts before the tail-threshold) must NOT
        be dropped — the rule requires BOTH conditions."""
        lines = [
            _mk_line("normal line", 1.0, 5.0),
            _mk_line("long trailing line that fills the rest", 10.0, 22.0),
        ]
        out = _select_section_lines(lines, best_start_s=0.0, best_end_s=20.0)
        # The trailing line is clamped to 10.0-20.0 (10s), so duration
        # comfortably > 1.0s threshold even though end clamps at section end.
        # AND start is at section 10.0 which is NOT in the last 1.0s of
        # the section (which is [19.0, 20.0]).
        assert len(out) == 2

    def test_does_not_drop_trailing_line_starting_just_before_tail(self) -> None:
        """A line starting at section 18.5 (NOT in the last 1.0s of a 20s
        section, since 20.0 − 1.0 = 19.0) keeps its 1.5s clamped duration
        and renders normally."""
        lines = [
            _mk_line("normal line", 1.0, 5.0),
            _mk_line("trailing line just before tail", 18.5, 25.0),
        ]
        out = _select_section_lines(lines, best_start_s=0.0, best_end_s=20.0)
        # Start at 18.5 is BEFORE the tail threshold (19.0). Rule does not fire.
        assert len(out) == 2
        assert out[-1]["text"] == "trailing line just before tail"
        assert out[-1]["start_s"] == pytest.approx(18.5, abs=1e-3)
        assert out[-1]["end_s"] == pytest.approx(20.0, abs=1e-3)  # clamped to section end

    def test_constants_documented_values(self) -> None:
        """The thresholds are public module constants so callers (and this
        test) can reason about them. Lock the current values; any change
        deserves a deliberate test update."""
        assert _TRAILING_LINE_DROP_TAIL_S == pytest.approx(1.0, abs=1e-6)
        assert _TRAILING_LINE_DROP_MIN_DUR_S == pytest.approx(1.0, abs=1e-6)
