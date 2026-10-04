"""Creator-uploaded song ingest for phone montages (KRI-374).

``analyze_user_song_task(item_id, generation)``
  -> download the pinned song generation
  -> beats (``pipeline/music_beats.detect_music_beats``) + word timings (cached
     whisper; no lyrics is fine) -> ``SongAnalysis`` on ``PlanItem.song_analysis``

``align_user_song_takes_task(item_id)``
  -> locate every raw take (the phone's analysis proxy) inside the song and
     persist a ``SongAlignment`` on ``PlanItem.song_alignment``.

Contract (mirrors ``kria_clip_understanding``): background, idempotent, NEVER
fatal. A task never raises; a failure is recorded on the row (analysis
``status="failed"`` with an error, a take ``unmatched``) so the planner can still
answer. Every write re-reads the row under ``FOR UPDATE`` and is fenced on the
song's pinned ``(path, generation)``, so a song replaced mid-flight can never
receive a stale result. Enqueue helpers are called only AFTER the attach commit.

Idempotency: analysis is skipped when the stored row already matches
``(generation, SONG_ANALYSIS_VERSION)`` and is ready. Alignment recomputes only
the takes whose ``(song generation, proxy generation, SONG_ALIGNMENT_VERSION)``
differ from the stored row, and merges its results into the CURRENT row, so two
overlapping runs cannot clobber each other.

The aligner itself (``app.pipeline.song_alignment.align_takes``) and the PCM
decoder (``app.pipeline.audio_pcm.decode_pcm_f32``) are imported lazily inside
``_run_aligner`` / ``_decode_pcm`` -- the only two seams to those modules.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from typing import Any

import structlog
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.config import settings
from app.database import sync_session
from app.models import PlanItem
from app.pipeline.music_beats import detect_music_beats_strict
from app.schemas.user_song import (
    SONG_ALIGNMENT_VERSION,
    SONG_ANALYSIS_VERSION,
    SongAlignment,
    SongAnalysis,
    SongLine,
    SongWord,
    TakeAlignment,
)
from app.worker import celery_app

log = structlog.get_logger()

# A lyric line ends on a long pause or after this many words.
LINE_GAP_S = 0.8
LINE_MAX_WORDS = 8


# Faults of the run, not of the media: a hung/missing ffmpeg, a dropped
# connection, a timeout. They must never be written down as a verdict about the
# song or a take ("unmatched", "ready with no beats"); the work is simply
# retried by the next run.
_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    subprocess.TimeoutExpired,
    TimeoutError,
    OSError,
)


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, _TRANSIENT_ERRORS)


# ── pure helpers ──────────────────────────────────────────────────────────────


def group_words_into_lines(words: list[SongWord]) -> list[SongLine]:
    """Group word timings into lyric lines (gap > 0.8 s, or ~8 words, ends a line)."""
    lines: list[SongLine] = []
    current: list[SongWord] = []

    def flush() -> None:
        if current:
            lines.append(
                SongLine(
                    start_s=current[0].start_s,
                    end_s=max(word.end_s for word in current),
                    text=" ".join(word.text for word in current),
                )
            )
            current.clear()

    for word in words:
        if current and (
            word.start_s - current[-1].end_s > LINE_GAP_S or len(current) >= LINE_MAX_WORDS
        ):
            flush()
        current.append(word)
    flush()
    return lines


def _song_words(raw: list[dict[str, Any]]) -> list[SongWord]:
    words: list[SongWord] = []
    for entry in raw:
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        start = max(0.0, float(entry.get("start_s") or 0.0))
        end = max(start, float(entry.get("end_s") or start))
        words.append(SongWord(text=text, start_s=start, end_s=end))
    words.sort(key=lambda word: (word.start_s, word.end_s))
    return words


def _transcribe_words(local_path: str) -> list[dict[str, Any]]:
    """Cached whisper word timings for one local media file. May raise."""
    from app.services.speech_segments import transcribe_clip  # noqa: PLC0415

    words, _language = transcribe_clip(local_path)
    return words


def _decode_pcm(local_path: str) -> Any:
    """f32le 16 kHz mono samples (Lane A's decoder, imported lazily)."""
    from app.pipeline.audio_pcm import decode_pcm_f32  # noqa: PLC0415

    return decode_pcm_f32(local_path)


def _run_aligner(
    song_pcm: Any, song: SongAnalysis, takes: list[dict[str, Any]]
) -> dict[str, TakeAlignment]:
    """The single seam to Lane A's pure aligner.

    ``takes`` items are ``{"media_id", "pcm", "words"}`` (``words`` = the take's
    transcript as ``[{"text", "start_s", "end_s"}]``, possibly empty). The
    aligner returns ``{media_id: TakeAlignment | dict}``.
    """
    from app.pipeline.song_alignment import align_takes  # noqa: PLC0415

    # The task applies ``song_alignment_proxy_offset_s`` itself (``_shift``), so
    # the aligner runs with the offset zeroed to avoid adding it twice.
    cfg = settings.model_copy(update={"song_alignment_proxy_offset_s": 0.0})
    result = align_takes(
        song_pcm,
        {take["media_id"]: (take["pcm"], take.get("words") or [], None) for take in takes},
        list(song.words),
        song.generation,
        settings_obj=cfg,
    )
    return {str(media_id): alignment for media_id, alignment in result.takes.items()}


def _shift(alignment: TakeAlignment, offset_s: float) -> TakeAlignment:
    """Add the measured proxy-vs-original timing offset to every delta."""
    if not offset_s:
        return alignment
    return alignment.model_copy(
        update={
            "delta_s": None if alignment.delta_s is None else alignment.delta_s + offset_s,
            "alternates": [
                alt.model_copy(update={"delta_s": alt.delta_s + offset_s})
                for alt in alignment.alternates
            ],
        }
    )


def _unmatched(media_id: str, proxy_generation: int | None) -> TakeAlignment:
    return TakeAlignment(media_id=media_id, proxy_generation=proxy_generation, status="unmatched")


def _generation_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── row access (short transactions, fenced on the pinned generation) ──────────


def _read_song(item_id: uuid.UUID) -> dict[str, Any] | None:
    with sync_session() as db:
        item = db.get(PlanItem, item_id)
        if item is None or not item.song_gcs_path or item.song_generation is None:
            return None
        return {
            "path": str(item.song_gcs_path),
            "generation": int(item.song_generation),
            "duration_s": float(item.song_duration_s or 0.0),
            "analysis": dict(item.song_analysis) if isinstance(item.song_analysis, dict) else None,
            "alignment": dict(item.song_alignment)
            if isinstance(item.song_alignment, dict)
            else None,
            "clips": [
                dict(row)
                for row in (item.clip_assignments or [])
                if isinstance(row, dict)
                and row.get("gcs_path")
                and row.get("media_id")
                and str(row.get("kind") or "video") == "video"
            ],
        }


def _write(
    item_id: uuid.UUID,
    *,
    generation: int,
    mutate: Callable[[PlanItem], bool],
) -> bool:
    """Apply ``mutate`` under FOR UPDATE iff the same song generation is still attached."""
    with sync_session() as db:
        item = db.execute(
            select(PlanItem).where(PlanItem.id == item_id).with_for_update()
        ).scalar_one_or_none()
        if item is None or item.song_generation != generation or not item.song_gcs_path:
            return False
        if not mutate(item):
            return False
        db.commit()
        return True


def _analysis_current(row: dict[str, Any] | None, generation: int) -> SongAnalysis | None:
    if not row:
        return None
    try:
        analysis = SongAnalysis.model_validate(row)
    except Exception:  # noqa: BLE001 - an unreadable row is simply stale
        return None
    if analysis.version != SONG_ANALYSIS_VERSION or analysis.generation != generation:
        return None
    return analysis


def _analysis_ready(row: dict[str, Any] | None, generation: int) -> bool:
    """A current analysis that actually finished. ``pending`` and ``failed`` rows
    are current for their generation but unusable, so the render-time inline path
    runs them once (one attempt per call, never a loop) instead of trusting them."""
    analysis = _analysis_current(row, generation)
    return analysis is not None and analysis.status == "ready"


def _alignment_current(row: dict[str, Any] | None, generation: int) -> SongAlignment | None:
    if not row:
        return None
    try:
        alignment = SongAlignment.model_validate(row)
    except Exception:  # noqa: BLE001
        return None
    if alignment.version != SONG_ALIGNMENT_VERSION or alignment.song_generation != generation:
        return None
    return alignment


# ── analyze_user_song_task ────────────────────────────────────────────────────


def _download_song(path: str, generation: int, local: str) -> None:
    from app.storage import download_generation_to_file  # noqa: PLC0415

    download_generation_to_file(path, local, generation=str(generation))


def _analyze(song: dict[str, Any], generation: int) -> SongAnalysis:
    with tempfile.TemporaryDirectory(prefix="user-song-") as tmp:
        local = os.path.join(tmp, "song")
        _download_song(song["path"], generation, local)
        # Raises ``BeatDetectionError`` when ffmpeg broke, so a failed detector is a
        # failed analysis (retried), never a "ready" song with zero beats.
        beats = detect_music_beats_strict(local)
        try:
            words = _song_words(_transcribe_words(local))
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - lyrics are optional; instrumentals are valid
            if _is_transient(exc):
                # A timeout is not "this song has no lyrics": fail the analysis so
                # it is retried instead of freezing a lyric-less song.
                raise
            log.warning("user_song_transcribe_failed", generation=generation, exc_info=True)
            words = []
    return SongAnalysis(
        generation=generation,
        status="ready",
        duration_s=song["duration_s"],
        beats_s=[float(b) for b in beats],
        words=words,
        lines=group_words_into_lines(words),
    )


def _store_analysis(item_id: uuid.UUID, generation: int, analysis: SongAnalysis) -> bool:
    def mutate(item: PlanItem) -> bool:
        item.song_analysis = analysis.model_dump(mode="json")
        return True

    return _write(item_id, generation=generation, mutate=mutate)


def _run_analysis(identifier: uuid.UUID, generation: int, *, item_id: str) -> dict[str, Any]:
    """The body of ``analyze_user_song_task`` (also the worker's inline fallback).

    Records a failure on the row instead of raising; only ``SoftTimeLimitExceeded``
    and a crash outside the analysis itself propagate to the caller.
    """
    song = _read_song(identifier)
    if song is None or song["generation"] != generation:
        return {"item_id": item_id, "status": "stale"}
    existing = _analysis_current(song["analysis"], generation)
    if existing is not None and existing.status == "ready":
        # Idempotent re-delivery: nothing to recompute, but alignment may
        # still be owed (a clip could have attached meanwhile).
        enqueue_user_song_alignment(identifier)
        return {"item_id": item_id, "status": "cached"}
    try:
        analysis = _analyze(song, generation)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - recorded on the row, never raised
        log.warning("user_song_analysis_failed", item_id=item_id, exc_info=True)
        analysis = SongAnalysis(
            generation=generation,
            status="failed",
            duration_s=song["duration_s"],
            error=str(exc)[:300] or exc.__class__.__name__,
        )
    stored = _store_analysis(identifier, generation, analysis)
    if stored and analysis.status == "ready":
        enqueue_user_song_alignment(identifier)
    return {
        "item_id": item_id,
        "status": analysis.status if stored else "stale",
        "beats": len(analysis.beats_s),
        "words": len(analysis.words),
    }


@celery_app.task(
    bind=True,
    name="tasks.analyze_user_song",
    soft_time_limit=600,
    time_limit=660,
    max_retries=0,
)
def analyze_user_song_task(self, item_id: str, generation: int) -> dict[str, Any]:  # noqa: ANN001
    """Beats + lyric word timings for the attached song. Never raises."""
    del self
    try:
        identifier = uuid.UUID(str(item_id))
        generation = int(generation)
    except (TypeError, ValueError):
        return {"item_id": str(item_id), "status": "invalid"}
    try:
        return _run_analysis(identifier, generation, item_id=str(item_id))
    except SoftTimeLimitExceeded:
        try:
            _store_analysis(
                identifier,
                generation,
                SongAnalysis(generation=generation, status="failed", error="analysis timed out"),
            )
        except Exception:  # noqa: BLE001
            log.warning("user_song_analysis_timeout_record_failed", exc_info=True)
        return {"item_id": str(item_id), "status": "failed"}
    except Exception:  # noqa: BLE001 - background task: never fatal
        log.warning("user_song_analysis_crashed", item_id=str(item_id), exc_info=True)
        return {"item_id": str(item_id), "status": "failed"}


# ── align_user_song_takes_task ────────────────────────────────────────────────


def _take_proxy_generation(clip: dict[str, Any]) -> int | None:
    return _generation_int(clip.get("storage_generation") or clip.get("generation"))


def _download_take(clip: dict[str, Any], local: str) -> None:
    from app.storage import download_generation_to_file, download_to_file  # noqa: PLC0415

    generation = _take_proxy_generation(clip)
    if generation is not None:
        download_generation_to_file(str(clip["gcs_path"]), local, generation=str(generation))
    else:
        download_to_file(str(clip["gcs_path"]), local)


def _prepare_take(clip: dict[str, Any], tmp: str) -> dict[str, Any] | None:
    """Decode one take; None when it has no readable audio (-> unmatched)."""
    local = os.path.join(tmp, f"take-{clip['media_id']}")
    _download_take(clip, local)
    try:
        pcm = _decode_pcm(local)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:  # noqa: BLE001
        if _is_transient(exc):
            raise  # the caller leaves the take out of the stored alignment (retried)
        pcm = None
    if pcm is None or len(pcm) == 0:
        return None
    try:
        words = _transcribe_words(local)
    except SoftTimeLimitExceeded:
        raise
    except Exception:  # noqa: BLE001 - text anchors are supporting evidence only
        words = []
    return {"media_id": str(clip["media_id"]), "pcm": pcm, "words": words}


def _compute_alignments(
    song: dict[str, Any], analysis: SongAnalysis, todo: list[dict[str, Any]]
) -> dict[str, TakeAlignment]:
    results: dict[str, TakeAlignment] = {}
    offset = float(settings.song_alignment_proxy_offset_s)
    with tempfile.TemporaryDirectory(prefix="user-song-align-") as tmp:
        prepared: list[dict[str, Any]] = []
        for clip in todo:
            media_id = str(clip["media_id"])
            try:
                take = _prepare_take(clip, tmp)
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 - one unreadable take must not stop the rest
                log.warning("user_song_take_prepare_failed", media_id=media_id, exc_info=True)
                if _is_transient(exc):
                    # Not stored at all: the take stays "to do" and the next run retries it.
                    continue
                take = None
            if take is None:
                results[media_id] = _unmatched(media_id, _take_proxy_generation(clip))
            else:
                prepared.append(take)
        if not prepared:
            return results
        song_local = os.path.join(tmp, "song")
        _download_song(song["path"], song["generation"], song_local)
        try:
            song_pcm = _decode_pcm(song_local)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            if _is_transient(exc):
                # The song could not be read right now; no take can be judged. Leave
                # the prepared takes out of the stored alignment so they are retried.
                log.warning("user_song_song_decode_transient", exc_info=True)
                return results
            song_pcm = None
        by_id = {str(clip["media_id"]): clip for clip in todo}
        if song_pcm is None or len(song_pcm) == 0:
            for take in prepared:
                media_id = take["media_id"]
                results[media_id] = _unmatched(media_id, _take_proxy_generation(by_id[media_id]))
            return results
        try:
            aligned = _run_aligner(song_pcm, analysis, prepared)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - recorded as unmatched; the planner will ask
            log.warning("user_song_aligner_failed", exc_info=True)
            if _is_transient(exc):
                return results
            aligned = {}
        for take in prepared:
            media_id = take["media_id"]
            proxy_generation = _take_proxy_generation(by_id[media_id])
            alignment = aligned.get(media_id)
            if alignment is None:
                results[media_id] = _unmatched(media_id, proxy_generation)
                continue
            results[media_id] = _shift(
                alignment.model_copy(
                    update={"media_id": media_id, "proxy_generation": proxy_generation}
                ),
                offset,
            )
    return results


def _store_alignment(
    item_id: uuid.UUID,
    generation: int,
    results: dict[str, TakeAlignment],
) -> bool:
    def mutate(item: PlanItem) -> bool:
        current = _alignment_current(
            dict(item.song_alignment) if isinstance(item.song_alignment, dict) else None,
            generation,
        )
        takes = dict(current.takes) if current else {}
        takes.update(results)
        live = {
            str(row.get("media_id"))
            for row in (item.clip_assignments or [])
            if isinstance(row, dict) and row.get("media_id")
        }
        # Drop rows for takes the creator removed since the last run.
        takes = {media_id: take for media_id, take in takes.items() if media_id in live}
        item.song_alignment = SongAlignment(song_generation=generation, takes=takes).model_dump(
            mode="json"
        )
        return True

    return _write(item_id, generation=generation, mutate=mutate)


def _run_alignment(identifier: uuid.UUID, *, item_id: str) -> dict[str, Any]:
    """The body of ``align_user_song_takes_task`` (also the worker's inline fallback)."""
    song = _read_song(identifier)
    if song is None:
        return {"item_id": item_id, "status": "no_song"}
    generation = song["generation"]
    analysis = _analysis_current(song["analysis"], generation)
    if analysis is None or analysis.status == "pending":
        # analyze_user_song_task enqueues alignment when it finishes.
        return {"item_id": item_id, "status": "waiting_analysis"}
    if analysis.status == "failed":
        return {"item_id": item_id, "status": "analysis_failed"}
    current = _alignment_current(song["alignment"], generation)
    done = current.takes if current else {}
    todo = [
        clip
        for clip in song["clips"]
        if (
            (existing := done.get(str(clip["media_id"]))) is None
            or existing.proxy_generation != _take_proxy_generation(clip)
        )
    ]
    live_ids = {str(clip["media_id"]) for clip in song["clips"]}
    if not todo and set(done) <= live_ids:
        return {"item_id": item_id, "status": "unchanged", "aligned": 0}
    results = _compute_alignments(song, analysis, todo) if todo else {}
    stored = _store_alignment(identifier, generation, results)
    return {"item_id": item_id, "status": "ok" if stored else "stale", "aligned": len(results)}


@celery_app.task(
    bind=True,
    name="tasks.align_user_song_takes",
    soft_time_limit=1200,
    time_limit=1260,
    max_retries=0,
)
def align_user_song_takes_task(self, item_id: str) -> dict[str, Any]:  # noqa: ANN001
    """Place every take of a plan item inside its song. Idempotent; never raises."""
    del self
    try:
        identifier = uuid.UUID(str(item_id))
    except (TypeError, ValueError):
        return {"item_id": str(item_id), "status": "invalid"}
    try:
        return _run_alignment(identifier, item_id=str(item_id))
    except SoftTimeLimitExceeded:
        log.warning("user_song_alignment_timed_out", item_id=str(item_id))
        return {"item_id": str(item_id), "status": "timed_out"}
    except Exception:  # noqa: BLE001 - background task: never fatal
        log.warning("user_song_alignment_crashed", item_id=str(item_id), exc_info=True)
        return {"item_id": str(item_id), "status": "failed"}


# ── inline fallback for the render worker (KRI-374 D2) ────────────────────────


def ensure_song_alignment(
    item_id: uuid.UUID | str,
) -> tuple[SongAnalysis | None, SongAlignment | None]:
    """Current ``(analysis, alignment)`` for a plan item, computed inline if missing/stale.

    Used by the montage worker so a render never depends on the background tasks
    having won a race: it runs the SAME bodies as the tasks (so the result is the
    row the planner gate would have seen), then re-reads the row. A ``pending``
    (enqueue lost or expired) or ``failed`` analysis is retried once here. Returns
    ``(analysis, None)`` when the alignment could not be produced and
    ``(None, None)`` when there is no usable analysis; it does not raise for a
    recorded failure.
    """
    identifier = uuid.UUID(str(item_id))
    song = _read_song(identifier)
    if song is None:
        return None, None
    generation = song["generation"]
    if not _analysis_ready(song["analysis"], generation):
        _run_analysis(identifier, generation, item_id=str(identifier))
    _run_alignment(identifier, item_id=str(identifier))
    song = _read_song(identifier)
    if song is None:
        return None, None
    return (
        _analysis_current(song["analysis"], song["generation"]),
        _alignment_current(song["alignment"], song["generation"]),
    )


def ensure_song_analysis(item_id: uuid.UUID | str) -> SongAnalysis | None:
    """Current ``SongAnalysis`` for a plan item, computed inline when missing."""
    identifier = uuid.UUID(str(item_id))
    song = _read_song(identifier)
    if song is None:
        return None
    generation = song["generation"]
    if not _analysis_ready(song["analysis"], generation):
        _run_analysis(identifier, generation, item_id=str(identifier))
        song = _read_song(identifier)
        if song is None:
            return None
    return _analysis_current(song["analysis"], song["generation"])


# ── enqueue (call only AFTER the attach commit) ───────────────────────────────


def enqueue_user_song_analysis(item_id: uuid.UUID | str, generation: int) -> None:
    """Fire-and-forget; never raises (the caller already committed its own work)."""
    try:
        analyze_user_song_task.apply_async(
            args=[str(item_id), int(generation)],
            queue=settings.pool_asset_analysis_queue,
            expires=3600,
        )
    except Exception:  # noqa: BLE001
        log.warning("user_song_analysis_enqueue_failed", item_id=str(item_id), exc_info=True)


def enqueue_user_song_alignment(item_id: uuid.UUID | str) -> None:
    """Fire-and-forget; never raises."""
    try:
        align_user_song_takes_task.apply_async(
            args=[str(item_id)],
            queue=settings.pool_asset_analysis_queue,
            expires=3600,
        )
    except Exception:  # noqa: BLE001
        log.warning("user_song_alignment_enqueue_failed", item_id=str(item_id), exc_info=True)
