"""Whisper API client tuned for lyric word-level timing.

Pipeline-distinct from app.pipeline.transcribe — that module is for spoken
narration in user-uploaded videos. This one is for music: it accepts a
canonical lyric `prompt` (Genius lyrics) to bias Whisper toward the correct
spelling, and it does NOT extract audio with FFmpeg (the music file is
already audio-only m4a from yt-dlp).

Returns word-level timing as a list of WhisperWord; alignment with Genius
text happens in app.pipeline.lyrics_alignment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import structlog

from app.config import settings

log = structlog.get_logger()

# OpenAI Whisper API ceiling — uploads must be < 25 MB. Our m4a tracks are
# usually 3-4 MB, but a 10-min lossless wav would blow past this. Caller
# truncates / re-encodes upstream if needed; we just fail loudly here.
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
    """
    if not os.path.exists(audio_path):
        raise WhisperLyricsError(f"audio file does not exist: {audio_path}")
    size = os.path.getsize(audio_path)
    if size > _MAX_FILE_BYTES:
        raise WhisperLyricsError(
            f"audio file is {size} bytes; Whisper API ceiling is {_MAX_FILE_BYTES}"
        )
    if not settings.openai_api_key:
        raise WhisperLyricsError("OPENAI_API_KEY not configured")

    import openai  # noqa: PLC0415 — keeps OpenAI SDK out of cold-import path

    client = openai.OpenAI(api_key=settings.openai_api_key)
    trimmed_prompt = (prompt or "")[:_MAX_PROMPT_CHARS].strip()

    log.info(
        "whisper_lyrics_start",
        path=os.path.basename(audio_path),
        size_bytes=size,
        prompt_chars=len(trimmed_prompt),
        language=language or "auto",
    )

    try:
        with open(audio_path, "rb") as f:
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
