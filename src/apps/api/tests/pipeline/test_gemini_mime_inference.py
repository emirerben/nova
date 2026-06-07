"""Unit tests for _infer_mime_type in gemini_analyzer.

Why this exists: newer google-genai SDK versions reject uploads
without an explicit mime_type. macOS's stdlib ``mimetypes.guess_type``
returns ``audio/mp4a-latm`` for ``.m4a`` which Gemini's File API does
not accept (we saw this fail in prod on PR #166's first ingest run).
The mapping table in _infer_mime_type hard-maps common audio/video
extensions to IANA-registered names Gemini accepts.
"""

from __future__ import annotations

import pytest

from app.pipeline.agents.gemini_analyzer import _infer_mime_type


@pytest.mark.parametrize(
    "path,expected",
    [
        # Audio — m4a is the critical one; yt-dlp's default audio output.
        ("music/track/audio.m4a", "audio/mp4"),
        ("/tmp/x.m4a", "audio/mp4"),
        ("a.mp4a", "audio/mp4"),
        ("a.aac", "audio/aac"),
        ("song.mp3", "audio/mpeg"),
        ("sample.wav", "audio/wav"),
        ("voice.ogg", "audio/ogg"),
        ("lossless.flac", "audio/flac"),
        ("speech.opus", "audio/opus"),
        # Video
        ("clip.mp4", "video/mp4"),
        ("/users/uploads/v.mp4", "video/mp4"),
        ("a.mov", "video/quicktime"),
        ("b.webm", "video/webm"),
        # Mixed case
        ("MIXED.M4A", "audio/mp4"),
        ("UPPER.MP3", "audio/mpeg"),
        # No-extension fallback (yt-dlp temp files occasionally lose extension)
        ("no_extension_file", "video/mp4"),
        # Unknown extension falls through to stdlib guess or default
        ("strange.xyz", "video/mp4"),
    ],
)
def test_infer_mime_type(path: str, expected: str) -> None:
    assert _infer_mime_type(path) == expected


def test_m4a_does_not_return_mp4a_latm() -> None:
    # Regression: macOS stdlib mimetypes returns "audio/mp4a-latm" for
    # .m4a which Gemini rejects with "Unknown mime type". The explicit
    # mapping in _infer_mime_type must override that guess.
    assert _infer_mime_type("audio.m4a") == "audio/mp4"
    assert "latm" not in _infer_mime_type("audio.m4a")


def test_wav_does_not_return_x_wav() -> None:
    # macOS mimetypes returns "audio/x-wav"; Gemini wants "audio/wav".
    assert _infer_mime_type("a.wav") == "audio/wav"


def test_flac_does_not_return_x_flac() -> None:
    assert _infer_mime_type("a.flac") == "audio/flac"


# ── Image extension coverage (added for photo-support) ───────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("photo.jpg", "image/jpeg"),
        ("photo.jpeg", "image/jpeg"),
        ("screenshot.png", "image/png"),
        ("animated.webp", "image/webp"),
        ("iphone.heic", "image/heic"),
        ("iphone.heif", "image/heif"),
        # Upper-case (case-folded in _infer_mime_type)
        ("PHOTO.JPG", "image/jpeg"),
        ("SHOT.HEIC", "image/heic"),
    ],
)
def test_infer_mime_type_images(path: str, expected: str) -> None:
    """Image extensions must return the correct IANA image/* type, not 'video/mp4'."""
    result = _infer_mime_type(path)
    assert result == expected, f"Expected {expected!r} for {path!r}, got {result!r}"
    assert result.startswith("image/"), "Image files must not fall through to video/mp4"


def test_clip_meta_is_image_defaults_false() -> None:
    """ClipMeta.is_image must default to False so video-only paths are unaffected."""
    from app.pipeline.agents.gemini_analyzer import ClipMeta

    meta = ClipMeta(
        clip_id="clip_0",
        hook_score=7.0,
        hook_text="",
        detected_subject="",
        transcript="",
        best_moments=[],
    )
    assert meta.is_image is False  # must not default to True


def test_clip_meta_is_image_set_true() -> None:
    """is_image=True can be set without triggering a Pydantic parse() trap (plain dataclass)."""
    from app.pipeline.agents.gemini_analyzer import ClipMeta

    meta = ClipMeta(
        clip_id="clip_1",
        hook_score=5.0,
        hook_text="",
        detected_subject="",
        transcript="",
        best_moments=[],
        is_image=True,
    )
    assert meta.is_image is True
