"""Voice dictation for the chat composers (KRI-742).

The phone shows live words from Apple's on-device recognizer while the creator talks; on
stop it posts the same recording here and swaps in Whisper's text, which copes with mixed
Turkish/English and with the creator's own clip/song names (sent as ``context_terms``).

The audio is a throwaway: it lives in a temp file for the request and is never stored. The
route is stateless (no plan item, no DB) so every chat surface can share it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from typing import Literal

import structlog
from fastapi import APIRouter, File, Form, HTTPException, Request, status
from fastapi import UploadFile as MultipartFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.agents.music_matcher import _sanitize_text
from app.auth import CurrentUser
from app.config import settings
from app.kria.brief_route import fold_text, loose_text
from app.kria.reply_language import (
    SUPPORTED_REPLY_LANGUAGES,
    locale_language,
    reply_language_for,
    say,
)
from app.limiter import limiter
from app.routes.waitlist import get_real_ip

log = structlog.get_logger()

router = APIRouter()

_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "audio/mp4",
        "audio/m4a",
        "audio/x-m4a",
        "audio/aac",
        "audio/mpeg",
        "audio/wav",
        "audio/x-wav",
        "audio/webm",
        "audio/ogg",
        "video/mp4",  # MediaRecorder/Safari may wrap an audio-only take as MP4.
    }
)
_MAX_AUDIO_BYTES = 3 * 1024 * 1024
_MAX_AUDIO_DURATION_S = 120.0
_TRANSCRIPTION_TIMEOUT_S = 25.0
_MAX_TEXT_CHARS = 4000
_MAX_CONTEXT_TERMS = 40
_MAX_TERM_CHARS = 60
_MAX_PROMPT_CHARS = 700
# Words Kria creators actually say; Whisper's prompt biases spelling toward them. The
# product words are said as-is in every language ("music olmasın"); a language's own words
# join them only when the creator's locale names it. KRI-780: the prompt is a bare word
# list, never an English sentence, so it does not pull Turkish speech toward English.
_FIXED_TERMS = ("montage", "lip-sync", "captions", "voiceover", "b-roll", "transition")
_LOCALE_TERMS: dict[str, tuple[str, ...]] = {
    "tr": ("altyazı", "seslendirme", "geçiş", "kurgu", "müzik", "şarkı"),
}
# Whisper hallucinates on silence/noise, more so with a vocabulary prompt. A transcript whose
# every segment is this likely non-speech is dropped (KRI-653: a dead mic invented words).
_NO_SPEECH_PROB = 0.6
# Kria's non-English languages: the only ones Whisper is ever forced to (KRI-780 review).
_FORCED_LANGUAGES = SUPPORTED_REPLY_LANGUAGES - {"en"}
# Turkish Whisper's silence hallucinations: subtitle credits and YouTube sign-offs it learned
# from subtitled video. Loose-folded word tuples, dropped only from the END of a take, so a
# creator who keeps holding the mic after "klipleri sırala" does not send the credit along.
_TRAILING_FILLER: tuple[tuple[str, ...], ...] = (
    ("altyazi", "m", "k"),
    ("izlediginiz", "icin", "tesekkurler"),
    ("izlediginiz", "icin", "tesekkur", "ederim"),
    ("abone", "olmayi", "unutmayin"),
)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class DictationOut(BaseModel):
    text: str
    # Set only when ``text`` is empty, so the app can say why.
    reason: Literal["no_speech"] | None = None


def _dictation_rate_key(request: Request) -> str:
    """Key paid transcription by authenticated proxy identity (same as direction notes)."""

    user_id = request.headers.get("x-user-id", "").strip()
    if user_id:
        return f"user:{user_id[:128]}"
    return f"ip:{get_real_ip(request)}"


def _parse_terms(raw: str) -> list[str]:
    """``context_terms`` arrives as a JSON array in a multipart field; bad input is ignored."""

    try:
        parsed = json.loads(raw) if raw.strip() else []
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    terms: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, str):
            continue
        term = _sanitize_text(item).strip()[:_MAX_TERM_CHARS]
        if term and term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)
        if len(terms) >= _MAX_CONTEXT_TERMS:
            break
    return terms


def _locale_tags(locale_hint: str) -> list[str]:
    """``locale_hint`` is the device's preferred language tags, comma-separated, best first."""
    tags = [part.strip().lower().replace("_", "-") for part in (locale_hint or "").split(",")]
    return [tag for tag in tags if tag][:4]


def whisper_language(locale_hint: str) -> str | None:
    """The language Whisper is told to transcribe, from the app's ``locale_hint``.

    Only the top preference counts, and only when it is a Kria language other than English
    (today ``"tr-TR"`` -> ``"tr"``). Everything else stays ``None`` (auto-detect): forcing a
    language on someone speaking another makes Whisper translate, and device codes Whisper
    does not know (iOS Norwegian "nb") would fail the call outright.
    """
    tags = _locale_tags(locale_hint)
    lang = tags[0].split("-", 1)[0] if tags else ""
    return lang if lang in _FORCED_LANGUAGES else None


def vocabulary_language(locale_hint: str) -> str | None:
    """The language whose words join the prompt: the first Kria language the creator lists.

    A phone in English with Turkish second ("en-TR,tr-TR") still gets the Turkish spelling
    bias, without Whisper being forced to Turkish.
    """
    for tag in _locale_tags(locale_hint):
        lang = tag.split("-", 1)[0]
        if lang in _FORCED_LANGUAGES:
            return lang
    return None


