"""transcript_source — the ONE place that answers "what words does this variant speak,
and what hash identifies that state?" (plans/005, decisions 3A + outside-voice tension 1/3).

Precedence:
  1. `variants[i]["transcript"]`   — persisted compact word records ({"word","start_s","end_s"})
  2. word-timed caption cues       — (not yet persisted anywhere word-granular → skipped;
                                      branch documented so PR-later can add it here, and ONLY here)
  3. on-demand bounded Whisper     — the flagship talking-head-with-original-audio case has
                                      neither 1 nor 2; transcribe once, persist to
                                      `variants[i]["transcript"]` so it never re-runs
  4. None                          — caller disables the feature for this variant

The hash covers word texts + timings + variant duration. The matcher AND the
staleness checks both call THIS module — they can never diverge. Staleness is
read-time (GET rail + Apply recompute + compare), not hook-time.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile

import structlog

log = structlog.get_logger()

# Bounded ASR: a match run must never hang on transcription (Celery soft limit 240s).
_WHISPER_WALL_CLOCK_S = 90


def compute_transcript_hash(words: list[dict], duration_s: float | None) -> str:
    """Deterministic hash of the word records + variant duration."""
    payload = [
        [
            str(w.get("word", "")),
            round(float(w.get("start_s", 0.0)), 3),
            round(float(w.get("end_s", 0.0)), 3),
        ]
        for w in words
    ]
    blob = json.dumps([payload, round(float(duration_s or 0.0), 2)], ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def words_from_variant(variant: dict) -> list[dict] | None:
    """Branch 1: persisted compact word records, validated shape.

    Reads the editorial-sequence `transcript` key FIRST (authoritative — it is
    the real spoken-word source when that feature ran), then falls back to the
    matcher's own `overlay_transcript` key. The matcher MUST persist under
    `overlay_transcript`, NOT `transcript` (review C19): generative_jobs.py's
    `sequence_capable = ... or bool(variant.get("transcript"))` treats a present
    `transcript` as a sequence-eligibility signal, so writing matcher Whisper
    words there flipped voiceover/none variants into sequence-capable and
    changed live render behavior."""
    raw = variant.get("transcript")
    if not (isinstance(raw, list) and raw):
        raw = variant.get("overlay_transcript")
    if not isinstance(raw, list) or not raw:
        return None
    words: list[dict] = []
    for w in raw:
        if not isinstance(w, dict):
            return None
        text = str(w.get("word", "")).strip()
        if not text:
            continue
        try:
            words.append(
                {
                    "word": text,
                    "start_s": float(w.get("start_s", 0.0)),
                    "end_s": float(w.get("end_s", 0.0)),
                }
            )
        except (TypeError, ValueError):
            return None
    return words or None


def _variant_duration_s(variant: dict) -> float | None:
    for key in ("duration_s", "output_duration_s", "video_duration_s"):
        v = variant.get(key)
        try:
            if v and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def transcribe_variant_video(variant: dict) -> list[dict] | None:
    """Branch 3: bounded Whisper over the variant's rendered video (sync/task context).

    Downloads `variants[i]["video_path"]`, runs `transcribe_whisper`
    (WHISPER_BACKEND decides local vs openai-api), returns compact word records.
    Best-effort: any failure → None (caller falls through to disabled).
    """
    video_key = (variant.get("video_path") or "").lstrip("/")
    if not video_key:
        return None
    try:
        from app.pipeline.transcribe import transcribe_whisper  # noqa: PLC0415
        from app.storage import download_to_file  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        log.warning("transcript_source.import_failed", error=str(exc)[:200])
        return None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "variant.mp4")
            download_to_file(video_key, local)
            transcript = transcribe_whisper(local)

        def _t(w, *names) -> float:
            for n in names:
                v = getattr(w, n, None)
                if v is not None:
                    return float(v)
            return 0.0

        words = [
            {
                "word": str(getattr(w, "text", "")).strip(),
                # transcribe.Word uses start_s/end_s; tolerate start/end variants.
                "start_s": round(_t(w, "start_s", "start"), 3),
                "end_s": round(_t(w, "end_s", "end"), 3),
            }
            for w in (getattr(transcript, "words", None) or [])
            if str(getattr(w, "text", "")).strip()
        ]
        return words or None
    except Exception as exc:  # noqa: BLE001
        log.warning("transcript_source.whisper_failed", error=str(exc)[:200])
        return None


def transcript_source(
    variant: dict, *, allow_whisper: bool = False
) -> tuple[list[dict], str] | None:
    """Return (words, transcript_hash) for a variant, or None when unavailable.

    `allow_whisper=True` only in task context (the matcher) — routes doing
    read-time staleness checks recompute the hash from the PERSISTED source only.
    When Whisper runs, the CALLER is responsible for persisting the returned
    words back to `variants[i]["overlay_transcript"]` (run-once semantics; NOT
    `transcript` — review C19, see words_from_variant).
    """
    words = words_from_variant(variant)
    # Branch 2 (word-timed caption cues) intentionally lands here when a
    # word-granular persisted cue source exists. Do not add other lookups
    # elsewhere — this module is the single source of truth.
    if words is None and allow_whisper:
        words = transcribe_variant_video(variant)
    if not words:
        return None
    return words, compute_transcript_hash(words, _variant_duration_s(variant))


def persisted_hash_is_stale(variant: dict) -> bool:
    """Read-time staleness: does the stored suggest-hash still match the
    persisted transcript? Only meaningful when a persisted transcript exists —
    Whisper-derived sets (persisted back to the variant by the matcher) are
    covered by the same comparison."""
    stored = variant.get("overlay_suggest_hash")
    if not stored:
        return False
    src = transcript_source(variant, allow_whisper=False)
    if src is None:
        # No persisted words anymore (e.g. a merge cleared them) — anything
        # matched against the old words is stale by definition.
        return True
    return src[1] != stored
