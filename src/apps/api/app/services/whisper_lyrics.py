"""Whisper API client tuned for lyric word-level timing.

Pipeline-distinct from app.pipeline.transcribe — that module is for spoken
narration in user-uploaded videos. This one is for music: it accepts a
canonical lyric `prompt` (Genius lyrics) to bias Whisper toward the correct
spelling.

Inputs are expected to be audio-only m4a from yt-dlp or the admin upload
route. When that contract is violated (e.g. an admin uploads a YouTube
.mp4 with the video stream intact, or a long uncompressed wav blows past
the 25 MB Whisper ceiling), `_shrink_for_whisper` transparently re-muxes
or re-encodes to a fitting audio-only file before upload.

Returns word-level timing as a list of WhisperWord; alignment with Genius
text happens in app.pipeline.lyrics_alignment.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

import structlog

from app.config import settings
from app.services.audio_preprocess import (
    AudioPreprocessError,
    compress_to_mono_64k,
    has_video_stream,
    strip_video,
)

log = structlog.get_logger()

# OpenAI Whisper API ceiling — uploads must be < 25 MB (real limit, not
# a Nova-side choice). When over, _shrink_for_whisper transcodes in two
# stages (strip-video lossless, then mono-64k re-encode) before upload.
_MAX_FILE_BYTES = 25 * 1024 * 1024

# Genius prompt is appended verbatim. Whisper's `prompt` field is documented
# to accept up to 224 tokens (~1000 chars) — anything beyond is silently
# truncated. We cap at 800 chars to stay safely under that limit while still
# giving Whisper a strong vocabulary hint for the first verse / chorus.
_MAX_PROMPT_CHARS = 800


class WhisperLyricsError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class WhisperWord:
    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class WhisperLyricsResult:
    words: tuple[WhisperWord, ...]
    full_text: str
    language: str  # ISO 639-1 (Whisper auto-detects; "" if unavailable)


def _shrink_for_whisper(audio_path: str, dest_dir: str) -> tuple[str, list[str]]:
    """Return a (possibly the original) path that fits under the Whisper cap.

    Two-stage strategy:
      1. If the file has a video stream, strip it lossless (``-vn -c:a copy``).
         Most "audio.mp4" admin uploads are full music videos — stripping the
         video alone typically takes ~27 MB → ~4 MB.
      2. If still over the cap (genuinely-large pure audio, e.g. uncompressed
         wav), re-encode to mono 64 kbps AAC.

    Returns the path to use and the list of stages that ran (for logging).
    Raises WhisperLyricsError if it still doesn't fit (would need chunking).
    """
    stages: list[str] = []
    current = audio_path
    size = os.path.getsize(current)

    if has_video_stream(current):
        stripped = os.path.join(dest_dir, "stripped.m4a")
        try:
            strip_video(current, stripped)
        except AudioPreprocessError as exc:
            raise WhisperLyricsError(f"failed to strip video stream: {exc}") from exc
        stages.append("strip_video")
        current = stripped
        size = os.path.getsize(current)

    if size > _MAX_FILE_BYTES:
        compressed = os.path.join(dest_dir, "compressed_mono64k.m4a")
        try:
            compress_to_mono_64k(current, compressed)
        except AudioPreprocessError as exc:
            raise WhisperLyricsError(f"failed to compress audio: {exc}") from exc
        stages.append("compress_mono_64k")
        current = compressed
        size = os.path.getsize(current)

    if size > _MAX_FILE_BYTES:
        raise WhisperLyricsError(
            f"audio file is {size} bytes even after mono-64kbps compression; "
            f"track exceeds Whisper's {_MAX_FILE_BYTES}-byte ceiling and needs chunking"
        )

    return current, stages


def transcribe_for_lyrics(
    audio_path: str,
    *,
    prompt: str = "",
    language: str | None = None,
) -> WhisperLyricsResult:
    """Transcribe a music audio file via OpenAI Whisper, with word timings.

    `prompt` is a vocabulary hint (Genius canonical lyrics joined with
    newlines work well). `language` is an optional ISO 639-1 code — pass it
    when you know the song's language to skip auto-detect and improve
    timing on non-English tracks.

    Oversized inputs are transparently shrunk via ``_shrink_for_whisper``
    before upload (defense-in-depth: ingest should already store audio-only
    bytes, but legacy tracks and edge-case uploads may still land here
    with a video stream or as long uncompressed audio).
    """
    if not os.path.exists(audio_path):
        raise WhisperLyricsError(f"audio file does not exist: {audio_path}")
    if not settings.openai_api_key:
        raise WhisperLyricsError("OPENAI_API_KEY not configured")

    import openai  # noqa: PLC0415 — keeps OpenAI SDK out of cold-import path

    client = openai.OpenAI(api_key=settings.openai_api_key)
    trimmed_prompt = (prompt or "")[:_MAX_PROMPT_CHARS].strip()
    original_size = os.path.getsize(audio_path)

    with tempfile.TemporaryDirectory(prefix="nova_whisper_shrink_") as shrink_dir:
        upload_path = audio_path
        if original_size > _MAX_FILE_BYTES or has_video_stream(audio_path):
            upload_path, stages = _shrink_for_whisper(audio_path, shrink_dir)
            log.info(
                "whisper_lyrics_compressed",
                original_bytes=original_size,
                compressed_bytes=os.path.getsize(upload_path),
                stages=stages,
            )

        upload_size = os.path.getsize(upload_path)
        log.info(
            "whisper_lyrics_start",
            path=os.path.basename(upload_path),
            size_bytes=upload_size,
            original_size_bytes=original_size,
            prompt_chars=len(trimmed_prompt),
            language=language or "auto",
        )

        try:
            with open(upload_path, "rb") as f:
                kwargs: dict = {
                    "model": "whisper-1",
                    "file": f,
                    "response_format": "verbose_json",
                    "timestamp_granularities": ["word"],
                }
                if trimmed_prompt:
                    kwargs["prompt"] = trimmed_prompt
                if language:
                    kwargs["language"] = language
                response = client.audio.transcriptions.create(**kwargs)
        except Exception as exc:
            raise WhisperLyricsError(f"whisper API call failed: {exc}") from exc

    words = tuple(
        WhisperWord(
            text=str(w.word).strip(),
            start_s=float(w.start),
            end_s=float(w.end),
        )
        for w in (response.words or [])
        if str(w.word).strip()
    )

    detected_language = ""
    raw_lang = getattr(response, "language", "") or ""
    if isinstance(raw_lang, str):
        # Whisper sometimes returns "english" instead of "en". Normalize the
        # well-known cases; otherwise pass through whatever the API said —
        # downstream is tolerant.
        lang_map = {
            "english": "en",
            "turkish": "tr",
            "spanish": "es",
            "french": "fr",
            "german": "de",
            "italian": "it",
            "portuguese": "pt",
            "russian": "ru",
            "japanese": "ja",
            "korean": "ko",
            "arabic": "ar",
        }
        detected_language = lang_map.get(raw_lang.lower(), raw_lang.lower())[:8]

    log.info(
        "whisper_lyrics_done",
        word_count=len(words),
        language=detected_language,
    )
    return WhisperLyricsResult(
        words=words,
        full_text=str(response.text or ""),
        language=detected_language,
    )
