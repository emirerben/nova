"""[Stage 2] Whisper transcription.

WHISPER_BACKEND=openai-api  — default/prod; processes 10-min audio in ~30-60s
WHISPER_BACKEND=local       — dev only; faster-whisper on CPU (too slow for prod SLA)
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field

import structlog

from app.config import settings

log = structlog.get_logger()

# Bumped when the cached transcript shape or the transcription contract changes,
# so a stale cache from an older code version is never reused.
# v2: align_punctuated_text now restores punctuation/case onto the word stream —
# v1 entries hold pre-punctuation words and must not be served.
_TRANSCRIPT_CACHE_VERSION = "v2"

LOW_CONFIDENCE_THRESHOLD = 0.6
LOW_SPEECH_RATIO_THRESHOLD = 0.10  # <10% of words above threshold → ASR fallback

# align_punctuated_text: normalize for comparison by casefolding + stripping
# ALL punctuation (not just leading/trailing) — matches the codebase's existing
# fuzzy-match convention (see `_fold` in caption_correct.py / smart_edit's
# planner.py). This is what lets "thats" (whisper strips the apostrophe from
# word-level tokens) match display token "that's", and lets the concatenation
# of "200" + "000" match display token "200,000" in the merge case below. The
# DISPLAY token (full punctuation intact) is always what ends up on the Word —
# normalization only decides whether two tokens refer to the same thing.
_WORD_CHARS_RE = re.compile(r"[^\W_]+", re.UNICODE)
_MAX_MERGE_TOKENS = 3  # "200,000" vs whisper's "200" + "000" (k <= 3)
_MAX_LOOKAHEAD = 2  # bounded resync window, each side


@dataclass
class Word:
    text: str
    start_s: float
    end_s: float
    confidence: float
    # Segment-level quality signals: whisper-1 verbose_json `segments[]` and
    # faster-whisper segments both report avg_logprob + no_speech_prob, mapped
    # onto words in _apply_segment_signals(). None → the backend returned no
    # segments or the word fell outside all of them. Defaults keep every
    # existing construction site (positional or keyword) source-compatible.
    segment_avg_logprob: float | None = None
    segment_no_speech_prob: float | None = None


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
    verbatim_prompt: str | None = None,
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

    `verbatim_prompt` biases Whisper toward a verbatim transcript that keeps
    filler vocalizations ("Uh, um, ııı, eee…") as tokens — the silence-cut stage
    needs them for lexical filler detection. Whisper-only (the Gemini path
    ignores it); None (default) leaves every Whisper request byte-identical to
    the pre-verbatim behavior (regression-pinned).
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

    return transcribe_whisper(video_path, language=language, verbatim_prompt=verbatim_prompt)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _transcript_to_json(transcript: Transcript) -> bytes:
    return json.dumps(
        {
            "words": [
                {"text": w.text, "start_s": w.start_s, "end_s": w.end_s, "confidence": w.confidence}
                for w in transcript.words
            ],
            "full_text": transcript.full_text,
            "low_confidence": transcript.low_confidence,
            "language": transcript.language,
        }
    ).encode("utf-8")


def _transcript_from_json(raw: bytes) -> Transcript:
    data = json.loads(raw)
    return Transcript(
        words=[
            Word(
                text=str(w["text"]),
                start_s=float(w["start_s"]),
                end_s=float(w["end_s"]),
                confidence=float(w.get("confidence", 1.0)),
            )
            for w in data.get("words", [])
        ],
        full_text=str(data.get("full_text", "")),
        low_confidence=bool(data.get("low_confidence", False)),
        language=str(data.get("language", "")),
    )


def transcribe_whisper_cached(
    clip_path: str,
    *,
    language: str | None = None,
    verbatim_prompt: str | None = None,
) -> Transcript:
    """Content-addressed transcript cache (plan 012 P1-4).

    whisper-1 is non-deterministic — the same audio yields different words across
    renders (a dropped proper noun, a split enumeration). Keying the transcript
    by the clip's content hash means every re-render of the SAME clip reuses the
    identical word list, which kills the "two renders differ" symptom. Fully
    fail-open: any hashing / GCS error falls straight through to a live
    transcription, and a cache write failure never affects the returned result.
    Gated by ``smart_caption_transcript_cache_enabled`` (default on).
    """

    if not getattr(settings, "smart_caption_transcript_cache_enabled", True):
        transcript = transcribe_whisper(
            clip_path, language=language, verbatim_prompt=verbatim_prompt
        )
        setattr(transcript, "cache_status", "disabled")
        return transcript
    try:
        digest = _sha256_file(clip_path)
    except OSError:
        transcript = transcribe_whisper(
            clip_path, language=language, verbatim_prompt=verbatim_prompt
        )
        setattr(transcript, "cache_status", "hash_failed")
        return transcript

    # digest is a sha256 hex string; sanitize the caller-supplied language to a
    # short alpha token so an unexpected value can never shape the GCS object key
    # (defense-in-depth — today's sole caller passes None → "auto").
    lang_key = re.sub(r"[^a-z]", "", (language or "auto").lower())[:8] or "auto"
    # verbatim_prompt biases the whisper output, so it is part of the cache
    # identity: two different prompts on the SAME clip bytes must not collide on
    # one entry (today's sole caller passes None → the stable "noprompt" slot).
    prompt_key = "noprompt"
    if verbatim_prompt:
        prompt_key = hashlib.sha256(verbatim_prompt.encode()).hexdigest()[:12]
    object_path = (
        f"transcript-cache/{_TRANSCRIPT_CACHE_VERSION}/{digest}_{lang_key}_{prompt_key}.json"
    )
    try:
        from app.storage import download_to_file, object_exists  # noqa: PLC0415

        if object_exists(object_path):
            with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as tmp:
                download_to_file(object_path, tmp.name)
                transcript = _transcript_from_json(open(tmp.name, "rb").read())  # noqa: SIM115
            setattr(transcript, "cache_status", "hit")
            log.info("transcript_cache_hit", object_path=object_path, words=len(transcript.words))
            return transcript
    except Exception as exc:  # noqa: BLE001 — cache read is best-effort
        log.warning("transcript_cache_read_failed", error=str(exc))

    transcript = transcribe_whisper(clip_path, language=language, verbatim_prompt=verbatim_prompt)
    setattr(transcript, "cache_status", "miss")
    try:
        from app.storage import upload_bytes_public_read  # noqa: PLC0415

        upload_bytes_public_read(
            _transcript_to_json(transcript), object_path, content_type="application/json"
        )
        log.info("transcript_cache_store", object_path=object_path, words=len(transcript.words))
    except Exception as exc:  # noqa: BLE001 — cache write is best-effort
        log.warning("transcript_cache_write_failed", error=str(exc))
    return transcript


def transcribe_whisper(
    video_path: str,
    *,
    model: str | None = None,
    language: str | None = None,
    verbatim_prompt: str | None = None,
) -> Transcript:
    """Transcribe via Whisper (OpenAI API or local). Always returns a Transcript.

    ``model`` overrides the local Whisper model for this call (e.g. the narrated
    pipeline uses a larger model for caption accuracy). Ignored by the openai-api
    backend, which is pinned to whisper-1. None → `settings.whisper_model`.

    ``language`` is an optional ISO-639-1 hint ("tr", "en"); None → auto-detect.
    Passed to whisper-1's ``language`` arg (prod) and faster-whisper (local dev).

    ``verbatim_prompt`` is an optional bias prompt ("Uh, um, ııı, eee…") that
    makes Whisper keep filler vocalizations as tokens (silence-cut lexical
    detection). Passed as whisper-1's ``prompt`` (prod) and faster-whisper's
    ``initial_prompt`` (local dev) ONLY when not None — the default path stays
    byte-identical (regression-pinned).
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        audio_path = tmp.name

    try:
        _extract_audio(video_path, audio_path)

        if settings.whisper_backend == "openai-api":
            return _transcribe_openai(
                audio_path, language=language, verbatim_prompt=verbatim_prompt
            )
        else:
            return _transcribe_local(
                audio_path, model=model, language=language, verbatim_prompt=verbatim_prompt
            )
    finally:
        if os.path.exists(audio_path):
            os.unlink(audio_path)


