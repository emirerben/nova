"""End-to-end regression test for the curtain-close seam frame-freeze bug.

`apply_curtain_close_tail` splits the slot video at `anim_start` into a
prefix (everything before the animation) and a tail (the animated portion),
then concats them. An earlier version stream-copied the prefix with
``ffmpeg -t {anim_start} -c copy``, which can only cut at I-frame boundaries.
After concat with the frame-accurate tail (re-encoded from ``-ss
{anim_start}``), one frame was duplicated at the seam — visible in the
rendered output as a ~33ms freeze right at the moment the curtain
animation begins.

This test runs the function end-to-end on a synthetic input that has
continuous motion in every frame (so any duplicated frame is detectable
as a near-zero MAD between consecutive frames) and asserts that no MAD
in the resulting output drops below the freeze threshold.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.pipeline.interstitials import apply_curtain_close_tail


SOURCE_FPS = 30
SOURCE_W = 1080
SOURCE_H = 1920
SOURCE_DURATION_S = 5.0
ANIMATE_S = 1.6
FREEZE_MAD_THRESHOLD = 1.0


def _make_moving_clip(out_path: Path, duration_s: float, fps: int) -> None:
    """Generate a deterministic test clip with strong per-frame motion.

    Uses ``geq`` with the time variable T to compute every pixel as a
    function of (X, Y, T). Each frame is guaranteed distinct from the
    previous one across the entire viewport: a translating sinusoidal
    gradient on RGB. MAD between consecutive frames is ~15-30. Any
    duplicate frame in the output collapses MAD to ~0 at that point.
    """
    geq_r = "128+127*sin(2*PI*(X/64 + T*2))"
    geq_g = "128+127*sin(2*PI*(Y/64 + T*3))"
    geq_b = "128+127*cos(2*PI*(X/96 + Y/96 + T*5))"
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"color=c=black:s={SOURCE_W}x{SOURCE_H}:r={fps}:d={duration_s}",
        "-f", "lavfi",
        "-i", f"sine=frequency=440:sample_rate=44100:duration={duration_s}",
        "-vf", f"geq=r='{geq_r}':g='{geq_g}':b='{geq_b}'",
        "-c:v", "libx264", "-profile:v", "high",
        "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-shortest",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)


def _decode_consecutive_mads(video_path: Path, scratch_dir: Path) -> list[float]:
    """Extract every NATIVE frame of the video and return per-frame MAD
    against the previous frame. Uses ``-vsync passthrough`` so that the
    decoder emits exactly the frames present in the stream, never
    duplicating or dropping to fit a fixed output fps. Without this,
    ``-vf fps=N`` synthesizes virtual frames at the head/tail when the
    container duration drifts a few ms from N×frame_count (concat
    headers do this routinely) — those virtual frames are duplicates of
    real frames and would mask or falsely trigger the freeze detection.
    Length is (frame_count - 1)."""
    subprocess.run([
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-i", str(video_path),
        "-vsync", "passthrough",
        "-vf", "scale=270:480",  # downscale for speed only, no fps munging
        "-y", str(scratch_dir / "f_%04d.png"),
    ], check=True, capture_output=True, timeout=60)
    frames = sorted(scratch_dir.glob("f_*.png"))
    mads: list[float] = []
    prev: np.ndarray | None = None
    for fp in frames:
        arr = np.array(Image.open(fp).convert("RGB"), dtype=np.int16)
        if prev is not None:
            mads.append(float(np.abs(arr - prev).mean()))
        prev = arr
    return mads


def test_apply_curtain_close_tail_no_frozen_frame_at_seam(tmp_path: Path) -> None:
    """The split-and-concat in apply_curtain_close_tail must not produce a
    duplicate frame at the prefix/tail boundary. Prior bug (Step 1
    stream-copy with ``-c copy -t anim_start``) cut at keyframes only,
    leaving a duplicated frame at the seam — visible as a one-frame freeze
    at t=anim_start in the rendered output.

    Synthetic input has continuous motion in every frame, so any MAD ≈ 0 is
    a stutter, not natural still content."""
    in_path = tmp_path / "moving.mp4"
    out_path = tmp_path / "curtain.mp4"

    _make_moving_clip(in_path, duration_s=SOURCE_DURATION_S, fps=SOURCE_FPS)

    apply_curtain_close_tail(
        str(in_path),
        str(out_path),
        animate_s=ANIMATE_S,
    )

    assert out_path.exists() and out_path.stat().st_size > 0, (
        "apply_curtain_close_tail produced no output"
    )

    scratch = tmp_path / "frames"
    scratch.mkdir()
    mads = _decode_consecutive_mads(out_path, scratch)

    assert len(mads) > 0, "no frames decoded from output"
    min_mad = min(mads)
    min_idx = mads.index(min_mad)
    seam_t = min_idx / SOURCE_FPS

    assert min_mad >= FREEZE_MAD_THRESHOLD, (
        f"Frozen frame detected: MAD between consecutive frames drops to "
        f"{min_mad:.3f} at t≈{seam_t:.3f}s (frame {min_idx} → {min_idx+1}). "
        f"Threshold is {FREEZE_MAD_THRESHOLD}. The synthetic input has "
        f"continuous motion in every frame, so any MAD below threshold is "
        f"a duplicated frame at the prefix/tail concat seam. Check that "
        f"Step 1 of apply_curtain_close_tail re-encodes the prefix (not "
        f"stream-copy) so the cut is frame-accurate."
    )


def test_apply_curtain_close_tail_motion_uniform_across_seam(tmp_path: Path) -> None:
    """Even when no single frame is frozen, the seam must not produce an
    abnormally large motion spike (which would happen if a frame went
    *missing* instead of duplicated). The MAD distribution should be
    reasonably tight across the whole output."""
    in_path = tmp_path / "moving.mp4"
    out_path = tmp_path / "curtain.mp4"
    _make_moving_clip(in_path, duration_s=SOURCE_DURATION_S, fps=SOURCE_FPS)
    apply_curtain_close_tail(
        str(in_path), str(out_path), animate_s=ANIMATE_S,
    )
    scratch = tmp_path / "frames"
    scratch.mkdir()
    mads = _decode_consecutive_mads(out_path, scratch)
    assert len(mads) > 5
    median_mad = sorted(mads)[len(mads) // 2]
    max_mad = max(mads)
    # A missing frame would create a 2× jump; allow up to 3× for the curtain
    # geq darkening the bars, which can produce a one-time motion spike.
    assert max_mad <= median_mad * 3.0, (
        f"Spike in inter-frame motion: max MAD {max_mad:.2f} vs median "
        f"{median_mad:.2f}. May indicate a frame went missing at the "
        f"prefix/tail concat seam (the inverse of the duplicate-frame bug)."
    )
