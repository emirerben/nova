"""[Stage 2] Whisper transcription.

WHISPER_BACKEND=openai-api  — default/prod; processes 10-min audio in ~30-60s
WHISPER_BACKEND=local       — dev only; faster-whisper on CPU (too slow for prod SLA)
"""

import os
import subprocess
import tempfile
from dataclasses import dataclass, field

import structlog

from app.config import settings

log = structlog.get_logger()

LOW_CONFIDENCE_THRESHOLD = 0.6
LOW_SPEECH_RATIO_THRESHOLD = 0.10  # <10% of words above threshold → ASR fallback


@dataclass
class Word:
    text: str
    start_s: float
    end_s: float
    confidence: float


@dataclass
class Transcript:
    words: list[Word] = field(default_factory=list)
    full_text: str = ""
    low_confidence: bool = False  # True → engagement-only scoring + no-transcript copy
    # Detected/used language as an ISO-639-1 code ("en"/"tr"), "" when unknown. Set from
    # whisper's auto-detect so the subtitled style captions in the SPOKEN language.
    language: str = ""

    @property
    def high_confidence_ratio(self) -> float:
        if not self.words:
            return 0.0
        high = sum(1 for w in self.words if w.confidence >= LOW_CONFIDENCE_THRESHOLD)
        return high / len(self.words)


class TranscribeError(Exception):
    pass


# whisper reports the detected language as a full lowercase name in verbose_json
# ("turkish") and as an ISO code from faster-whisper ("tr"). Normalize both to ISO.
_LANG_NAME_TO_ISO = {
    "english": "en",
    "turkish": "tr",
    "german": "de",
    "spanish": "es",
    "french": "fr",
    "italian": "it",
    "portuguese": "pt",
    "dutch": "nl",
    "russian": "ru",
    "arabic": "ar",
    "japanese": "ja",
    "korean": "ko",
    "chinese": "zh",
}


def _normalize_lang(value: str) -> str:
    v = (value or "").strip().lower()
    if not v:
        return ""
    if v in _LANG_NAME_TO_ISO:
        return _LANG_NAME_TO_ISO[v]
    return v  # already an ISO code (e.g. "en", "tr") or an uncommon language


def transcribe(
    video_path: str,
    file_ref: object | None = None,
    *,
    job_id: str | None = None,
    language: str | None = None,
) -> Transcript:
    """Transcribe audio from video_path. Returns Transcript (may be low_confidence).

    If file_ref (a Gemini File API reference) is provided AND transcriber_backend
    is 'gemini', attempts Gemini transcription first with Whisper as fallback.
    Otherwise uses Whisper directly.

    `job_id` is threaded into the Gemini agent's `RunContext` for Langfuse
    session clustering. Defaults to None for back-compat.

    `language` is an optional ISO-639-1 hint ("tr", "en"). None → auto-detect.
    whisper-1 auto-detect is unreliable on short/accented clips, so callers that
    know the language (e.g. the subtitled style's language chip) should pass it —
    this is what makes Turkish transcription reliable. Forwarded to the Whisper
    backends; the Gemini path is already multilingual and detects on its own.
    """
    if file_ref is not None and settings.transcriber_backend == "gemini":
        try:
            from app.pipeline.agents.gemini_analyzer import (
                transcribe as gemini_transcribe,  # noqa: PLC0415
            )
            # Attach local path for Whisper fallback inside gemini_analyzer
            file_ref._local_path = video_path  # type: ignore[attr-defined]
            result = gemini_transcribe(file_ref, job_id=job_id)
            if not result.low_confidence:
                log.info("gemini_transcribe_success", path=video_path)
                return result
            log.info("gemini_transcribe_low_confidence_falling_back", path=video_path)
        except Exception as exc:
            log.warning("gemini_transcribe_failed_falling_back", error=str(exc))

    return transcribe_whisper(video_path, language=language)


