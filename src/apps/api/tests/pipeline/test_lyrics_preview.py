from pathlib import Path
from types import SimpleNamespace

import pytest

from app.pipeline.lyric_injector import inject_lyric_overlays
from app.pipeline.lyrics_preview import (
    LyricsPreviewInputError,
    build_lyrics_preview_ass_files,
    render_lyrics_preview,
)
from app.pipeline.text_overlay import generate_animated_overlay_ass


def _track(**overrides):
    track = SimpleNamespace(
        id="track-preview",
        audio_gcs_path="music/track/audio.m4a",
        duration_s=5.0,
        track_config={},
        lyrics_cached={
            "lines": [
                {
                    "text": "hello world",
                    "start_s": 1.0,
                    "end_s": 2.0,
                    "words": [
                        {"text": "hello", "start_s": 1.0, "end_s": 1.5},
                        {"text": "world", "start_s": 1.5, "end_s": 2.0},
                    ],
                }
            ]
        },
    )
    for key, value in overrides.items():
        setattr(track, key, value)
    return track


def test_preview_ass_byte_identical_to_production_path(tmp_path: Path) -> None:
    track = _track()
    cfg = {"enabled": True, "style": "line", "post_dwell_s": 1.0}

    preview_files = build_lyrics_preview_ass_files(track, cfg, str(tmp_path / "preview"))

    production_dir = tmp_path / "production"
    production_dir.mkdir()
    recipe = {"slots": [{"position": 1, "target_duration_s": 5.0, "text_overlays": []}]}
    recipe = inject_lyric_overlays(
        recipe,
        track.lyrics_cached,
        best_start_s=0.0,
        best_end_s=5.0,
        lyrics_config=cfg,
    )
    production_files = generate_animated_overlay_ass(
        recipe["slots"][0]["text_overlays"],
        slot_duration_s=5.0,
        output_dir=str(production_dir),
        slot_index=0,
    )

    assert production_files
    assert [Path(p).read_text() for p in preview_files] == [
        Path(p).read_text() for p in production_files
    ]


def test_preview_rejects_missing_lyrics_cached(tmp_path: Path) -> None:
    with pytest.raises(LyricsPreviewInputError, match="cached lyrics"):
        build_lyrics_preview_ass_files(_track(lyrics_cached=None), {}, str(tmp_path))


def test_render_lyrics_preview_builds_browser_safe_ffmpeg(monkeypatch, tmp_path: Path) -> None:
    def fake_download(_gcs_path: str, local_path: str) -> None:
        Path(local_path).write_bytes(b"audio")

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("app.pipeline.lyrics_preview.download_to_file", fake_download)
    monkeypatch.setattr("app.pipeline.lyrics_preview.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.upload_public_read",
        lambda _local, _obj: "https://example.com/preview.mp4",
    )

    output_url, meta = render_lyrics_preview(_track(), {"enabled": True, "style": "line"})

    assert output_url == "https://example.com/preview.mp4"
    cmd = meta["ffmpeg_cmd"]
    assert "-nostdin" in cmd
    assert "yuv420p" in cmd
    assert "+faststart" in cmd
    assert "-shortest" in cmd
    assert any("subtitles=" in part for part in cmd)