def build_vocabulary_prompt(terms: list[str], language: str | None = None) -> str:
    """Whisper ``prompt``: the creator's own names first, then Kria's fixed vocabulary.

    A plain word list with no sentence around it, so it biases spelling, not language.
    """

    words = [*terms, *_FIXED_TERMS, *_LOCALE_TERMS.get(language or "", ())]
    prompt = ", ".join(words) + "."
    return prompt[:_MAX_PROMPT_CHARS]


def _clean_transcript(text: str) -> str:
    """Strip control characters and collapse whitespace. Unlike ``_sanitize_text`` this keeps
    the full utterance: a spoken edit request is longer than its 400-character field cap."""

    return re.sub(r"\s+", " ", _CONTROL_CHARS.sub(" ", text)).strip()


def _is_silence(transcript, prompt: str) -> bool:
    text = (transcript.full_text or "").strip()
    if not text:
        return True
    probs = [
        w.segment_no_speech_prob for w in transcript.words if w.segment_no_speech_prob is not None
    ]
    if probs and min(probs) >= _NO_SPEECH_PROB:
        return True
    # Whisper echoing the vocabulary prompt back is the classic silence hallucination. Needs 3+
    # words so a creator saying just "captions" or a clip name still comes through.
    # Turkish-aware fold on both sides, so an all-caps "ALTYAZI" echo matches "altyazı".
    normalized = re.sub(r"\W+", " ", fold_text(text)).strip()
    return len(normalized.split()) >= 3 and normalized in re.sub(r"\W+", " ", fold_text(prompt))


def _strip_trailing_filler(text: str) -> str:
    """Drop Turkish credit/sign-off hallucinations from the end of a transcript."""

    words = text.split()
    while words:
        for k in range(1, min(len(words), 6) + 1):
            tail = tuple(re.sub(r"\W+", " ", loose_text(" ".join(words[-k:]))).split())
            if tail in _TRAILING_FILLER:
                words = words[:-k]
                break
        else:
            break
    return " ".join(words)


def _creator_error(language: str | None, *, en: str, tr: str, code: int = 422) -> HTTPException:
    """An error the app may show the creator, in their language (KRI-784)."""
    with reply_language_for(language):
        return HTTPException(status_code=code, detail=say(en=en, tr=tr))


@router.post("/transcribe", response_model=DictationOut)
@limiter.limit("30/minute", key_func=_dictation_rate_key)
async def transcribe_dictation(
    request: Request,
    user: CurrentUser,
    file: MultipartFile = File(...),  # noqa: B008
    context_terms: str = Form(""),
    locale_hint: str = Form(""),
) -> DictationOut:
    if not settings.voice_dictation_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    # The phone's hint, else the device language; Whisper still auto-detects without it.
    language = locale_language(locale_hint) or locale_language(
        request.headers.get("accept-language")
    )

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise _creator_error(
            language,
            en="Dictation must be an audio recording",
            tr="Dikte bir ses kaydı olmalı",
        )

    suffix = os.path.splitext(file.filename or "")[1][:8] or ".m4a"
    local_path = ""
    try:
        total = 0
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            local_path = tmp.name
            while chunk := await file.read(256 * 1024):
                total += len(chunk)
                if total > _MAX_AUDIO_BYTES:
                    raise _creator_error(
                        language,
                        en="Recording is too long — keep it under two minutes",
                        tr="Kayıt çok uzun, iki dakikanın altında tut",
                    )
                tmp.write(chunk)
        if total == 0:
            raise _creator_error(language, en="Recording is empty", tr="Kayıt boş")

        from app.services.audio_download import probe_duration  # noqa: PLC0415

        duration_s = await run_in_threadpool(probe_duration, local_path)
        if duration_s is None:
            raise _creator_error(
                language,
                en="We couldn't read that recording — try again",
                tr="Bu kaydı okuyamadık, tekrar dene",
            )
        if duration_s > _MAX_AUDIO_DURATION_S:
            raise _creator_error(
                language,
                en="Recording is too long — keep it under two minutes",
                tr="Kayıt çok uzun, iki dakikanın altında tut",
            )

        lang = whisper_language(locale_hint)
        prompt = build_vocabulary_prompt(
            _parse_terms(context_terms), vocabulary_language(locale_hint)
        )
        from app.pipeline.transcribe import transcribe_whisper  # noqa: PLC0415

        transcript = await asyncio.wait_for(
            run_in_threadpool(
                transcribe_whisper,
                local_path,
                language=lang,
                verbatim_prompt=prompt,
            ),
            timeout=_TRANSCRIPTION_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise _creator_error(
            language,
            en="Transcription timed out — try again",
            tr="Yazıya dökme zaman aşımına uğradı, tekrar dene",
            code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — the app keeps its live text on any failure
        log.warning("dictation_transcription_failed", exc_info=True)
        raise _creator_error(
            language,
            en="We couldn't understand that recording — try again",
            tr="Bu kaydı anlayamadık, tekrar dene",
        ) from exc
    finally:
        if local_path and os.path.exists(local_path):
            os.unlink(local_path)

    text = _strip_trailing_filler(_clean_transcript(transcript.full_text or ""))
    if not text or _is_silence(transcript, prompt):
        log.info("dictation_no_speech", user_id=str(user.id), duration_s=round(duration_s, 1))
        return DictationOut(text="", reason="no_speech")
    text = text[:_MAX_TEXT_CHARS].strip()
    log.info(
        "dictation_transcribed",
        user_id=str(user.id),
        duration_s=round(duration_s, 1),
        chars=len(text),
        language=transcript.language or "unknown",
    )
    return DictationOut(text=text)
