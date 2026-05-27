"""Tests for app.services.whisper_lyrics + app.services.audio_preprocess.

Covers the two-stage shrink pipeline that handles oversized inputs to the
Whisper API (25 MB ceiling). Two layers of tests:

  * audio_preprocess helpers — mock subprocess.run, verify ffmpeg/ffprobe
    command shape + error wrapping.
  * whisper_lyrics shrink orchestration — mock the helpers, verify which
    stages run for each input shape.

See plan: https-nova-video-vercel-app-admin-music-glittery-sketch.md
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# audio_preprocess.has_video_stream
# ─────────────────────────────────────────────────────────────────────────────


def test_has_video_stream_true_when_video_codec_present(tmp_path: Path) -> None:
    from app.services.audio_preprocess import has_video_stream

    f = tmp_path / "media.mp4"
    f.write_bytes(b"x")
    fake = MagicMock(
        returncode=0,
        stdout='{"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}',
    )
    with patch("app.services.audio_preprocess.subprocess.run", return_value=fake):
        assert has_video_stream(str(f)) is True


def test_has_video_stream_false_when_audio_only(tmp_path: Path) -> None:
    from app.services.audio_preprocess import has_video_stream

    f = tmp_path / "audio.m4a"
    f.write_bytes(b"x")
    fake = MagicMock(
        returncode=0,
        stdout='{"streams":[{"codec_type":"audio"}]}',
    )
    with patch("app.services.audio_preprocess.subprocess.run", return_value=fake):
        assert has_video_stream(str(f)) is False


def test_has_video_stream_false_when_ffprobe_fails(tmp_path: Path) -> None:
    from app.services.audio_preprocess import has_video_stream

    f = tmp_path / "audio.m4a"
    f.write_bytes(b"x")
    fake = MagicMock(returncode=1, stdout="", stderr="error")
    with patch("app.services.audio_preprocess.subprocess.run", return_value=fake):
        assert has_video_stream(str(f)) is False


def test_has_video_stream_false_on_exception() -> None:
    from app.services.audio_preprocess import has_video_stream

    with patch(
        "app.services.audio_preprocess.subprocess.run",
        side_effect=OSError("ffprobe not found"),
    ):
        assert has_video_stream("/nonexistent") is False


# ─────────────────────────────────────────────────────────────────────────────
# audio_preprocess.strip_video / compress_to_mono_64k
# ─────────────────────────────────────────────────────────────────────────────


def test_strip_video_invokes_ffmpeg_with_lossless_audio_copy(tmp_path: Path) -> None:
    from app.services.audio_preprocess import strip_video

    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    dest = tmp_path / "audio.m4a"
    fake = MagicMock(returncode=0, stderr="")
    with patch("app.services.audio_preprocess.subprocess.run", return_value=fake) as run:
        strip_video(str(src), str(dest))
    args = run.call_args.args[0]
    assert args[0] == "ffmpeg"
    # Critical flags: -vn drops video, -c:a copy keeps audio bytes verbatim (no re-encode).
    assert "-vn" in args
    assert "copy" in args  # c:a copy
    # Output container must be mp4 so .m4a containers are valid.
    assert "-f" in args and args[args.index("-f") + 1] == "mp4"


def test_strip_video_wraps_ffmpeg_failure(tmp_path: Path) -> None:
    from app.services.audio_preprocess import AudioPreprocessError, strip_video

    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    dest = tmp_path / "audio.m4a"
    fake = MagicMock(returncode=1, stderr="Invalid data found when processing input")
    with patch("app.services.audio_preprocess.subprocess.run", return_value=fake):
        with pytest.raises(AudioPreprocessError, match="strip-video failed"):
            strip_video(str(src), str(dest))


def test_compress_to_mono_64k_invokes_aac_64k_mono(tmp_path: Path) -> None:
    from app.services.audio_preprocess import compress_to_mono_64k

    src = tmp_path / "src.wav"
    src.write_bytes(b"x")
    dest = tmp_path / "out.m4a"
    fake = MagicMock(returncode=0, stderr="")
    with patch("app.services.audio_preprocess.subprocess.run", return_value=fake) as run:
        compress_to_mono_64k(str(src), str(dest))
    args = run.call_args.args[0]
    assert args[0] == "ffmpeg"
    # Mono channel, 64 kbps, AAC codec.
    assert "-ac" in args and args[args.index("-ac") + 1] == "1"
    assert "-b:a" in args and args[args.index("-b:a") + 1] == "64k"
    assert "-c:a" in args and args[args.index("-c:a") + 1] == "aac"


def test_compress_to_mono_64k_wraps_ffmpeg_failure(tmp_path: Path) -> None:
    from app.services.audio_preprocess import AudioPreprocessError, compress_to_mono_64k

    src = tmp_path / "src.wav"
    src.write_bytes(b"x")
    dest = tmp_path / "out.m4a"
    fake = MagicMock(returncode=2, stderr="bad input")
    with patch("app.services.audio_preprocess.subprocess.run", return_value=fake):
        with pytest.raises(AudioPreprocessError, match="mono-64k compress failed"):
            compress_to_mono_64k(str(src), str(dest))


# ─────────────────────────────────────────────────────────────────────────────
# whisper_lyrics._shrink_for_whisper — orchestration
# ─────────────────────────────────────────────────────────────────────────────


def _write_file_of_size(path: Path, size: int) -> None:
    """Allocate a file of *size* bytes. Sparse-write so it's near-instant."""
    with open(path, "wb") as f:
        f.seek(max(size - 1, 0))
        f.write(b"\x00")


