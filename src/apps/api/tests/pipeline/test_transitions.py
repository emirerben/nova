"""Tests for transitions.py — xfade filter chain building.

NOTE on the "none" transition: prior to the Dimples Passport truncation fix,
_build_xfade_filter handled "none" by emitting xfade=fade:duration=0.001 to
keep chain logic uniform. That hack caused FFmpeg to drop frames in long
chains (15+ hard-cuts) and truncate output to ~3-4s. The contract now is:
join_with_transitions REJECTS "none" outright (caller pre-groups slots via
_join_or_concat). The rejection is covered by
tests/pipeline/test_compositor.py::TestJoinWithTransitionsRejectsNone.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.transitions import (
    TransitionError,
    _build_xfade_filter,
    join_with_transitions,
)

# ── _build_xfade_filter unit tests ───────────────────────────────────────────


class TestBuildXfadeFilter:
    def test_two_slots_crossfade(self):
        """Happy path: 2 slots with a crossfade between them."""
        result = _build_xfade_filter(["crossfade"], [5.0, 5.0])
        assert "xfade=transition=fade" in result
        assert "duration=0.300" in result
        # Offset = slot0_dur - trans_dur = 5.0 - 0.3 = 4.7
        assert "offset=4.700" in result

    def test_three_slots_offset_accumulation(self):
        """Offsets accumulate correctly across 3 slots."""
        result = _build_xfade_filter(
            ["crossfade", "fade_black"],
            [5.0, 4.0, 3.0],
        )
        parts = result.split(";")
        assert len(parts) == 2
        assert "fadeblack" in parts[1]

    def test_different_slot_durations(self):
        """Different slot durations produce correct offsets."""
        result = _build_xfade_filter(["crossfade"], [3.0, 7.0])
        # Offset = 3.0 - 0.3 = 2.7
        assert "offset=2.700" in result

    def test_duration_clamping_short_slot(self):
        """Transition duration clamped to 30% of shorter adjacent slot."""
        result = _build_xfade_filter(["crossfade"], [0.8, 5.0])
        # max_dur = min(0.8, 5.0) * 0.3 = 0.24  (< DEFAULT 0.3)
        assert "duration=0.240" in result

    def test_five_slots(self):
        """5 slots produce 4 xfade filters."""
        result = _build_xfade_filter(
            ["crossfade", "fade_black", "wipe_left", "wipe_right"],
            [3.0, 3.0, 3.0, 3.0, 3.0],
        )
        parts = result.split(";")
        assert len(parts) == 4
        assert "wipeleft" in parts[2]
        assert "wiperight" in parts[3]

    def test_eight_slots(self):
        """8 slots produce 7 xfade filters."""
        transitions = ["crossfade"] * 7
        durations = [4.0] * 8
        result = _build_xfade_filter(transitions, durations)
        parts = result.split(";")
        assert len(parts) == 7


# ── join_with_transitions integration tests ──────────────────────────────────


class TestJoinWithTransitions:
    def test_input_validation_too_few_slots(self):
        with pytest.raises(ValueError, match="at least 2 slots"):
            join_with_transitions(["a.mp4"], [], [], "out.mp4")

    def test_input_validation_transition_count_mismatch(self):
        with pytest.raises(ValueError, match="Expected 1 transitions"):
            join_with_transitions(
                ["a.mp4", "b.mp4"], ["crossfade", "extra"], [5.0, 5.0], "out.mp4"
            )

    def test_input_validation_duration_count_mismatch(self):
        with pytest.raises(ValueError, match="Expected 2 durations"):
            join_with_transitions(
                ["a.mp4", "b.mp4"], ["crossfade"], [5.0], "out.mp4"
            )

    @patch("app.pipeline.transitions.subprocess.run")
    def test_happy_path_calls_ffmpeg(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        join_with_transitions(
            ["a.mp4", "b.mp4"], ["crossfade"], [5.0, 5.0], "out.mp4"
        )
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ffmpeg"
        assert "-an" in cmd  # video-only
        assert "-filter_complex" in cmd

    @patch("app.pipeline.transitions.subprocess.run")
    def test_ffmpeg_failure_raises_transition_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stderr=b"error details"
        )
        with pytest.raises(TransitionError, match="xfade join failed"):
            join_with_transitions(
                ["a.mp4", "b.mp4"], ["crossfade"], [5.0, 5.0], "out.mp4"
            )

    @patch("app.pipeline.transitions.subprocess.run")
    def test_an_flag_strips_audio(self, mock_run):
        """Video-only output — template audio mixed separately."""
        mock_run.return_value = MagicMock(returncode=0)
        join_with_transitions(
            ["a.mp4", "b.mp4"], ["crossfade"], [5.0, 5.0], "out.mp4"
        )
        cmd = mock_run.call_args[0][0]
        assert "-an" in cmd
