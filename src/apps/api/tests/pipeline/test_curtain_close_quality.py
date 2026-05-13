"""Regression guard for the curtain-close encoder banding fix + the
overlay-based animation path.

History:
  PR #102 swapped preset=ultrafast for preset=medium to kill banding on
  dark gradients (the blue tent canopy under Dimples slot-N BRAZIL title).
  PR #103 added a 600s subprocess timeout and made the orchestrator
  re-raise on curtain failure instead of swallowing it.
  Job d018d1c3 (2026-05-11) timed out at exactly 600s in prod on Fly's
  2-shared-CPU worker — the per-pixel geq filter cost was prohibitive
  under preset=medium. The fix-perf PR replaces geq with a pre-rendered
  PNG sequence composited via the overlay filter (orders of magnitude
  faster) and drops the preset to "fast" for additional headroom.

The two tests below guard against silent regressions:

1. **Constants pin.** PRESET / TUNE / CRF are module-level so reverts
   require a deliberate edit, not just an ffmpeg arg change.

2. **End-to-end smoke.** Stub ``subprocess.run`` and verify the captured
   command uses the overlay filter (NOT geq) with the pinned encoder
   constants. Catches a future caller that hard-codes a slower path or
   reintroduces the geq filter.

The perceptual banding assertion (does the dark blue tent canopy actually
look smooth?) is impractical at unit-test scope — visible banding
correlates with macroblock visibility in real-world content with grain
and motion, which a synthetic gradient does not reproduce reliably across
ffmpeg/libx264 versions.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

from app.pipeline import interstitials
from app.pipeline.interstitials import apply_curtain_close_tail


def test_curtain_close_preset_constants() -> None:
    """Pin preset/tune/crf at the module level. Reverting to ``ultrafast``
    reintroduces visible banding on dark gradients; reverting to ``medium``
    reintroduces the Fly timeout regression. Both must be deliberate."""
    assert interstitials.CURTAIN_CLOSE_X264_PRESET == "fast"
    assert interstitials.CURTAIN_CLOSE_X264_TUNE == "film"
    assert interstitials.CURTAIN_CLOSE_X264_CRF == "18"


def test_curtain_close_uses_overlay_path_with_pinned_encoder_args(tmp_path: Path) -> None:
    """The ffmpeg invocation inside ``apply_curtain_close_tail`` must use
    the overlay filter (NOT geq) with the pinned encoder constants. Catches
    drift where the constants are tightened but the call site hard-codes a
    slower preset or reintroduces the per-pixel geq path."""
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
        if (
            isinstance(cmd, list)
            and cmd
            and cmd[0] == "ffmpeg"
            and "-filter_complex" in cmd
        ):
            captured.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    with mock.patch.object(subprocess, "run", side_effect=capturing_run):
        apply_curtain_close_tail(str(src), str(out), animate_s=0.5)

    assert captured, (
        "no ffmpeg overlay call observed — function may have reverted to a "
        "single-pass -vf path (geq?) or never reached the re-encode step"
    )
    cmd = captured[0]

    def arg_after(flag: str) -> str:
        idx = cmd.index(flag)
        return cmd[idx + 1]

    filter_complex = arg_after("-filter_complex")
    assert "overlay=" in filter_complex, (
        f"filter_complex does not use overlay: {filter_complex[:200]}"
    )
    assert "geq=" not in filter_complex, (
        "filter_complex still uses geq — the slow per-pixel path is back. "
        f"Filter: {filter_complex[:200]}"
    )

    assert arg_after("-preset") == interstitials.CURTAIN_CLOSE_X264_PRESET, (
        f"preset drift: ffmpeg called with -preset {arg_after('-preset')} "
        f"but constant is {interstitials.CURTAIN_CLOSE_X264_PRESET}"
    )
    assert arg_after("-tune") == interstitials.CURTAIN_CLOSE_X264_TUNE
    assert arg_after("-crf") == interstitials.CURTAIN_CLOSE_X264_CRF
    assert out.exists() and out.stat().st_size > 0, (
        "function did not produce an output file"
    )