def test_shrink_strips_video_when_present(tmp_path: Path) -> None:
    from app.services import whisper_lyrics

    src = tmp_path / "src.mp4"
    _write_file_of_size(src, 30 * 1024 * 1024)  # 30 MB pre-strip

    def fake_strip_video(_src: str, dest: str) -> None:
        _write_file_of_size(Path(dest), 4 * 1024 * 1024)  # 4 MB after strip

    with (
        patch("app.services.whisper_lyrics.has_video_stream", return_value=True),
        patch("app.services.whisper_lyrics.strip_video", side_effect=fake_strip_video) as strip,
        patch("app.services.whisper_lyrics.compress_to_mono_64k") as compress,
    ):
        out, stages = whisper_lyrics._shrink_for_whisper(str(src), str(tmp_path))

    assert stages == ["strip_video"]
    assert out.endswith("stripped.m4a")
    assert os.path.getsize(out) == 4 * 1024 * 1024
    strip.assert_called_once()
    compress.assert_not_called()


def test_shrink_compresses_when_pure_audio_over_cap(tmp_path: Path) -> None:
    from app.services import whisper_lyrics

    src = tmp_path / "src.wav"
    _write_file_of_size(src, 40 * 1024 * 1024)  # 40 MB pure audio

    def fake_compress(_src: str, dest: str) -> None:
        _write_file_of_size(Path(dest), 5 * 1024 * 1024)  # 5 MB after compress

    with (
        patch("app.services.whisper_lyrics.has_video_stream", return_value=False),
        patch("app.services.whisper_lyrics.strip_video") as strip,
        patch(
            "app.services.whisper_lyrics.compress_to_mono_64k", side_effect=fake_compress
        ) as compress,
    ):
        out, stages = whisper_lyrics._shrink_for_whisper(str(src), str(tmp_path))

    assert stages == ["compress_mono_64k"]
    assert out.endswith("compressed_mono64k.m4a")
    strip.assert_not_called()
    compress.assert_called_once()


def test_shrink_runs_both_stages_when_strip_alone_insufficient(tmp_path: Path) -> None:
    from app.services import whisper_lyrics

    src = tmp_path / "long.mp4"
    _write_file_of_size(src, 80 * 1024 * 1024)

    def fake_strip(_src: str, dest: str) -> None:
        _write_file_of_size(Path(dest), 28 * 1024 * 1024)  # still > 25 MiB

    def fake_compress(_src: str, dest: str) -> None:
        _write_file_of_size(Path(dest), 6 * 1024 * 1024)

    with (
        patch("app.services.whisper_lyrics.has_video_stream", return_value=True),
        patch("app.services.whisper_lyrics.strip_video", side_effect=fake_strip),
        patch("app.services.whisper_lyrics.compress_to_mono_64k", side_effect=fake_compress),
    ):
        out, stages = whisper_lyrics._shrink_for_whisper(str(src), str(tmp_path))

    assert stages == ["strip_video", "compress_mono_64k"]
    assert out.endswith("compressed_mono64k.m4a")


def test_shrink_raises_when_still_over_after_both_stages(tmp_path: Path) -> None:
    from app.services import whisper_lyrics
    from app.services.whisper_lyrics import WhisperLyricsError

    src = tmp_path / "huge.wav"
    _write_file_of_size(src, 200 * 1024 * 1024)

    def fake_compress(_src: str, dest: str) -> None:
        # Even mono-64k still over the cap — simulate a pathologically long upload.
        _write_file_of_size(Path(dest), 30 * 1024 * 1024)

    with (
        patch("app.services.whisper_lyrics.has_video_stream", return_value=False),
        patch("app.services.whisper_lyrics.compress_to_mono_64k", side_effect=fake_compress),
    ):
        with pytest.raises(WhisperLyricsError, match="even after mono-64kbps"):
            whisper_lyrics._shrink_for_whisper(str(src), str(tmp_path))


