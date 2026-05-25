"""Lyric-only preview rendering.

Renders the lyric `.ass` produced by the production ASS-generation path over a
1080x1920 black video with the track's audio. If preview output disagrees with
full-render output for any reason other than compositing, the bug is in the
production renderer, not here. This module never re-implements ASS generation.

Window policy (2026-05-25): previews show a 20-second window anchored at the
first lyric line. We slice `[first_line_start - LEAD_IN_S, +PREVIEW_WINDOW_S]`
out of the audio so the dashboard works for songs with instrumental intros
(e.g. Billie Jean — first vocal at 30.8s would have rendered 20s of silence
under the prior `[0, 20s]` policy and tripped the "no renderable lyric
overlays" error). LEAD_IN_S preserves ~2s of pre-vocal audio so the fade-in
reads as natural rather than chopped at frame 0. Tracks whose available tail
after the anchor is shorter than `PREVIEW_WINDOW_S` render the shorter window.

The window is enforced at TWO layers:
  1. `build_lyrics_preview_recipe` passes the anchored
     `[best_start_s, best_end_s]` into `inject_lyric_overlays`, which rebases
     line timings to section-relative coordinates so the recipe slot only
     contains lines that fall inside the window.
  2. `_build_preview_ffmpeg_cmd` passes `-ss {preview_start_s}` (input-seek on
     the audio) and `-t {preview_duration_s}` to FFmpeg so the final MP4 is
     hard-capped on the encoder side too. Relying only on `-shortest` is unsafe
     here because lavfi `color=...` is an infinite source — without `-t`, the
     output would run until the audio ends.

Encoder policy (2026-05-25): goes through `_encoding_args(preset="fast")` so
the bytes admins watch in the browser stay banding-free on dark gradients. The
preset is locked by `tests/test_encoder_policy.py`; the CRF literal is asserted
inline in `tests/pipeline/test_lyrics_preview.py`.
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings
from app.pipeline._ffmpeg_filter_paths import escape_ffmpeg_filter_path
from app.pipeline.lyric_injector import inject_lyric_overlays
from app.pipeline.reframe import _encoding_args
from app.pipeline.text_overlay import FONTS_DIR, generate_animated_overlay_ass
from app.storage import download_to_file, upload_public_read

# Maximum preview duration. The window is anchored at the first lyric line and
# extended forward up to this many seconds; tracks whose remaining audio after
# the anchor is shorter than this render the available tail and stop.
PREVIEW_WINDOW_S: float = 20.0

# Seconds of pre-roll the preview shows before the first lyric line so the
# fade-in animation reads as natural rather than chopped at video frame 0.
# `preview_start_s = max(0, first_line.start_s - LEAD_IN_S)` — early-vocal
# tracks (first line < LEAD_IN_S) keep the original `start_s = 0` behavior.
LEAD_IN_S: float = 2.0

# CRF target for the final preview encode. Documented + tested inline so a
# future tweak forces a conscious choice (encoder policy locks preset class,
# not the CRF literal — see test_lyrics_preview.py for the assertion).
PREVIEW_CRF: str = "20"


class LyricsPreviewInputError(ValueError):
    """Raised when a track cannot produce a lyric preview."""


def _read_best_end_s(track_config: Any) -> float | None:
    """Read `best_end_s` from a track_config that may be a dict (JSONB load)
    or an object with attributes (Pydantic, SimpleNamespace, etc.).

    Returns the float value or None if absent/unparseable. Never raises.
    """
    if track_config is None:
        return None
    if isinstance(track_config, dict):
        raw = track_config.get("best_end_s")
    else:
        raw = getattr(track_config, "best_end_s", None)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _resolve_track_duration_s(track: Any) -> float:
    """Resolve the source track's total duration in seconds.

    Resolution order:
      1. `track.duration_s` if positive
      2. `track.track_config.best_end_s` (supports dict OR object shapes)
      3. Raise `LyricsPreviewInputError` — we won't ship a preview without
         knowing the source length.
    """
    duration_s = float(getattr(track, "duration_s", None) or 0.0)
    if duration_s <= 0:
        fallback = _read_best_end_s(getattr(track, "track_config", None))
        if fallback is not None and fallback > 0:
            duration_s = fallback
    if duration_s <= 0:
        raise LyricsPreviewInputError("Music track duration is unknown.")
    return duration_s


def _first_line_start_s(lyrics_cached: Any) -> float | None:
    """Return the earliest `start_s` across cached lyric lines, or None if the
    cache has no lines / no parseable timings.

    `lyrics_cached["lines"]` is normally pre-sorted ascending by
    `app/agents/lyrics.py`, but we min() across the array anyway so a future
    backfill / manual edit that breaks ordering still picks the right anchor.
    """
    if not isinstance(lyrics_cached, dict):
        return None
    lines = lyrics_cached.get("lines") or []
    starts: list[float] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        raw = line.get("start_s")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        # Reject NaN/Inf: `float("nan")` and `float("inf")` succeed and would
        # propagate to FFmpeg `-ss nan` (FFmpeg error) and to the JSON status
        # response (where the frontend's `formatMSS` would render "NaN:NaN").
        # All NaN comparisons return False, so the `<=` clamp in
        # `_resolve_preview_window` would silently pass non-finite values
        # through if we didn't guard here.
        if not math.isfinite(value):
            continue
        starts.append(value)
    if not starts:
        return None
    return min(starts)


def _resolve_preview_window(track: Any) -> tuple[float, float]:
    """Compute the preview's anchored `(start_s, duration_s)` window.

    Anchors at `max(0, first_line.start_s - LEAD_IN_S)` and extends forward by
    `PREVIEW_WINDOW_S`, capped at the track's remaining audio. Both values are
    rounded to 3 decimals to match the recipe slot's `target_duration_s`
    precision and the FFmpeg `-ss` / `-t` literals.

    Falls back to `start_s = 0.0` when `lyrics_cached["lines"]` is missing or
    has no parseable timings — that path is then caught downstream as "no
    renderable lyric overlays" with the same clear error.
    """
    track_duration_s = _resolve_track_duration_s(track)
    first_start = _first_line_start_s(getattr(track, "lyrics_cached", None))
    if first_start is None or first_start <= LEAD_IN_S:
        start_s = 0.0
    else:
        start_s = first_start - LEAD_IN_S
    available = track_duration_s - start_s
    if available <= 0:
        raise LyricsPreviewInputError(
            f"Lyric anchor at {start_s:.3f}s (first line at "
            f"{first_start if first_start is not None else 'n/a'}s) "
            f"exceeds track duration {track_duration_s:.3f}s."
        )
    duration_s = min(PREVIEW_WINDOW_S, available)
    return round(start_s, 3), round(duration_s, 3)


def build_lyrics_preview_recipe(track: Any, lyrics_config_effective: dict) -> dict:
    """Build a one-slot recipe anchored at the first lyric line and inject
    lyrics via production code.
    """
    lyrics_cached = getattr(track, "lyrics_cached", None)
    if not lyrics_cached:
        raise LyricsPreviewInputError("Music track has no cached lyrics to preview.")
    preview_start_s, preview_duration_s = _resolve_preview_window(track)

    recipe = {
        "slots": [
            {
                "position": 1,
                "target_duration_s": preview_duration_s,
                "text_overlays": [],
            }
        ]
    }
    return inject_lyric_overlays(
        recipe,
        lyrics_cached,
        best_start_s=preview_start_s,
        best_end_s=preview_start_s + preview_duration_s,
        lyrics_config={**lyrics_config_effective, "enabled": True, "style": "line"},
    )


def build_lyrics_preview_ass_files(
    track: Any,
    lyrics_config_effective: dict,
    output_dir: str,
) -> list[str]:
    """Generate the same ASS files production would burn for lyric overlays."""
    os.makedirs(output_dir, exist_ok=True)
    recipe = build_lyrics_preview_recipe(track, lyrics_config_effective)
    slots = recipe.get("slots") or []
    if not slots:
        raise LyricsPreviewInputError("Lyric preview produced no renderable slots.")
    slot = slots[0]
    ass_files = generate_animated_overlay_ass(
        slot.get("text_overlays") or [],
        slot_duration_s=float(slot.get("target_duration_s") or 0.0),
        output_dir=output_dir,
        slot_index=0,
    )
    if not ass_files:
        raise LyricsPreviewInputError("Lyric preview produced no renderable lyric overlays.")
    return ass_files


def render_lyrics_preview(
    track: Any,
    lyrics_config_effective: dict,
    job_id: str,
) -> tuple[str, dict]:
    """Render a browser-playable MP4 preview and upload it.

    ``job_id`` is required and namespaces the GCS object so concurrent
    previews (or sequential previews after a config edit) do not overwrite
    one another. Without this, every preview for the same track wrote to
    `music-lyrics-previews/{track_id}/lyrics-preview.mp4` — admin-visible
    silent UX corruption (job A's status row pointed at a URL serving job
    B's render bytes).
    """
    audio_gcs_path = getattr(track, "audio_gcs_path", None)
    if not audio_gcs_path:
        raise LyricsPreviewInputError("Music track has no audio file.")
    track_id = str(getattr(track, "id", "unknown"))

    # Resolve the window ONCE so the same (start_s, duration_s) reaches both
    # the ASS generation (via build_lyrics_preview_recipe → inject_lyric_overlays)
    # and the FFmpeg `-ss` / `-t` flags. Drift between those two would mean the
    # lyrics land outside the video frame or the audio plays a different
    # segment than the lyrics describe.
    preview_start_s, preview_duration_s = _resolve_preview_window(track)

    with tempfile.TemporaryDirectory(prefix="nova_lyrics_preview_") as tmpdir:
        audio_ext = Path(str(audio_gcs_path)).suffix or ".m4a"
        local_audio = os.path.join(tmpdir, f"audio{audio_ext}")
        download_to_file(audio_gcs_path, local_audio)

        ass_files = build_lyrics_preview_ass_files(track, lyrics_config_effective, tmpdir)
        output_path = os.path.join(tmpdir, "lyrics_preview.mp4")
        cmd = _build_preview_ffmpeg_cmd(
            local_audio, ass_files, output_path, preview_start_s, preview_duration_s
        )

        result = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"lyrics preview ffmpeg failed (rc={result.returncode}): {stderr}")
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError("lyrics preview ffmpeg produced empty output")

        # Per-job namespacing: track_id alone collides across iterations.
        # The 24h delete rule in `infra/gcs-lifecycle.json` matches this prefix
        # so blobs don't accumulate forever.
        object_path = f"music-lyrics-previews/{track_id}/{job_id}/lyrics-preview.mp4"
        output_url = upload_public_read(output_path, object_path)
        return output_url, {
            "ass_count": len(ass_files),
            "ffmpeg_cmd": cmd,
            "output_gcs_path": object_path,
            "preview_start_s": preview_start_s,
            "preview_duration_s": preview_duration_s,
        }


def _build_preview_ffmpeg_cmd(
    local_audio: str,
    ass_files: list[str],
    output_path: str,
    preview_start_s: float,
    preview_duration_s: float,
) -> list[str]:
    """Assemble the FFmpeg invocation for a lyric-only preview.

    Encoder policy: routes through ``_encoding_args(preset="fast", crf="20")``
    so the final output stays in the banding-safe x264 territory. The call
    site is locked by ``tests/test_encoder_policy.py:FINAL_OUTPUT_REQUIRED``
    (preset class) and ``test_lyrics_preview.py`` (CRF literal).

    Window: emits ``-ss {preview_start_s}`` immediately before the audio
    ``-i`` (input-seek, fast and keyframe-safe; affects only the audio input,
    not the infinite lavfi color source) and ``-t {preview_duration_s}``
    before the encoding block. The lavfi color source is INFINITE, so
    ``-shortest`` alone would let the output run until the (track-tail) audio
    ends — the explicit ``-t`` is the layer that actually guarantees the
    output stays ≤ ``PREVIEW_WINDOW_S``.
    """
    filter_parts: list[str] = ["[0:v]null[base]"]
    prev = "base"
    fontsdir = escape_ffmpeg_filter_path(FONTS_DIR)
    for idx, ass_path in enumerate(ass_files):
        out = f"ass{idx}"
        escaped_ass = escape_ffmpeg_filter_path(ass_path)
        filter_parts.append(f"[{prev}]subtitles='{escaped_ass}':fontsdir='{fontsdir}'[{out}]")
        prev = out

    # ``-t`` and ``-shortest`` are both per-output flags and must appear
    # before the output encoding block (which ends in ``-y output_path``).
    # _encoding_args owns everything from ``-c:v`` onward, so they go just
    # before that. The ``-ss`` is an *input* option for the audio input — it
    # must come right before its ``-i``, never after.
    return [
        "ffmpeg",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={settings.output_width}x{settings.output_height}:r={settings.output_fps}",
        "-ss",
        f"{preview_start_s:.3f}",
        "-i",
        local_audio,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        f"[{prev}]",
        "-map",
        "1:a",
        "-t",
        f"{preview_duration_s:.3f}",
        "-shortest",
        *_encoding_args(output_path, preset="fast", crf=PREVIEW_CRF),
    ]
