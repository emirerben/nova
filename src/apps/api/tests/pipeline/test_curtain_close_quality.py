"""Regression guard for the curtain-close encoder banding fix.

`apply_curtain_close_tail` re-encodes every frame of the slot through a
single geq pass. The earlier split-and-concat implementation set the x264
preset to ``ultrafast`` because only a short tail was re-encoded (the
prefix was stream-copied). PR #100 commit 89dc3aa unified the prefix and
tail into one pass but inherited the same ``ultrafast`` setting — which
disables mb-tree, psy-rd, B-frames and trellis quant. On smooth dark
content (e.g. the dark-blue tent canopy under Dimples slot 5's BRAZIL
title) ultrafast produces visible blocky banding artifacts across the
entire slot.

The two tests below guard against silent regressions:

1. **Constants pin.** ``CURTAIN_CLOSE_X264_PRESET``, ``_TUNE`` and ``_CRF``
   are module-level so they can be referenced from tests. A revert to
   ``ultrafast`` therefore requires deliberately editing the constant,
   not just the ffmpeg call.

2. **End-to-end smoke.** Run ``apply_curtain_close_tail`` on a synthetic
   clip and assert the new preset args are actually present in the
   exec'd command line, by stubbing ``subprocess.run`` and inspecting
   the captured args. Catches the case where someone duplicates the
   ffmpeg call elsewhere with hard-coded ``ultrafast``.

A perceptual banding assertion (does the dark blue tent canopy actually
look smooth?) is impractical at unit-test scope — visible banding
correlates with macroblock visibility in real-world content with grain
and motion, which a synthetic gradient does not reproduce reliably across
ffmpeg/libx264 versions. End-to-end visual verification is part of the
PR test plan in plans/100-nolu-pr-zerine-twinkly-whisper.md.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

from app.pipeline import interstitials
from app.pipeline.interstitials import apply_curtain_close_tail


def test_curtain_close_preset_constants() -> None:
    """Pin preset/tune/crf at the module level. Reverting to ``ultrafast``
    reintroduces visible banding on dark gradients and must be a deliberate,
    reviewed decision rather than a silent edit."""
    assert interstitials.CURTAIN_CLOSE_X264_PRESET == "medium"
    assert interstitials.CURTAIN_CLOSE_X264_TUNE == "film"
    assert interstitials.CURTAIN_CLOSE_X264_CRF == "18"


def test_curtain_close_uses_pinned_encoder_args(tmp_path: Path) -> None:
    """The ffmpeg invocation inside ``apply_curtain_close_tail`` must use
    the pinned constants, not a hard-coded preset. Catches drift where the
    constants are tightened but the call site still passes ``ultrafast``."""
    src = tmp_path / "src.mp4"
    out = tmp_path / "out.mp4"

    subprocess.run([
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=1.5",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100:d=1.5",
        "-c:v", "libx264", "-profile:v", "high",
        "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-shortest", str(src),
    ], check=True, capture_output=True, timeout=30)

    real_run = subprocess.run
    captured: list[list[str]] = []

    def capturing_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "ffmpeg" and "-vf" in cmd:
            vf_idx = cmd.index("-vf")
            if vf_idx + 1 < len(cmd) and cmd[vf_idx + 1].startswith("geq="):
                captured.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    with mock.patch.object(subprocess, "run", side_effect=capturing_run):
        apply_curtain_close_tail(str(src), str(out), animate_s=0.5)

    assert captured, (
        "no geq ffmpeg call observed — function never reached the re-encode step"
    )
    cmd = captured[0]

    def arg_after(flag: str) -> str:
        idx = cmd.index(flag)
        return cmd[idx + 1]

    assert arg_after("-preset") == interstitials.CURTAIN_CLOSE_X264_PRESET, (
        f"preset drift: ffmpeg called with -preset {arg_after('-preset')} "
        f"but constant is {interstitials.CURTAIN_CLOSE_X264_PRESET}"
    )
    assert arg_after("-tune") == interstitials.CURTAIN_CLOSE_X264_TUNE
    assert arg_after("-crf") == interstitials.CURTAIN_CLOSE_X264_CRF
    assert out.exists() and out.stat().st_size > 0, (
        "function did not produce an output file"
    )