def test_shrink_wraps_ffmpeg_strip_failure(tmp_path: Path) -> None:
    from app.services import whisper_lyrics
    from app.services.audio_preprocess import AudioPreprocessError
    from app.services.whisper_lyrics import WhisperLyricsError

    src = tmp_path / "src.mp4"
    _write_file_of_size(src, 30 * 1024 * 1024)

    with (
        patch("app.services.whisper_lyrics.has_video_stream", return_value=True),
        patch(
            "app.services.whisper_lyrics.strip_video",
            side_effect=AudioPreprocessError("ffmpeg said no"),
        ),
    ):
        with pytest.raises(WhisperLyricsError, match="failed to strip video stream"):
            whisper_lyrics._shrink_for_whisper(str(src), str(tmp_path))


# ─────────────────────────────────────────────────────────────────────────────
# whisper_lyrics.transcribe_for_lyrics — integration with shrink
# ─────────────────────────────────────────────────────────────────────────────


def _make_fake_openai_response() -> MagicMock:
    response = MagicMock()
    response.words = [MagicMock(word="hola", start=0.0, end=0.4)]
    response.language = "es"
    response.text = "hola"
    return response


def test_transcribe_uses_original_path_when_under_cap(tmp_path: Path) -> None:
    """Small audio-only file → no preprocessing, original path uploaded."""
    from app.services import whisper_lyrics

    audio = tmp_path / "audio.m4a"
    _write_file_of_size(audio, 4 * 1024 * 1024)  # 4 MB

    fake_client = MagicMock()
    fake_client.audio.transcriptions.create.return_value = _make_fake_openai_response()

    captured_paths: list[str] = []

    def capture_open(path, *a, **kw):  # type: ignore[no-untyped-def]
        captured_paths.append(str(path))
        return _real_open(path, *a, **kw)

    _real_open = open  # noqa: A001 — keep handle so capture_open can call through

    with (
        patch.object(whisper_lyrics, "has_video_stream", return_value=False),
        patch("openai.OpenAI", return_value=fake_client),
        patch("builtins.open", side_effect=capture_open),
    ):
        result = whisper_lyrics.transcribe_for_lyrics(str(audio))

    assert result.full_text == "hola"
    # The file passed to Whisper must be the original — no shrinking happened.
    opened_for_upload = [p for p in captured_paths if p.endswith(".m4a")]
    assert opened_for_upload and opened_for_upload[-1] == str(audio)


def test_transcribe_shrinks_then_uploads_when_over_cap(tmp_path: Path) -> None:
    """Oversized video-bearing file → strip_video runs, smaller path uploaded."""
    from app.services import whisper_lyrics

    audio = tmp_path / "audio.mp4"
    _write_file_of_size(audio, 30 * 1024 * 1024)  # 30 MB with embedded video

    def fake_strip(_src: str, dest: str) -> None:
        _write_file_of_size(Path(dest), 4 * 1024 * 1024)

    fake_client = MagicMock()
    fake_client.audio.transcriptions.create.return_value = _make_fake_openai_response()

    captured_paths: list[str] = []
    _real_open = open  # noqa: A001

    def capture_open(path, *a, **kw):  # type: ignore[no-untyped-def]
        captured_paths.append(str(path))
        return _real_open(path, *a, **kw)

    with (
        patch.object(whisper_lyrics, "has_video_stream", return_value=True),
        patch.object(whisper_lyrics, "strip_video", side_effect=fake_strip),
        patch("openai.OpenAI", return_value=fake_client),
        patch("builtins.open", side_effect=capture_open),
    ):
        result = whisper_lyrics.transcribe_for_lyrics(str(audio))

    assert result.full_text == "hola"
    # The file passed to Whisper must be the stripped one, NOT the original.
    uploaded_paths = [p for p in captured_paths if p.endswith(".m4a")]
    assert uploaded_paths, "expected at least one .m4a path to be opened"
    assert all(p != str(audio) for p in uploaded_paths), (
        "should NOT upload the original mp4 once we've stripped it"
    )
    assert any("stripped.m4a" in p for p in uploaded_paths)


def test_transcribe_raises_when_audio_missing() -> None:
    from app.services.whisper_lyrics import WhisperLyricsError, transcribe_for_lyrics

    with pytest.raises(WhisperLyricsError, match="does not exist"):
        transcribe_for_lyrics("/nonexistent/audio.m4a")
