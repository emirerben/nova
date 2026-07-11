"""Seed a ready+published beat-sync music track for local lyric-selector demos.

Synthesizes beats + timed lyrics + MusicLabels (no yt-dlp / Gemini analysis),
uploads a real audio track (extracted from a local sample video) to GCS, and
inserts a `ready` + `published` MusicTrack. Lyrics are ENABLED with NO pinned
style_set_id, so orchestrate_music_job runs the LyricStyleSelectorAgent against
the labels (pop / high-energy → expected pick: lyric_karaoke_bold).

Usage: python scripts/seed_demo_music_track.py <sample_video_path>
Prints the track_id.
"""

# Dev-only seed tool: long sample lyric strings exceed the line cap.
# ruff: noqa: E501

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime

from app.agents._schemas.music_labels import CURRENT_LABEL_VERSION
from app.database import sync_session
from app.models import MusicTrack
from app.storage import upload_public_read

BEST_END_S = 24.0
BEAT_GAP_S = 0.5  # 120 BPM


def _lyrics() -> dict:
    """A handful of timed lines with per-word timings (karaoke sweep needs words)."""
    lines_words = [
        ("city lights keep calling out my name", 2.0),
        ("we were dancing till the morning came", 7.0),
        ("hold on tight and never let me go", 12.0),
        ("every heartbeat moving nice and slow", 17.0),
    ]
    lines = []
    for text, start in lines_words:
        words = text.split()
        per = 3.6 / len(words)
        wlist = [
            {"text": w, "start_s": round(start + i * per, 3), "end_s": round(start + (i + 1) * per, 3)}
            for i, w in enumerate(words)
        ]
        lines.append({"text": text, "start_s": start, "end_s": round(start + 3.6, 3), "words": wlist})
    return {"lines": lines, "source": "demo-seed"}


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: seed_demo_music_track.py <sample_video_path>")
        sys.exit(2)
    src = sys.argv[1]
    track_id = str(uuid.uuid4())

    # Extract ~25s of audio (loop the short sample to fill the window).
    with tempfile.TemporaryDirectory() as td:
        audio = os.path.join(td, "audio.m4a")
        subprocess.run(
            ["ffmpeg", "-y", "-stream_loop", "-1", "-i", src, "-t", "26",
             "-vn", "-c:a", "aac", "-b:a", "128k", audio],
            check=True, capture_output=True,
        )
        object_path = f"music/{track_id}/audio.m4a"
        upload_public_read(audio, object_path, content_type="audio/mp4")

    beats = [round(i * BEAT_GAP_S, 3) for i in range(int(BEST_END_S / BEAT_GAP_S) + 1)]
    now = datetime.now(UTC)

    track = MusicTrack(
        id=track_id,
        title="Neon Skies (demo)",
        artist="Kria Demo",
        source_url="https://example.com/demo",
        audio_gcs_path=object_path,
        duration_s=26.0,
        beat_timestamps_s=beats,
        analysis_status="ready",
        published_at=now,
        track_config={
            "best_start_s": 0.0,
            "best_end_s": BEST_END_S,
            "slot_every_n_beats": 8,
            "required_clips_min": 1,
            "required_clips_max": 20,
            "lyrics_config": {"enabled": True},  # no style / no style_set_id → selector picks
        },
        lyrics_cached=_lyrics(),
        lyrics_status="ready",
        ai_labels={
            "labels": {
                "label_version": CURRENT_LABEL_VERSION,
                "genre": "pop",
                "vibe_tags": ["energetic", "uplifting", "night-drive"],
                "energy": "high",
                "pacing": "fast",
                "mood": "euphoric late-night energy",
                "ideal_content_profile": "fast-cut travel and nightlife clips",
                "copy_tone": "high_energy",
                "color_grade": "vibrant neon",
                "transition_style": "beat_pulse",
            }
        },
        label_version=CURRENT_LABEL_VERSION,
    )
    with sync_session() as db:
        db.add(track)
        db.commit()
    print(f"TRACK_ID={track_id}")


if __name__ == "__main__":
    main()
