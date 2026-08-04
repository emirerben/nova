"""FFmpeg encoding of a rendered carousel PNG-sequence into a video segment.

Encoder policy: this is an INTERMEDIATE encode (preset="ultrafast") — the
carousel moment is spliced into a montage's `steps` list as a synthetic
exact-window clip (see `_insert_carousel_moment_step` in
`app/tasks/generative_build.py`) and re-encoded downstream by the assembler's
per-clip reframe/burn pass, same reasoning as `image_clip.py`'s intermediate
renders. See `app/pipeline/reframe.py:_encoding_args.__doc__` and
`tests/test_encoder_policy.py` for the audited allowlist this call site is
registered in.

Audio: the PNG sequence has no inherent audio. The assembler generally probes
`has_audio` per source clip and injects silent audio at reframe time when it's
False (see `reframe.reframe_and_export`'s `has_audio=False` branch), so an
audio-less segment would likely still work downstream. But every OTHER
locally-generated (non-user-sourced) video segment in this pipeline —
`interstitials.render_color_hold` is the exact precedent — bakes in a silent
AAC track at the body-slot layout (`app/pipeline/audio_layout.py`) up front,
so the synthetic clip is unconditionally concat-compatible regardless of which
downstream code path picks it up next. This mirrors that pattern exactly:
`SILENT_AUDIO_INPUT_ARGS` + an explicit `-t` (see below) +
`_encoding_args(..., include_audio=True)` (which appends
`BODY_SLOT_AUDIO_OUT_ARGS`).

`-shortest` alone is NOT enough to truncate the endless lavfi audio to the
video's duration here: measured empirically, `-shortest` with an AAC-encoded
lavfi `anullsrc` input let the audio stream run to ~2x the intended length
(e.g. 15 frames/30fps = 0.5s video vs. ~1.0s of AAC — 44 encoded frames
instead of the expected ~22). `render_color_hold` never hits this because it
also passes an explicit `-t {hold_s}`; adding the equivalent explicit `-t`
here (computed from `n_frames / fps`) fixes it the same way, with `-shortest`
kept as a second line of defense.
"""

from __future__ import annotations

import os
import subprocess

from app.pipeline.audio_layout import SILENT_AUDIO_INPUT_ARGS
from app.pipeline.reframe import _encoding_args

ENCODE_TIMEOUT_S = 120


def encode_carousel_segment(
    png_dir: str, pattern: str, n_frames: int, fps: int, output_path: str
) -> None:
    """Mux a `frame_%04d.png`-style sequence in `png_dir` into an mp4 at
    `output_path`, with a silent audio track at the body-slot layout.

    Args:
        png_dir: directory containing the rendered frame PNGs.
        pattern: ffmpeg image2-demuxer pattern (e.g. "frame_%04d.png"),
            relative to `png_dir`.
        n_frames: exact number of frames to read from the sequence (caps a
            longer directory listing; also the frame count the caller has
            already fit to the target duration).
        fps: output frame rate. Also the `-framerate` the image2 demuxer uses
            to read the PNG sequence, so frame N lands at t = N / fps.
        output_path: destination .mp4 path.

    Raises:
        RuntimeError: ffmpeg exited non-zero, or produced no/empty output.
    """
    input_pattern = os.path.join(png_dir, pattern)
    duration_s = n_frames / fps

    cmd = [
        "ffmpeg",
        "-y",
        "-framerate",
        str(fps),
        "-i",
        input_pattern,
        *SILENT_AUDIO_INPUT_ARGS,
        # -frames:v / -t / -shortest / -map are OUTPUT options here (they
        # follow both -i's): -frames:v caps the video stream at exactly
        # n_frames; -t hard-caps BOTH streams' output duration (belt-and-
        # braces — see the module docstring: -shortest alone measurably
        # over-runs the lavfi audio here); -shortest stays as a second line
        # of defense. Placing -frames:v between the two -i's would instead
        # bind it to the SECOND (lavfi) input, which ffmpeg rejects
        # (frame-count options don't apply to lavfi sources).
        "-frames:v",
        str(n_frames),
        "-t",
        f"{duration_s:.3f}",
        "-shortest",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        "format=yuv420p",
    ]
    cmd += _encoding_args(output_path, preset="ultrafast", include_audio=True)

    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=ENCODE_TIMEOUT_S,
        check=False,
    )

    if result.returncode != 0 or not os.path.exists(output_path):
        stderr_excerpt = result.stderr.decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(
            f"encode_carousel_segment: ffmpeg failed (rc={result.returncode}, "
            f"png_dir={png_dir!r}, pattern={pattern!r}, n_frames={n_frames}, "
            f"fps={fps}): {stderr_excerpt}"
        )
