"""Unit tests for scripts/analyze_scene_glitches.py.

Covers two core invariants:
  1. stutter pattern detector flags a 24fps source rendered at 30fps with
     period ≈ 0.167s and implied source fps ≈ 23.976.
  2. region-masked MAD isolates the background from a text-band overlay
     (bg MAD ≈ 0 while text-band MAD is dominated by overlay motion).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import numpy as np
import pytest

_HERE = Path(__file__).parent
_SCRIPT_PATH = _HERE.parent.parent / "scripts" / "analyze_scene_glitches.py"
_spec = importlib.util.spec_from_file_location(
    "analyze_scene_glitches", str(_SCRIPT_PATH),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["analyze_scene_glitches"] = _mod
_spec.loader.exec_module(_mod)


def _make_obs(mads_by_field: list[tuple[float, float, float]],
              fps: float = 30.0) -> list:
    """Build a list of FrameMAD from triples (full, bg, text)."""
    return [
        _mod.FrameMAD(
            frame_idx=i + 1,
            t_s=(i + 1) / fps,
            full=full,
            bg_below_text=bg,
            text_band=text,
        )
        for i, (full, bg, text) in enumerate(mads_by_field)
    ]


class TestStutterDetector:
    def test_flags_24fps_source_rendered_at_30fps(self):
        """Simulate a 24fps source up-sampled to 30fps: every 5th
        consecutive-frame pair has MAD ≈ 0 (the duplicated frame), the
        rest have normal motion."""
        # 30 frames worth of pairs (1s of output). Pattern: 4 normal + 1
        # frozen, repeating. So pairs at indices 4, 9, 14, 19, 24 are the
        # frozen ones (every 5th).
        triples = []
        for i in range(30):
            if (i + 1) % 5 == 0:
                triples.append((0.3, 0.3, 0.3))  # frozen
            else:
                triples.append((6.0, 6.0, 6.0))  # motion
        obs = _make_obs(triples)
        clusters = _mod.detect_stutter_clusters(obs, field_name="bg_below_text")
        assert len(clusters) >= 1, (
            f"no stutter cluster detected; expected one for periodic "
            f"frozen-every-5-frames pattern. Clusters: {clusters}"
        )
        c = clusters[0]
        assert abs(c.period_s - 5.0 / 30) < 0.02, (
            f"period_s={c.period_s} expected ~0.167s"
        )
        assert abs(c.implied_source_fps - 24.0) < 0.5, (
            f"implied_source_fps={c.implied_source_fps} expected ~24"
        )
        assert c.count >= 5, f"cluster count {c.count} expected ≥5"

    def test_no_cluster_on_random_motion(self):
        """Random non-periodic motion should not register as a stutter
        cluster — false positives here would break the report."""
        rng = np.random.default_rng(seed=42)
        triples = [
            (float(m), float(m), float(m))
            for m in rng.uniform(3.0, 10.0, size=40)
        ]
        obs = _make_obs(triples)
        clusters = _mod.detect_stutter_clusters(obs, field_name="bg_below_text")
        assert clusters == [], (
            f"unexpected cluster on random motion: {clusters}"
        )

    def test_below_minimum_count_does_not_emit_cluster(self):
        """Two frozen frames is not enough to call a 'cluster' — need at
        least 3 evenly-spaced low frames before we report a stutter."""
        triples = [
            (6.0, 6.0, 6.0), (6.0, 6.0, 6.0), (6.0, 6.0, 6.0),
            (0.2, 0.2, 0.2),    # freeze
            (6.0, 6.0, 6.0), (6.0, 6.0, 6.0), (6.0, 6.0, 6.0),
            (0.2, 0.2, 0.2),    # freeze
            (6.0, 6.0, 6.0),
        ]
        obs = _make_obs(triples)
        clusters = _mod.detect_stutter_clusters(obs, field_name="bg_below_text")
        assert clusters == [], (
            f"two-freeze pattern incorrectly reported as cluster: {clusters}"
        )


class TestRegionMad:
    def test_region_y_bounds_split_correctly(self):
        """Title band must sit roughly around y_frac=0.45 of frame height,
        and bg_below_text must start AFTER the title band ends so descenders
        from the title text never bleed into the bg signal."""
        bounds = _mod._region_y_bounds(scaled_h=960)
        full = bounds["full"]
        text = bounds["text_band"]
        bg = bounds["bg_below_text"]
        assert full == (0, 960)
        assert text[0] < text[1]
        assert bg[0] >= text[1], (
            f"bg region starts at {bg[0]} but text region ends at "
            f"{text[1]} — overlap would let title-text motion leak into "
            f"the bg-MAD signal."
        )
        # Title band should straddle the expected center
        expected_center = int(960 * 0.45)
        assert text[0] < expected_center < text[1], (
            f"title band [{text[0]},{text[1]}] doesn't contain expected "
            f"center {expected_center}"
        )

    def test_region_mad_isolates_bg_from_text_band(self):
        """Synthetic frames: bg below the title is identical between
        consecutive frames (MAD=0); the title band has a strong color
        flip (MAD high). Region-MAD must report exactly that."""
        h, w = 960, 540
        bounds = _mod._region_y_bounds(h)
        bg_lo = bounds["bg_below_text"][0]

        prev = np.zeros((h, w, 3), dtype=np.uint8)
        curr = np.zeros((h, w, 3), dtype=np.uint8)
        # Background: solid grey in BOTH frames (no motion)
        prev[bg_lo:, :, :] = 128
        curr[bg_lo:, :, :] = 128
        # Title band: solid red in prev, solid blue in curr (max motion)
        text_lo, text_hi = bounds["text_band"]
        prev[text_lo:text_hi, :, 0] = 255  # red
        curr[text_lo:text_hi, :, 2] = 255  # blue

        mads = _mod._region_mad_pair(prev, curr, bounds)
        assert mads["bg_below_text"] < 0.1, (
            f"bg_below_text MAD={mads['bg_below_text']} should be ~0 "
            f"(bg is identical between frames)"
        )
        assert mads["text_band"] > 50, (
            f"text_band MAD={mads['text_band']} should be high "
            f"(red→blue is max channel motion)"
        )
        # Full-frame MAD should be somewhere in between
        assert mads["bg_below_text"] < mads["full"] < mads["text_band"], (
            f"full-frame MAD {mads['full']} should sit between bg "
            f"({mads['bg_below_text']}) and text ({mads['text_band']})"
        )
