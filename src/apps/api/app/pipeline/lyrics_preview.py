"""Lyric-only preview rendering.

Renders the lyric `.ass` produced by the production ASS-generation path over a
1080x1920 black video with the track's audio. If preview output disagrees with
full-render output for any reason other than compositing, the bug is in the
production renderer, not here. This module never re-implements ASS generation.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.pipeline._ffmpeg_filter_paths import escape_ffmpeg_filter_path
from app.pipeline.lyric_injector import inject_lyric_overlays
from app.pipeline.text_overlay import FONTS_DIR, generate_animated_overlay_ass
from app.storage import download_to_file, upload_public_read


class LyricsPreviewInputError(ValueError):
    """Raised when a track cannot produce a lyric preview."""


def build_lyrics_preview_recipe(track: Any, lyrics_config_effective: dict) -> dict:
    """Build a one-slot full-track recipe and inject lyrics via production code."""
    lyrics_cached = getattr(track, "lyrics_cached", None)
    if not lyrics_cached:
        raise LyricsPreviewInputError("Music track has no cached lyrics to preview.")
    duration_s = float(getattr(track, "duration_s", None) or 0.0)
    track_config = getattr(track, "track_config", None) or {}
    if duration_s <= 0:
        duration_s = float(track_config.get("best_end_s") or 0.0)
    if duration_s <= 0:
        raise LyricsPreviewInputError("Music track duration is unknown.")

    recipe = {
        "slots": [
            {
                "position": 1,
                "target_duration_s": round(duration_s, 3),
                "text_overlays": [],
            }
        ]
    }
    return inject_lyric_overlays(
        recipe,
        lyrics_cached,
        best_start_s=0.0,
        best_end_s=duration_s,
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

    with tempfile.TemporaryDirectory(prefix="nova_lyrics_preview_") as tmpdir:
        audio_ext = Path(str(audio_gcs_path)).suffix or ".m4a"
        local_audio = os.path.join(tmpdir, f"audio{audio_ext}")
        download_to_file(audio_gcs_path, local_audio)

        ass_files = build_lyrics_preview_ass_files(track, lyrics_config_effective, tmpdir)
        output_path = os.path.join(tmpdir, "lyrics_preview.mp4")
        cmd = _build_preview_ffmpeg_cmd(local_audio, ass_files, output_path)

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
        }


def _build_preview_ffmpeg_cmd(
    local_audio: str,
    ass_files: list[str],
    output_path: str,
) -> list[str]:
    filter_parts: list[str] = ["[0:v]null[base]"]
    prev = "base"
    fontsdir = escape_ffmpeg_filter_path(FONTS_DIR)
    for idx, ass_path in enumerate(ass_files):
        out = f"ass{idx}"
        escaped_ass = escape_ffmpeg_filter_path(ass_path)
        filter_parts.append(f"[{prev}]subtitles='{escaped_ass}':fontsdir='{fontsdir}'[{out}]")
        prev = out

    return [
        "ffmpeg",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=1080x1920:r=30",
        "-i",
        local_audio,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        f"[{prev}]",
        "-map",
        "1:a",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        "-shortest",
        "-y",
        output_path,
    ]
