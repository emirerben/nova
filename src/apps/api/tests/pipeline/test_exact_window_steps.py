"""Tests for exact-window steps in _plan_slots + the resolved_plans_out sink.

Clip-editor contract: a step whose slot carries `exact_window: True` renders
the moment's exact start_s/end_s at 1.0x — bypassing clip cursors, beat-snap,
speed-ramp, and the footage-exhaustion machinery — WITHOUT the letterbox
routing that `locked` slots get (exact_window keeps the caller's output_fit).

`resolved_plans_out` is the read-back side of the same contract: callers of
_assemble_clips can pass a list sink and receive the POST-resolution
source-time window per slot, index-aligned with `steps`, so re-rendering those
windows as exact_window steps reproduces the same edit.

No real ffmpeg here — _plan_slots is pure arithmetic, and _assemble_clips
tests mock reframe/join the same way tests/tasks/test_template_orchestrate.py
does (TestBanSlowdownFill pattern).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.probe import VideoProbe
from app.tasks.template_orchestrate import _assemble_clips, _plan_slots

# Dense beat grid (every 0.5s) so beat-snap is ACTIVE for non-exact slots —
# the exact_window tests must hold even with beats present.
BEATS = [round(0.5 * i, 3) for i in range(60)]


def _probe(duration_s: float = 10.0) -> VideoProbe:
    return VideoProbe(
        duration_s=duration_s,
        fps=30.0,
        width=1920,
        height=1080,
        has_audio=True,
        codec="h264",
        aspect_ratio="16:9",
        file_size_bytes=4,
    )


def _step(clip_id: str, start_s: float, end_s: float, target_dur: float = 2.0, **slot_extra):
    step = MagicMock()
    step.clip_id = clip_id
    step.moment = {"start_s": start_s, "end_s": end_s}
    step.slot = {"position": 1, "target_duration_s": target_dur, **slot_extra}
    return step


def _clip_files(tmp_path, durations: dict[str, float]):
    """Write fake clip files and build the id->local + probe maps."""
    id2local: dict[str, str] = {}
    probes: dict[str, VideoProbe] = {}
    for cid, dur in durations.items():
        f = tmp_path / f"{cid}.mp4"
        f.write_bytes(b"fake")
        id2local[cid] = str(f)
        probes[str(f)] = _probe(dur)
    return id2local, probes


class TestExactWindowPlanning:
    def test_exact_window_uses_verbatim_range_and_bypasses_cursor(self, tmp_path):
        """Two exact_window slots on the SAME clip with OVERLAPPING windows
        both keep their exact start/end at 1.0x. The cursor machinery would
        have pushed the second slot to start at the first slot's end (5.0);
        exact_window must not consult or advance the cursor."""
        id2local, probes = _clip_files(tmp_path, {"A": 10.0})
        steps = [
            _step("A", 2.0, 5.0, exact_window=True),
            _step("A", 3.0, 6.0, exact_window=True),  # overlaps slot 0
        ]

        plans, cursors, cumulative = _plan_slots(
            steps,
            id2local,
            probes,
            BEATS,
            None,
            "none",
            str(tmp_path),
        )

        assert (plans[0].start_s, plans[0].end_s) == (2.0, 5.0)
        assert (plans[1].start_s, plans[1].end_s) == (3.0, 6.0)
        assert all(p.speed_factor == 1.0 for p in plans)
        # No cursor was written — exact slots never enter the cursor path.
        assert "A" not in cursors
        # Timeline advances by the exact window lengths, beat-snap untouched.
        assert cumulative == pytest.approx(6.0)

    def test_exact_window_keeps_output_fit_locked_gets_letterbox(self, tmp_path):
        """The letterbox coupling is gated on `locked` ONLY: exact_window
        slots keep the caller's output_fit (9:16 crop), while a locked slot
        still routes through letterbox_black. Pin both in one plan."""
        id2local, probes = _clip_files(tmp_path, {"A": 10.0, "B": 10.0})
        steps = [
            _step("A", 0.0, 2.0, exact_window=True),
            _step("B", 0.0, 2.0, locked=True),
        ]

        plans, _cursors, _cum = _plan_slots(
            steps,
            id2local,
            probes,
            [],
            None,
            "none",
            str(tmp_path),
            output_fit="crop",
        )

        assert plans[0].output_fit == "crop", "exact_window must NOT letterbox"
        assert plans[1].output_fit == "letterbox_black", "locked must still letterbox"
        # Both share the verbatim-window arithmetic.
        assert plans[1].start_s == 0.0 and plans[1].end_s == 2.0
        assert plans[1].speed_factor == 1.0

    def test_window_parity_roundtrip(self, tmp_path):
        """A normal plan re-run as exact_window steps built from the FIRST
        run's resolved windows yields the identical window list — the
        clip-editor round-trip invariant."""
        id2local, probes = _clip_files(tmp_path, {"A": 10.0, "B": 10.0})
        steps = [
            _step("A", 1.0, 3.0),
            _step("A", 1.0, 3.0),  # second use of A → cursor path
            _step("B", 0.0, 2.0),
        ]

        plans1, _c, _cum = _plan_slots(
            steps,
            id2local,
            probes,
            BEATS,
            None,
            "none",
            str(tmp_path),
        )

        exact_steps = [
            _step(s.clip_id, p.start_s, p.end_s, exact_window=True) for s, p in zip(steps, plans1)
        ]
        plans2, _c2, _cum2 = _plan_slots(
            exact_steps,
            id2local,
            probes,
            BEATS,
            None,
            "none",
            str(tmp_path),
        )

        windows1 = [(p.start_s, p.end_s, p.end_s - p.start_s) for p in plans1]
        windows2 = [(p.start_s, p.end_s, p.end_s - p.start_s) for p in plans2]
        assert windows2 == pytest.approx(windows1)

    def test_unflagged_steps_unchanged_byte_identity_guard(self, tmp_path):
        """Regression pin for music/template jobs: steps WITHOUT
        exact_window/locked flags keep the pre-change arithmetic — moment
        start on first use, cursor sharing on reuse, beat-snapped durations."""
        id2local, probes = _clip_files(tmp_path, {"A": 10.0, "B": 10.0})
        steps = [
            _step("A", 1.0, 3.0),
            _step("A", 1.0, 3.0),
            _step("B", 0.0, 2.0),
        ]

        plans, cursors, cumulative = _plan_slots(
            steps,
            id2local,
            probes,
            BEATS,
            None,
            "none",
            str(tmp_path),
        )

        # Slot 0: first use of A → moment start, 2.0s beat-aligned window.
        assert (plans[0].start_s, plans[0].end_s) == (1.0, 3.0)
        # Slot 1: reuse of A → starts at the cursor (3.0), NOT moment start.
        assert (plans[1].start_s, plans[1].end_s) == (3.0, 5.0)
        # Slot 2: first use of B → moment start.
        assert (plans[2].start_s, plans[2].end_s) == (0.0, 2.0)
        assert all(p.speed_factor == 1.0 for p in plans)
        assert all(p.output_fit == "crop" for p in plans)
        assert cursors == {"A": 5.0, "B": 2.0}
        assert cumulative == pytest.approx(6.0)


class TestResolvedPlansOut:
    def _assemble(self, tmp_path, steps, id2local, probes, **kwargs):
        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate._join_or_concat"),
            patch("app.tasks.template_orchestrate._probe_duration", return_value=4.0),
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            _assemble_clips(
                steps=steps,
                clip_id_to_local=id2local,
                clip_probe_map=probes,
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
                **kwargs,
            )
        return mock_reframe

    def test_sink_filled_index_aligned_with_steps(self, tmp_path):
        id2local, probes = _clip_files(tmp_path, {"A": 10.0, "B": 10.0})
        steps = [
            _step("A", 1.0, 3.0),
            _step("B", 2.0, 4.5, exact_window=True),
        ]

        sink: list = []
        self._assemble(tmp_path, steps, id2local, probes, resolved_plans_out=sink)

        assert len(sink) == len(steps)
        assert sink[0] == {
            "clip_id": "A",
            "start_s": 1.0,
            "end_s": 3.0,
            "duration_s": pytest.approx(2.0),
            "speed_factor": 1.0,
        }
        assert sink[1] == {
            "clip_id": "B",
            "start_s": 2.0,
            "end_s": 4.5,
            "duration_s": pytest.approx(2.5),
            "speed_factor": 1.0,
        }

    def test_omitting_sink_changes_nothing(self, tmp_path):
        """Existing callers pass nothing → render calls are byte-identical to
        a run that does pass the sink."""
        id2local, probes = _clip_files(tmp_path, {"A": 10.0})
        make_steps = lambda: [_step("A", 1.0, 3.0)]  # noqa: E731

        with_sink = self._assemble(tmp_path, make_steps(), id2local, probes, resolved_plans_out=[])
        without_sink = self._assemble(tmp_path, make_steps(), id2local, probes)

        assert with_sink.call_args.kwargs == without_sink.call_args.kwargs