def transcribe_whisper(
    video_path: str, *, model: str | None = None, language: str | None = None
) -> Transcript:
    """Transcribe via Whisper (OpenAI API or local). Always returns a Transcript.

    ``model`` overrides the local Whisper model for this call (e.g. the narrated
    pipeline uses a larger model for caption accuracy). Ignored by the openai-api
    backend, which is pinned to whisper-1. None → `settings.whisper_model`.

    ``language`` is an optional ISO-639-1 hint ("tr", "en"); None → auto-detect.
    Passed to whisper-1's ``language`` arg (prod) and faster-whisper (local dev).
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        audio_path = tmp.name

    try:
        _extract_audio(video_path, audio_path)

        if settings.whisper_backend == "openai-api":
            return _transcribe_openai(audio_path, language=language)
        else:
            return _transcribe_local(audio_path, model=model, language=language)
    finally:
        if os.path.exists(audio_path):
            os.unlink(audio_path)


def _extract_audio(video_path: str, audio_path: str) -> None:
    """Extract audio track to WAV using FFmpeg. Never shell=True."""
    cmd = [
        "ffmpeg",
        "-i", video_path,
        "-vn",                   # no video
        "-acodec", "pcm_s16le",  # PCM WAV
        "-ar", "16000",          # 16kHz (Whisper native)
        "-ac", "1",              # mono
        "-y",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if result.returncode != 0:
        raise TranscribeError(f"Audio extraction failed: {result.stderr.decode()[:300]}")


def _transcribe_openai(audio_path: str, *, language: str | None = None) -> Transcript:
    import openai

    client = openai.OpenAI(api_key=settings.openai_api_key)

    # Only pass `language` when the caller knows it — omitting it lets whisper-1
    # auto-detect. Passing an explicit hint (e.g. "tr") is what makes Turkish
    # reliable, since whisper-1's auto-detect is weak on short/accented clips.
    extra: dict[str, str] = {}
    lang = (language or "").strip().lower()
    if lang:
        extra["language"] = lang

    log.info("whisper_api_start", path=audio_path, language=lang or "auto")
    with open(audio_path, "rb") as f:
        # verbose_json gives us word-level timestamps
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["word"],
            **extra,
        )

    words = [
        Word(
            text=w.word,
            start_s=w.start,
            end_s=w.end,
            confidence=1.0,  # OpenAI API doesn't return per-word confidence
        )
        for w in (response.words or [])
    ]

    transcript = Transcript(
        words=words,
        full_text=response.text or "",
        # verbose_json reports the detected (or hinted) language — captions follow it.
        language=_normalize_lang(getattr(response, "language", "") or lang),
    )
    _mark_low_confidence(transcript)
    log.info(
        "whisper_api_done",
        word_count=len(words),
        low_confidence=transcript.low_confidence,
        language=transcript.language or "unknown",
    )
    return transcript


def _transcribe_local(
    audio_path: str, *, model: str | None = None, language: str | None = None
) -> Transcript:
    """Local faster-whisper backend — dev use only.

    ``language`` (ISO-639-1, e.g. "tr") is passed through to faster-whisper; None
    lets it auto-detect. NOTE: the English-only ``*.en`` models cannot decode other
    languages regardless of this hint — use a multilingual model (``small`` etc.)
    for Turkish in local dev.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscribeError(
            "faster-whisper not installed. Run: pip install faster-whisper"
        ) from exc

    model_name = (model or "").strip() or settings.whisper_model
    lang = (language or "").strip().lower() or None
    log.info("whisper_local_start", model=model_name, path=audio_path, language=lang or "auto")
    whisper_model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, info = whisper_model.transcribe(audio_path, word_timestamps=True, language=lang)

    words: list[Word] = []
    text_parts: list[str] = []
    for segment in segments:
        text_parts.append(segment.text)
        if segment.words:
            for w in segment.words:
                words.append(Word(
                    text=w.word,
                    start_s=w.start,
                    end_s=w.end,
                    confidence=w.probability,
                ))

    detected = _normalize_lang(getattr(info, "language", "") or lang or "")
    transcript = Transcript(
        words=words, full_text=" ".join(text_parts).strip(), language=detected
    )
    _mark_low_confidence(transcript)
    log.info("whisper_local_done", word_count=len(words), low_confidence=transcript.low_confidence)
    return transcript


def _mark_low_confidence(transcript: Transcript) -> None:
    if transcript.high_confidence_ratio < LOW_SPEECH_RATIO_THRESHOLD:
        transcript.low_confidence = True
        log.warning(
            "transcript_low_confidence",
            high_conf_ratio=transcript.high_confidence_ratio,
        )
