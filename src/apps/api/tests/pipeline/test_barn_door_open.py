"""Tests for the barn-door-open interstitial.

Mirror of the curtain-close test surface. The full FFmpeg render path
needs a real binary + tmp video file; those tests live in CI-only
fixtures. Here we cover what's testable without FFmpeg: the PNG bar
sequence has the expected shape (closed → open inverse of curtain), and
the orchestrator dispatches to `apply_barn_door_open_head` when the
previous slot's interstitial type matches.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")

from app.pipeline.interstitials import (  # noqa: E402
    _BARN_DOOR_MAX_RATIO,
    MIN_BARN_DOOR_ANIMATE_S,
    _generate_barn_door_bars_png_sequence,
)


class TestBarnDoorConstants:
    def test_min_animate_s(self) -> None:
        # Mirror curtain-close's MIN_CURTAIN_ANIMATE_S — same dramatic floor.
        assert MIN_BARN_DOOR_ANIMATE_S == 4.0

    def test_max_ratio(self) -> None:
        # Mirror curtain-close's _CURTAIN_MAX_RATIO — same uncovered-footage budget.
        assert _BARN_DOOR_MAX_RATIO == 0.6


class TestBarnDoorPngSequence:
    """PNG generation is pure Pillow — no FFmpeg, runs in CI."""

    def test_frame_zero_is_fully_closed(self) -> None:
        """Frame 0 has bars at full half-height (doors closed)."""
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            pattern, n_frames = _generate_barn_door_bars_png_sequence(
                output_dir=tmp,
                animate_s=0.5,
                fps=30,
                width=200,
                height=400,
            )
            assert n_frames >= 2
            first = Image.open(Path(tmp) / "bar_0000.png")
            # Top-center pixel should be opaque black at frame 0.
            r, g, b, a = first.getpixel((100, 50))
            assert (r, g, b) == (0, 0, 0)
            assert a == 255
            # Center pixel just inside the closed-door meeting line should
            # also be opaque (height=400 → half_h=200 → at progress=0 the
            # bars cover y=[0,200) and y=[200,400), so y=199 is bar, y=200
            # is bar, y=399 is bar).
            _, _, _, a_center_top = first.getpixel((100, 199))
            assert a_center_top == 255

    def test_frame_last_is_fully_open(self) -> None:
        """Last frame has bars at zero height — entirely transparent."""
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            pattern, n_frames = _generate_barn_door_bars_png_sequence(
                output_dir=tmp,
                animate_s=0.5,
                fps=30,
                width=200,
                height=400,
            )
            last = Image.open(Path(tmp) / f"bar_{n_frames - 1:04d}.png")
            # Every pixel transparent.
            for y in (0, 50, 199, 200, 399):
                r, g, b, a = last.getpixel((100, y))
                assert a == 0, f"frame {n_frames - 1} pixel y={y} is not transparent"

    def test_bars_shrink_monotonically(self) -> None:
        """Each successive frame has bars at strictly smaller height than the prior.

        This is the temporal inverse of curtain-close. The structural
        property guarantees the doors *open* (don't oscillate, don't grow).
        """
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            _, n_frames = _generate_barn_door_bars_png_sequence(
                output_dir=tmp,
                animate_s=0.5,
                fps=30,
                width=200,
                height=400,
            )
            last_h = 201  # > half_h=200
            for i in range(n_frames):
                frame = Image.open(Path(tmp) / f"bar_{i:04d}.png")
                # Walk down from top until we hit a transparent pixel — that's
                # the height of the top bar.
                top_bar_h = 0
                for y in range(200):
                    _, _, _, a = frame.getpixel((100, y))
                    if a == 0:
                        top_bar_h = y
                        break
                else:
                    top_bar_h = 200  # bar fills the whole top half
                assert top_bar_h < last_h, (
                    f"frame {i}: bar height {top_bar_h} not strictly less than "
                    f"previous frame's {last_h}"
                )
                last_h = top_bar_h

    def test_frame_count_scales_with_duration(self) -> None:
        """Doubling animate_s roughly doubles the frame count."""
        with tempfile.TemporaryDirectory() as tmp:
            _, n1 = _generate_barn_door_bars_png_sequence(
                output_dir=tmp, animate_s=1.0, fps=30, width=200, height=400,
            )
        with tempfile.TemporaryDirectory() as tmp:
            _, n2 = _generate_barn_door_bars_png_sequence(
                output_dir=tmp, animate_s=2.0, fps=30, width=200, height=400,
            )
        # Allow ±1 for the inclusive endpoint (`n_frames = int(round(...)) + 1`).
        assert abs(n2 - 2 * n1) <= 2

    def test_minimum_two_frames(self) -> None:
        """Even an absurdly short animate_s renders at least two frames."""
        with tempfile.TemporaryDirectory() as tmp:
            _, n_frames = _generate_barn_door_bars_png_sequence(
                output_dir=tmp, animate_s=0.001, fps=30, width=200, height=400,
            )
            assert n_frames >= 2


class TestBarnDoorIsInverseOfCurtain:
    """Direct comparison with the curtain-close bars at matching progress.

    For a given (animate_s, fps, width, height), the barn-door-open bar
    heights are the temporal reverse of curtain-close: barn-door bar at
    frame i should equal curtain-close bar at frame N-1-i. Closed and
    open are mirror states.
    """

    def test_mirror_relationship(self) -> None:
        from PIL import Image

        from app.pipeline.interstitials import _generate_curtain_bars_png_sequence

        with tempfile.TemporaryDirectory() as tmp_curtain, \
             tempfile.TemporaryDirectory() as tmp_door:
            _, n_curtain = _generate_curtain_bars_png_sequence(
                output_dir=tmp_curtain, animate_s=0.5, fps=30,
                width=200, height=400,
            )
            _, n_door = _generate_barn_door_bars_png_sequence(
                output_dir=tmp_door, animate_s=0.5, fps=30,
                width=200, height=400,
            )
            assert n_curtain == n_door

            def _top_bar_h(path: Path) -> int:
                img = Image.open(path)
                for y in range(200):
                    _, _, _, a = img.getpixel((100, y))
                    if a == 0:
                        return y
                return 200

            for i in range(n_curtain):
                curtain_h = _top_bar_h(Path(tmp_curtain) / f"bar_{i:04d}.png")
                door_h = _top_bar_h(Path(tmp_door) / f"bar_{n_door - 1 - i:04d}.png")
                # Allow off-by-one — both functions use int() truncation.
                assert abs(curtain_h - door_h) <= 1, (
                    f"frame {i}: curtain_h={curtain_h}, mirrored door_h={door_h}"
                )


class TestOrchestratorWiring:
    """The previous slot's interstitial type drives the head-animation dispatch.

    Full integration would render real video; we just confirm the
    `apply_barn_door_open_head` import path resolves and the
    template_orchestrate code references it under the expected name.
    """

    def test_helper_importable(self) -> None:
        from app.pipeline.interstitials import apply_barn_door_open_head

        assert callable(apply_barn_door_open_head)

    def test_orchestrator_references_barn_door_helper(self) -> None:
        """The post-render phase imports and dispatches to apply_barn_door_open_head.

        This is a string-search canary against the orchestrator source so
        a future refactor that drops the dispatch (e.g. accidentally
        deleting the conditional) trips the test before it ships a
        silently-missing animation to prod.
        """
        from app.tasks import template_orchestrate

        src = Path(template_orchestrate.__file__).read_text()
        assert "apply_barn_door_open_head" in src
        assert 'prev_inter_type == "barn-door-open"' in src

    def test_interstitial_dispatch_accepts_barn_door_open(self) -> None:
        """`_insert_interstitial` renders a color hold for barn-door-open.

        The hold is the pause between slot N's tail (closed doors) and
        slot N+1's head (opening reveal). Without this, the recipe-defined
        black hold would be silently dropped — same failure mode as the
        curtain-close insertion bug fixed earlier.
        """
        from app.tasks import template_orchestrate

        src = Path(template_orchestrate.__file__).read_text()
        # The conditional in _insert_interstitial must mention barn-door-open.
        assert '"barn-door-open"' in src