def _extract_audio(video_path: str, audio_path: str) -> None:
    """Extract audio track to WAV using FFmpeg. Never shell=True."""
    cmd = [
        "ffmpeg",
        "-i",
        video_path,
        "-vn",  # no video
        "-acodec",
        "pcm_s16le",  # PCM WAV
        "-ar",
        "16000",  # 16kHz (Whisper native)
        "-ac",
        "1",  # mono
        "-y",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if result.returncode != 0:
        raise TranscribeError(f"Audio extraction failed: {result.stderr.decode()[:300]}")


def _apply_segment_signals(words: list[Word], segments: list) -> None:
    """Map segment-level quality signals onto each contained word (both backends).

    Neither backend gives usable per-word confidence in prod (whisper-1 returns
    none at all), but both report per-segment ``avg_logprob`` + ``no_speech_prob``
    — whisper-1 in verbose_json ``segments[]``, faster-whisper on its Segment
    objects. A word belongs to the first segment whose [start, end] contains the
    word's midpoint ((start_s + end_s) / 2); words outside every segment keep
    their None defaults.
    """
    spans: list[tuple[float, float, object, object]] = []
    for seg in segments:
        start = getattr(seg, "start", None)
        end = getattr(seg, "end", None)
        if start is None or end is None:
            continue
        spans.append(
            (
                float(start),
                float(end),
                getattr(seg, "avg_logprob", None),
                getattr(seg, "no_speech_prob", None),
            )
        )
    for word in words:
        midpoint = (word.start_s + word.end_s) / 2
        for start, end, avg_logprob, no_speech_prob in spans:
            if start <= midpoint <= end:
                if avg_logprob is not None:
                    word.segment_avg_logprob = float(avg_logprob)  # type: ignore[arg-type]
                if no_speech_prob is not None:
                    word.segment_no_speech_prob = float(no_speech_prob)  # type: ignore[arg-type]
                break


def _norm_token(token: str) -> str:
    """Casefold + strip ALL punctuation for alignment comparison (display text
    is never derived from this — see module comment above)."""
    return "".join(_WORD_CHARS_RE.findall(token)).casefold()


def _retext(word: Word, display_token: str) -> Word:
    """Copy `word` with its text replaced by the punctuated display token."""
    return Word(
        text=display_token,
        start_s=word.start_s,
        end_s=word.end_s,
        confidence=word.confidence,
        segment_avg_logprob=word.segment_avg_logprob,
        segment_no_speech_prob=word.segment_no_speech_prob,
    )


def _try_merge(words: list[Word], wi: int, display_norm: str) -> tuple[Word, int] | None:
    """Merge case: concat of the next k (2 <= k <= _MAX_MERGE_TOKENS) stripped
    word tokens equals the display token — e.g. "200,000" vs "200" + "000".
    Returns (merged_word, tokens_consumed) or None."""
    n_words = len(words)
    for k in range(2, _MAX_MERGE_TOKENS + 1):
        if wi + k > n_words:
            break
        concat = "".join(_norm_token(words[wi + j].text) for j in range(k))
        if concat and concat == display_norm:
            first, last = words[wi], words[wi + k - 1]
            merged = Word(
                text=words[wi].text,  # overwritten by caller with the display token
                start_s=first.start_s,
                end_s=last.end_s,
                confidence=min(words[wi + j].confidence for j in range(k)),
                segment_avg_logprob=first.segment_avg_logprob,
                segment_no_speech_prob=last.segment_no_speech_prob,
            )
            return merged, k
    return None


def _find_resync(
    words: list[Word], wi: int, display_tokens: list[str], di: int
) -> tuple[int, int] | None:
    """Bounded lookahead (<= _MAX_LOOKAHEAD tokens each side) for a resync point
    after a 1:1 match and a merge both fail at (wi, di). Returns (word_skip,
    display_skip) for the smallest total skip found, preferring to skip WORDS
    (extra spoken tokens absent from the punctuated text) over DISPLAY tokens
    on ties. Returns None if no resync point exists within the window —
    callers must then bail for the whole transcript (fail-open)."""
    n_words, n_display = len(words), len(display_tokens)
    best: tuple[int, int, int] | None = None  # (total, word_skip, display_skip)
    for a in range(0, _MAX_LOOKAHEAD + 1):
        widx = wi + a
        if widx >= n_words:
            continue
        word_norm = _norm_token(words[widx].text)
        for b in range(0, _MAX_LOOKAHEAD + 1):
            if a == 0 and b == 0:
                continue
            didx = di + b
            if didx >= n_display:
                continue
            if word_norm and word_norm == _norm_token(display_tokens[didx]):
                total = a + b
                if best is None or total < best[0] or (total == best[0] and a > best[1]):
                    best = (total, a, b)
    if best is None:
        return None
    return best[1], best[2]


def align_punctuated_text(full_text: str, words: list[Word]) -> list[Word]:
    """Restore punctuation + capitalization from `full_text` onto the timed word
    stream (whisper-1's word-level output carries neither — they live only in
    the full-text transcript).

    Walks both token sequences with a cursor, comparing via casefold + strip of
    punctuation:
      - 1:1 match -> word.text becomes the punctuated display token.
      - merge case (display token == concat of the next k <= 3 stripped word
        tokens, e.g. "200,000" vs "200" + "000") -> one merged Word spanning
        first.start_s -> last.end_s.
      - residual mismatch -> a bounded (<= 2 tokens each side) lookahead tries
        to find a resync point; if none exists, BAIL FOR THE WHOLE TRANSCRIPT
        and return the original `words` unchanged. Fail-open by design — a
        half-aligned transcript (wrong text on the wrong timestamp) is worse
        than an unpunctuated one.

    Pure function: never mutates `words` in place, never raises.
    """
    if not words:
        return words
    display_tokens = full_text.split()
    if not display_tokens:
        log.info("alignment_bailed", reason="empty_full_text", word_count=len(words))
        return words

    aligned: list[Word] = []
    wi = di = 0
    n_words, n_display = len(words), len(display_tokens)

    while wi < n_words:
        if di >= n_display:
            log.info(
                "alignment_bailed",
                reason="display_tokens_exhausted",
                word_index=wi,
                display_index=di,
            )
            return words

        w = words[wi]
        display = display_tokens[di]
        display_norm = _norm_token(display)

        if _norm_token(w.text) == display_norm:
            aligned.append(_retext(w, display))
            wi += 1
            di += 1
            continue

        merge = _try_merge(words, wi, display_norm)
        if merge is not None:
            merged_word, consumed = merge
            aligned.append(_retext(merged_word, display))
            wi += consumed
            di += 1
            continue

        resync = _find_resync(words, wi, display_tokens, di)
        if resync is not None:
            word_skip, _display_skip = resync
            # Extra spoken word(s) not reflected in the punctuated text (e.g. a
            # stray token whisper's word list emitted that full_text dropped):
            # keep them verbatim, no display token to attach.
            for j in range(word_skip):
                aligned.append(words[wi + j])
            wi += resync[0]
            di += resync[1]
            continue

        log.info(
            "alignment_bailed",
            reason="unresolved_mismatch",
            word_index=wi,
            display_index=di,
        )
        return words

    if di != n_display:
        log.info("alignment_bailed", reason="display_tokens_leftover", leftover=n_display - di)
        return words

    return aligned


def _transcribe_openai(
    audio_path: str, *, language: str | None = None, verbatim_prompt: str | None = None
) -> Transcript:
    import openai

    client = openai.OpenAI(api_key=settings.openai_api_key)

    # Only pass `language` when the caller knows it — omitting it lets whisper-1
    # auto-detect. Passing an explicit hint (e.g. "tr") is what makes Turkish
    # reliable, since whisper-1's auto-detect is weak on short/accented clips.
    extra: dict[str, str] = {}
    lang = (language or "").strip().lower()
    if lang:
        extra["language"] = lang
    # Verbatim-bias prompt (silence-cut): keeps filler vocalizations as tokens.
    # Added ONLY when not None so the default request stays byte-identical
    # (regression-pinned in tests/pipeline/test_transcribe_verbatim_segments.py).
    if verbatim_prompt is not None:
        extra["prompt"] = verbatim_prompt

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
            # OpenAI API doesn't return per-word confidence — the segment-level
            # signals mapped below are the only quality info on this path.
            confidence=1.0,
        )
        for w in (response.words or [])
    ]
    # verbose_json also reports per-segment avg_logprob/no_speech_prob — map
    # them onto words (defensive: absent/None segments leave the fields None).
    _apply_segment_signals(words, list(getattr(response, "segments", None) or []))

    full_text = response.text or ""
    # Restore punctuation/capitalization from the full-text transcript onto the
    # timed word stream (whisper-1's word-level output has neither). Fail-open:
    # align_punctuated_text() bails to the original `words` on any residual
    # mismatch, so this is safe to call unconditionally when enabled.
    if settings.caption_punctuation_enabled:
        words = align_punctuated_text(full_text, words)

    transcript = Transcript(
        words=words,
        full_text=full_text,
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
    audio_path: str,
    *,
    model: str | None = None,
    language: str | None = None,
    verbatim_prompt: str | None = None,
) -> Transcript:
    """Local faster-whisper backend — dev use only.

    ``language`` (ISO-639-1, e.g. "tr") is passed through to faster-whisper; None
    lets it auto-detect. NOTE: the English-only ``*.en`` models cannot decode other
    languages regardless of this hint — use a multilingual model (``small`` etc.)
    for Turkish in local dev.

    ``verbatim_prompt`` is passed as ``initial_prompt`` ONLY when not None — the
    default call stays byte-identical (regression-pinned).
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscribeError(
            "faster-whisper not installed. Run: pip install faster-whisper"
        ) from exc

    model_name = (model or "").strip() or settings.whisper_model
    lang = (language or "").strip().lower() or None
    extra: dict[str, str] = {}
    if verbatim_prompt is not None:
        extra["initial_prompt"] = verbatim_prompt
    log.info("whisper_local_start", model=model_name, path=audio_path, language=lang or "auto")
    whisper_model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, info = whisper_model.transcribe(
        audio_path, word_timestamps=True, language=lang, **extra
    )

    words: list[Word] = []
    text_parts: list[str] = []
    seen_segments: list = []  # generator — materialize for _apply_segment_signals
    for segment in segments:
        seen_segments.append(segment)
        text_parts.append(segment.text)
        if segment.words:
            for w in segment.words:
                words.append(
                    Word(
                        text=w.word,
                        start_s=w.start,
                        end_s=w.end,
                        confidence=w.probability,
                    )
                )
    # faster-whisper segments expose the same avg_logprob/no_speech_prob signals.
    _apply_segment_signals(words, seen_segments)

    detected = _normalize_lang(getattr(info, "language", "") or lang or "")
    transcript = Transcript(words=words, full_text=" ".join(text_parts).strip(), language=detected)
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
