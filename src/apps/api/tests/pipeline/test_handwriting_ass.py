"""Classic/libass ink-reveal fallback coverage."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from app.pipeline.text_overlay import ASS_ANIMATED_EFFECTS, generate_animated_overlay_ass


def _dialogues(path: str) -> list[str]:
    return [line for line in Path(path).read_text().splitlines() if line.startswith("Dialogue:")]


def test_write_on_effects_route_through_the_ass_fallback_path():
    assert "handwriting" in ASS_ANIMATED_EFFECTS
    assert "ink-reveal" in ASS_ANIMATED_EFFECTS


def test_ink_reveal_ass_clips_the_whole_painted_block_and_settles():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = generate_animated_overlay_ass(
            [
                {
                    "text": "FIELD NOTES\nNUMBER TWO",
                    "start_s": 0.0,
                    "end_s": 4.0,
                    "position": "center",
                    "effect": "ink-reveal",
                    "font_family": "Inter",
                    "text_size_px": 96,
                    "text_color": "#FF484C",
                    "stroke_width": 3,
                    "letter_spacing": 0.04,
                    "line_spacing": 1.7,
                    "max_width_frac": 0.3,
                    "rotation_deg": -4,
                }
            ],
            4.0,
            tmpdir,
            0,
        )
        assert result is not None and len(result) == 1
        dialogues = _dialogues(result[0])
        assert len(dialogues) > 60
        assert r"\clip(" in dialogues[0]
        assert r"\bord3" in dialogues[0]
        assert r"\1c&H4C48FF&" in dialogues[0]
        assert r"\fsp3.840" in dialogues[0]
        assert r"\frz-4.000" in dialogues[0]

        # Each wrapped line gets its own positioned event so non-default line
        # spacing is honored by libass instead of its fixed leading.
        assert r"\N" not in dialogues[0]
        y_positions = [
            int(match.group(1))
            for line in dialogues[:2]
            if (match := re.search(r"\\pos\(\d+,(\d+)\)", line))
        ]
        assert len(y_positions) == 2
        assert y_positions[1] - y_positions[0] > 150

        # The settled tail drops clipping entirely, matching static text.
        assert r"\clip(" not in dialogues[-1]
        first_clip = re.search(r"\\clip\((\d+),(\d+),(\d+),(\d+)\)", dialogues[0])
        mid_clip = re.search(r"\\clip\((\d+),(\d+),(\d+),(\d+)\)", dialogues[70])
        assert first_clip and mid_clip
        assert first_clip.group(1) == first_clip.group(3)
        assert int(mid_clip.group(3)) > int(mid_clip.group(1))


def test_short_ink_reveal_ass_compresses_without_a_settled_tail():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = generate_animated_overlay_ass(
            [
                {
                    "text": "FAST",
                    "start_s": 0.0,
                    "end_s": 0.5,
                    "position": "center",
                    "effect": "ink-reveal",
                }
            ],
            1.0,
            tmpdir,
            0,
        )
        assert result is not None
        dialogues = _dialogues(result[0])
        assert 14 <= len(dialogues) <= 16
        assert all(r"\clip(" in line for line in dialogues)
