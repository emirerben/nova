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

    @property
    def high_confidence_ratio(self) -> float:
        if not self.words:
            return 0.0
        high = sum(1 for w in self.words if w.confidence >= LOW_CONFIDENCE_THRESHOLD)
        return high / len(self.words)


class TranscribeError(Exception):
    pass


def transcribe(video_path: str, file_ref: object | None = None) -> Transcript:
    """Transcribe audio from video_path. Returns Transcript (may be low_confidence).

    If file_ref (a Gemini File API reference) is provided AND transcriber_backend
    is 'gemini', attempts Gemini transcription first with Whisper as fallback.
    Otherwise uses Whisper directly.
    """
    if file_ref is not None and settings.transcriber_backend == "gemini":
        try:
            from app.pipeline.agents.gemini_analyzer import transcribe as gemini_transcribe  # noqa: PLC0415
            # Attach local path for Whisper fallback inside gemini_analyzer
            file_ref._local_path = video_path  # type: ignore[attr-defined]
            result = gemini_transcribe(file_ref)
            if not result.low_confidence:
                log.info("gemini_transcribe_success", path=video_path)
                return result
            log.info("gemini_transcribe_low_confidence_falling_back", path=video_path)
        except Exception as exc:
            log.warning("gemini_transcribe_failed_falling_back", error=str(exc))

    return transcribe_whisper(video_path)


def transcribe_whisper(video_path: str) -> Transcript:
    """Transcribe via Whisper (OpenAI API or local). Always returns a Transcript."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        audio_path = tmp.name

    try:
        _extract_audio(video_path, audio_path)

        if settings.whisper_backend == "openai-api":
            return _transcribe_openai(audio_path)
        else:
            return _transcribe_local(audio_path)
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


def _transcribe_openai(audio_path: str) -> Transcript:
    import openai

    client = openai.OpenAI(api_key=settings.openai_api_key)

    log.info("whisper_api_start", path=audio_path)
    with open(audio_path, "rb") as f:
        # verbose_json gives us word-level timestamps
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["word"],
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
    )
    _mark_low_confidence(transcript)
    log.info("whisper_api_done", word_count=len(words), low_confidence=transcript.low_confidence)
    return transcript


def _transcribe_local(audio_path: str) -> Transcript:
    """Local faster-whisper backend — dev use only."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscribeError(
            "faster-whisper not installed. Run: pip install faster-whisper"
        ) from exc

    log.info("whisper_local_start", model=settings.whisper_model, path=audio_path)
    model = WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(audio_path, word_timestamps=True)

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

    transcript = Transcript(words=words, full_text=" ".join(text_parts).strip())
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
