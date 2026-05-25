"""Lyric-only preview rendering.

Renders the lyric `.ass` produced by the production ASS-generation path over a
1080x1920 black video with the track's audio. If preview output disagrees with
full-render output for any reason other than compositing, the bug is in the
production renderer, not here. This module never re-implements ASS generation.

Window policy (2026-05-25): previews are clamped to a strict 20-second maximum.
The full track duration is irrelevant for line-style admin tuning — admins
iterate on opening-hook timing, and a 20s cap keeps the render budget bounded
while still letting 3–6 typical lines fit. Tracks shorter than 20s render their
full length unchanged.

The clamp is enforced at TWO layers:
  1. `build_lyrics_preview_recipe` clamps the recipe's `target_duration_s` so
     `inject_lyric_overlays` only emits ASS events inside the window.
  2. `_build_preview_ffmpeg_cmd` passes `-t {preview_duration_s}` to FFmpeg so
     the final MP4 is hard-capped on the encoder side too. Relying only on
     `-shortest` is unsafe here because lavfi `color=...` is an infinite
     source — without `-t`, the output would run until the (full-track) audio
     ends. Both layers must agree on the same `_resolve_preview_duration_s`.

Encoder policy (2026-05-25): goes through `_encoding_args(preset="fast")` so
the bytes admins watch in the browser stay banding-free on dark gradients. The
preset is locked by `tests/test_encoder_policy.py`; the CRF literal is asserted
inline in `tests/pipeline/test_lyrics_preview.py`.
"""

from __future__ import annotations

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

# Strict preview window. Tracks shorter than this render full length; longer
# tracks are capped at 20s.
PREVIEW_WINDOW_S: float = 20.0

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


def _resolve_preview_duration_s(track: Any) -> float:
    """Compute the preview's clamped duration in seconds.

    Resolution order:
      1. `track.duration_s` if positive
      2. `track.track_config.best_end_s` (supports dict OR object shapes)
      3. Raise `LyricsPreviewInputError` — we won't ship a preview without
         knowing the source length.

    The result is `min(resolved_duration_s, PREVIEW_WINDOW_S)` rounded to 3
    decimals to match the recipe slot's `target_duration_s` precision.
    """
    duration_s = float(getattr(track, "duration_s", None) or 0.0)
    if duration_s <= 0:
        fallback = _read_best_end_s(getattr(track, "track_config", None))
        if fallback is not None and fallback > 0:
            duration_s = fallback
    if duration_s <= 0:
        raise LyricsPreviewInputError("Music track duration is unknown.")
    return round(min(duration_s, PREVIEW_WINDOW_S), 3)


def build_lyrics_preview_recipe(track: Any, lyrics_config_effective: dict) -> dict:
    """Build a one-slot recipe clamped to ``PREVIEW_WINDOW_S`` and inject
    lyrics via production code.
    """
    lyrics_cached = getattr(track, "lyrics_cached", None)
    if not lyrics_cached:
        raise LyricsPreviewInputError("Music track has no cached lyrics to preview.")
    preview_duration_s = _resolve_preview_duration_s(track)

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
        best_start_s=0.0,
        best_end_s=preview_duration_s,
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
) -> tuple[str, dict]:
    """Render a browser-playable MP4 preview and upload it."""
    audio_gcs_path = getattr(track, "audio_gcs_path", None)
    if not audio_gcs_path:
        raise LyricsPreviewInputError("Music track has no audio file.")
    track_id = str(getattr(track, "id", "unknown"))

    # Resolve duration ONCE here so the same value reaches both the ASS
    # generation (via build_lyrics_preview_recipe inside the ass-files helper)
    # and the FFmpeg `-t` cap. Drift between those two would mean the lyrics
    # land outside the video frame or the video runs past the lyric track.
    preview_duration_s = _resolve_preview_duration_s(track)

    with tempfile.TemporaryDirectory(prefix="nova_lyrics_preview_") as tmpdir:
        audio_ext = Path(str(audio_gcs_path)).suffix or ".m4a"
        local_audio = os.path.join(tmpdir, f"audio{audio_ext}")
        download_to_file(audio_gcs_path, local_audio)

        ass_files = build_lyrics_preview_ass_files(track, lyrics_config_effective, tmpdir)
        output_path = os.path.join(tmpdir, "lyrics_preview.mp4")
        cmd = _build_preview_ffmpeg_cmd(local_audio, ass_files, output_path, preview_duration_s)

        result = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"lyrics preview ffmpeg failed (rc={result.returncode}): {stderr}")
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError("lyrics preview ffmpeg produced empty output")

        object_path = f"music-lyrics-previews/{track_id}/lyrics-preview.mp4"
        output_url = upload_public_read(output_path, object_path)
        return output_url, {
            "ass_count": len(ass_files),
            "ffmpeg_cmd": cmd,
            "output_gcs_path": object_path,
            "preview_duration_s": preview_duration_s,
        }


def _build_preview_ffmpeg_cmd(
    local_audio: str,
    ass_files: list[str],
    output_path: str,
    preview_duration_s: float,
) -> list[str]:
    """Assemble the FFmpeg invocation for a lyric-only preview.

    Encoder policy: routes through ``_encoding_args(preset="fast", crf="20")``
    so the final output stays in the banding-safe x264 territory. The call
    site is locked by ``tests/test_encoder_policy.py:FINAL_OUTPUT_REQUIRED``
    (preset class) and ``test_lyrics_preview.py`` (CRF literal).

    Duration cap: emits ``-t {preview_duration_s}`` before the encoding block.
    The lavfi color source is INFINITE, so `-shortest` alone would let the
    output run until the full-track audio ends. The explicit `-t` is the
    layer that actually guarantees the output stays ≤ PREVIEW_WINDOW_S.
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
    # before that.
    return [
        "ffmpeg",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={settings.output_width}x{settings.output_height}:r={settings.output_fps}",
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
