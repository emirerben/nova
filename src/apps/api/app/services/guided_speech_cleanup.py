"""Clean up speech for guided narrated stories through a pinned derivative.

A guided story plans, captions and renders against ``NarrationTrack``. When the
creator confirms "Clean up speech", the draft task applies the confirmed
preflight CutPlan to the raw voiceover exactly once and pins the result::

    confirmed attempt (choice=clean, analysis_id)
            |
    load_clean_consent_snapshot ── row current? same source? window whole?
            |
    build_cleaned_narration ── download raw generation -> FFmpeg keep segments
            |                   -> immutable WAV users/{owner}/plan/{item}/
            |                      speech-cleanup/{analysis}/{sha[:32]}.wav
            v
    NarrationTrack(gcs_path=<derivative>, duration_s=<cleaned>,
                   words=<snapshot words remapped through the cut>,
                   speech_cleanup=<provenance: source triple + cut_sha256>)

``duration_s`` and ``words`` keep meaning "the file the renderer plays", so the
planner, compiler and phone recipe need no second timeline. Every identity
boundary that compares narration to the item uses ``source_identity`` and
``derivative_path_ok``; render workers and the device grant use
``require_guided_cleanup_binding`` to prove the derivative matches the Job's
immutable preflight snapshot. Nothing here ever falls back to the uncut file.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas.edit_proposal import NarrationSpeechCleanup, NarrationTrack

SPEECH_CLEANUP_CHANGED = "speech_cleanup_changed"
SPEECH_CLEANUP_UNAVAILABLE = "speech_cleanup_unavailable"
# Cleanup switched off (rollout flag or silence cut) when the attempt ran.
# Its own code, so a Clean retry reopens once cleanup is available again.
SPEECH_CLEANUP_DISABLED = "speech_cleanup_disabled"
CHANGED_MESSAGE = "The detected pauses changed. Refresh the direction to choose again."
DISABLED_MESSAGE = (
    "Clean up speech isn't available right now. Keep the original speech to create this video."
)
WINDOW_MESSAGE = "Clean up speech works on voiceovers up to 5 minutes."
IO_MESSAGE = "Kria couldn't prepare the cleaned speech. Try again."
# The analysis window must cover the whole recording; the renderer plays all
# of it, so a capped window would ship uncut audio after its end.
WINDOW_TOLERANCE_S = 2e-3
# ffprobe of the written WAV against the accepted keep segments.
DURATION_TOLERANCE_S = 0.05
PROBE_TIMEOUT_S = 30
DERIVATIVE_CONTENT_TYPE = "audio/wav"
_EMPTY_IDENTITY: tuple[str, str, float] = ("", "", 0.0)
_HASH_NAME_RE = re.compile(r"[0-9a-f]{32}\.wav")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class GuidedSpeechCleanupError(RuntimeError):
    """Typed, creator-visible reason a clean choice cannot be honored."""

    def __init__(self, code: str, message: str, retryable: bool) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


def guided_voiceover_cleanup_available() -> bool:
    """Whether a guided "Clean up speech" choice can be honored right now."""

    from app.config import settings  # noqa: PLC0415

    return bool(settings.guided_voiceover_speech_cleanup_enabled and settings.silence_cut_enabled)


def _changed() -> GuidedSpeechCleanupError:
    return GuidedSpeechCleanupError(SPEECH_CLEANUP_CHANGED, CHANGED_MESSAGE, retryable=False)


def _io_unavailable() -> GuidedSpeechCleanupError:
    return GuidedSpeechCleanupError(SPEECH_CLEANUP_UNAVAILABLE, IO_MESSAGE, retryable=True)


def _canonical_uuid(value: object) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return None


def derivative_object_path(
    *,
    owner_id: object,
    item_id: object,
    analysis_id: object,
    sha256: str,
) -> str:
    """Content-addressed storage name of one cleaned voiceover derivative."""

    owner, item, analysis = (_canonical_uuid(v) for v in (owner_id, item_id, analysis_id))
    if owner is None or item is None or analysis is None or not _SHA256_RE.fullmatch(sha256):
        raise ValueError("invalid speech cleanup derivative identity")
    return f"users/{owner}/plan/{item}/speech-cleanup/{analysis}/{sha256[:32]}.wav"


def derivative_item_prefix(*, owner_id: object, item_id: object) -> str:
    """Storage prefix holding every cleaned derivative of one item's voiceovers."""

    owner, item = (_canonical_uuid(v) for v in (owner_id, item_id))
    if owner is None or item is None:
        raise ValueError("invalid speech cleanup derivative prefix")
    return f"users/{owner}/plan/{item}/speech-cleanup/"


def derivative_path_ok(
    path: object,
    *,
    owner_id: object,
    item_id: object,
    analysis_id: object,
) -> bool:
    """Whether ``path`` is a derivative this owner/item/analysis could have written.

    The prefix is server-owned and never client-writable, so a matching name
    cannot point at another creator's, item's or analysis's audio.
    """

    owner, item, analysis = (_canonical_uuid(v) for v in (owner_id, item_id, analysis_id))
    if owner is None or item is None or analysis is None or not isinstance(path, str):
        return False
    prefix = f"users/{owner}/plan/{item}/speech-cleanup/{analysis}/"
    return path.startswith(prefix) and bool(_HASH_NAME_RE.fullmatch(path[len(prefix) :]))


def narration_speech_cleanup(narration: object) -> NarrationSpeechCleanup | None:
    """Parse the provenance of a typed or JSON narration; raise if malformed."""

    if narration is None:
        return None
    if isinstance(narration, NarrationTrack):
        return narration.speech_cleanup
    if not isinstance(narration, Mapping):
        raise ValueError("narration must be a NarrationTrack or a mapping")
    raw = narration.get("speech_cleanup")
    return None if raw is None else NarrationSpeechCleanup.model_validate(raw)


def source_identity(narration: NarrationTrack | Mapping[str, Any]) -> tuple[str, str, float]:
    """Return the raw ``(gcs_path, generation, duration_s)`` a narration came from.

    A cleaned derivative answers with its provenance's source triple; any other
    narration is its own source. Malformed values return an empty identity so
    a caller comparing it against the item fails closed.
    """

    try:
        provenance = narration_speech_cleanup(narration)
    except ValueError:
        return _EMPTY_IDENTITY
    if provenance is not None:
        return (
            provenance.source_gcs_path,
            provenance.source_generation,
            float(provenance.source_duration_s),
        )
    if isinstance(narration, NarrationTrack):
        return narration.gcs_path, narration.generation, float(narration.duration_s)
    try:
        duration_s = float(narration.get("duration_s") or 0)
    except (TypeError, ValueError):
        return _EMPTY_IDENTITY
    return (
        str(narration.get("gcs_path") or ""),
        str(narration.get("generation") or ""),
        duration_s,
    )


def load_clean_consent_snapshot(db, item: Any, analysis_id: str) -> dict[str, Any]:  # noqa: ANN001
    """Return the private preflight snapshot a confirmed clean choice consented to.

    Runs under the caller's PlanItem lock. Only the exact, still-current,
    whole-recording voiceover analysis qualifies; anything else raises a typed
    non-retryable ``GuidedSpeechCleanupError`` instead of planning uncut audio.
    """

    from app.models import SpeechCleanupAnalysis  # noqa: PLC0415
    from app.services.plan_item_media import (  # noqa: PLC0415
        current_detector_policy,
        resolve_item_narration,
    )
    from app.services.speech_cleanup_preflight import (  # noqa: PLC0415
        _source_snapshot_matches,
        analysis_snapshot,
    )

    if not guided_voiceover_cleanup_available():
        raise GuidedSpeechCleanupError(SPEECH_CLEANUP_DISABLED, DISABLED_MESSAGE, retryable=False)
    identifier = _canonical_uuid(analysis_id)
    if identifier is None:
        raise _changed()
    row = db.get(SpeechCleanupAnalysis, uuid.UUID(identifier), populate_existing=True)
    if (
        row is None
        or row.plan_item_id != item.id
        or row.superseded_at is not None
        or row.status != "ready"
        or int(row.candidate_count or 0) <= 0
        or row.source_kind != "voiceover"
    ):
        raise _changed()
    source = resolve_item_narration(item, detector_policy=current_detector_policy()).source
    if source is None or not _source_snapshot_matches(row, source):
        raise _changed()
    duration_s = float(getattr(item, "voiceover_duration_s", 0) or 0)
    if (
        float(row.window_start_s or 0) != 0.0
        or row.window_end_s is None
        or abs(float(row.window_end_s) - duration_s) > WINDOW_TOLERANCE_S
    ):
        raise GuidedSpeechCleanupError(SPEECH_CLEANUP_UNAVAILABLE, WINDOW_MESSAGE, retryable=False)
    try:
        return analysis_snapshot(row)
    except ValueError as exc:
        raise _changed() from exc


def _probe_duration_s(path: str) -> float:
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable and argv, never a shell
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=True,
        )
        return float(result.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise _io_unavailable() from exc


def _file_sha256(path: str) -> str:
    with open(path, "rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def build_cleaned_narration(
    raw: NarrationTrack,
    payload: Mapping[str, Any],
    *,
    owner_id: object,
    item_id: object,
) -> NarrationTrack:
    """Apply the consented CutPlan to the raw voiceover and pin the result.

    Idempotent: the WAV is content-addressed and uploaded with a create-only
    precondition, so a re-driven attempt resolves to the same object and
    generation. Snapshot/plan disagreements raise ``speech_cleanup_changed``
    (not retryable); storage and FFmpeg failures raise
    ``speech_cleanup_unavailable`` (retryable).
    """

    from app import storage  # noqa: PLC0415
    from app.pipeline.speech_cleanup_apply import (  # noqa: PLC0415
        SpeechCleanupAudioApplyError,
        SpeechCleanupSnapshotError,
        apply_speech_cleanup_to_audio,
        cut_fingerprint,
        hydrate_speech_cleanup_snapshot,
    )

    try:
        snapshot = hydrate_speech_cleanup_snapshot(payload).require_source(
            kind="voiceover",
            storage_path=raw.gcs_path,
            window_start_s=0.0,
            window_duration_s=raw.duration_s,
        )
        if snapshot.generation != raw.generation:
            raise SpeechCleanupSnapshotError("generation")
        keep_segments = list(snapshot.cut_plan.keep_segments)
        if not keep_segments:
            raise SpeechCleanupSnapshotError("keep_segments")
        cut_sha256 = cut_fingerprint(snapshot)
    except SpeechCleanupSnapshotError as exc:
        raise _changed() from exc
    expected_s = sum(float(end_s) - float(start_s) for start_s, end_s in keep_segments)

    with tempfile.TemporaryDirectory(prefix="guided-speech-cleanup-") as tmpdir:
        source_local = os.path.join(tmpdir, f"source{Path(raw.gcs_path).suffix.lower() or '.m4a'}")
        cleaned_local = os.path.join(tmpdir, "cleaned.wav")
        try:
            metadata = storage.object_metadata(raw.gcs_path)
        except Exception as exc:  # noqa: BLE001 - any storage failure is transient here
            raise _io_unavailable() from exc
        if str(metadata.generation) != raw.generation:
            # The raw recording was replaced after the creator's consent.
            raise _changed()
        try:
            storage.download_generation_to_file(
                raw.gcs_path, source_local, generation=raw.generation
            )
        except Exception as exc:  # noqa: BLE001
            raise _io_unavailable() from exc
        try:
            apply_speech_cleanup_to_audio(snapshot, source_local, cleaned_local)
        except SpeechCleanupAudioApplyError as exc:
            # The CutPlan was validated above, so a failed FFmpeg run (exit
            # code, OOM kill, full disk, timeout) is resource/I/O trouble.
            raise _io_unavailable() from exc
        duration_s = _probe_duration_s(cleaned_local)
        if abs(duration_s - expected_s) > DURATION_TOLERANCE_S:
            raise _changed()
        object_path = derivative_object_path(
            owner_id=owner_id,
            item_id=item_id,
            analysis_id=snapshot.analysis_id,
            sha256=_file_sha256(cleaned_local),
        )
        try:
            uploaded = storage.upload_local_file_immutable(
                cleaned_local, object_path, DERIVATIVE_CONTENT_TYPE
            )
        except Exception as exc:  # noqa: BLE001
            raise _io_unavailable() from exc

    words = [
        {
            "text": word.text.strip(),
            "start_s": word.start_s,
            "end_s": word.end_s,
            "confidence": word.confidence,
        }
        for word in snapshot.transcript(apply_cut=True).words
        if str(word.text or "").strip()
    ]
    try:
        return NarrationTrack.model_validate(
            {
                "gcs_path": object_path,
                "generation": str(uploaded.generation),
                "duration_s": round(duration_s, 6),
                "words": words,
                "language": snapshot.analysis.language or "",
                "caption_style": raw.caption_style,
                "speech_cleanup": {
                    "analysis_id": snapshot.analysis_id,
                    "source_gcs_path": raw.gcs_path,
                    "source_generation": raw.generation,
                    "source_duration_s": raw.duration_s,
                    "cut_sha256": cut_sha256,
                },
            }
        )
    except ValidationError as exc:
        raise _changed() from exc


def reusable_cleaned_narration(
    saved: NarrationTrack | None,
    raw: NarrationTrack,
    payload: Mapping[str, Any],
    *,
    owner_id: object,
    item_id: object,
) -> NarrationTrack | None:
    """Return the derivative a re-driven attempt already pinned, if it still holds.

    The cut is only byte-stable on one host: FFmpeg's AAC decoder and
    resampler pick CPU-specific code paths, so a Celery retry on another
    machine could hash a re-cut to a different name and orphan the attempt's
    committed drafting step. Reuse the pinned file when its provenance equals
    this consent (analysis, raw source, cut fingerprint), its path is this
    owner/item/analysis's, and the stored object is the generation recorded.
    """

    from app import storage  # noqa: PLC0415
    from app.pipeline.speech_cleanup_apply import (  # noqa: PLC0415
        SpeechCleanupSnapshotError,
        cut_fingerprint,
        hydrate_speech_cleanup_snapshot,
    )

    provenance = saved.speech_cleanup if saved is not None else None
    if saved is None or provenance is None:
        return None
    try:
        snapshot = hydrate_speech_cleanup_snapshot(payload).require_source(
            kind="voiceover",
            storage_path=raw.gcs_path,
            window_start_s=0.0,
            window_duration_s=raw.duration_s,
        )
        fingerprint = cut_fingerprint(snapshot)
    except SpeechCleanupSnapshotError:
        return None
    if (
        snapshot.generation != raw.generation
        or provenance.analysis_id != snapshot.analysis_id
        or (provenance.source_gcs_path, provenance.source_generation)
        != (raw.gcs_path, raw.generation)
        or abs(float(provenance.source_duration_s) - float(raw.duration_s)) > 0.001
        or provenance.cut_sha256 != fingerprint
        or not derivative_path_ok(
            saved.gcs_path,
            owner_id=owner_id,
            item_id=item_id,
            analysis_id=snapshot.analysis_id,
        )
    ):
        return None
    try:
        metadata = storage.object_metadata(saved.gcs_path)
    except Exception:  # noqa: BLE001 - missing/unreadable: re-cut instead
        return None
    return saved if str(metadata.generation) == saved.generation else None


def has_preflight_snapshot(assembly_plan: Mapping[str, Any]) -> bool:
    """Whether a Job pins preflight consent (marker or private snapshot)."""

    from app.pipeline.speech_cleanup_apply import (  # noqa: PLC0415
        PREFLIGHT_JOB_CONTRACT_FIELD,
        PREFLIGHT_JOB_CONTRACT_VALUE,
    )

    private = assembly_plan.get("_speech_cleanup_internal")
    return assembly_plan.get(PREFLIGHT_JOB_CONTRACT_FIELD) == PREFLIGHT_JOB_CONTRACT_VALUE or (
        isinstance(private, Mapping) and "preflight_snapshot" in private
    )


def require_guided_cleanup_binding(
    assembly_plan: object,
    narration: object,
) -> dict[str, Any] | None:
    """Prove a guided Job's narration honors its speech-cleanup contract.

    ``required_v1`` with a preflight snapshot: the narration must be a cleaned
    derivative whose analysis, source generation and cut fingerprint equal
    that immutable snapshot; returns its outcome context for the receipt.
    A markerless ``required_v1`` Job (legacy item toggle, no preflight
    consent) keeps its historical raw narration. Any other contract must not
    carry a derivative. Returns ``None`` when no cleanup applies. Raises
    ``SpeechCleanupFailure("snapshot_mismatch")``.
    """

    from app.pipeline.speech_cleanup_apply import (  # noqa: PLC0415
        SpeechCleanupSnapshotError,
        cut_fingerprint,
        hydrate_job_speech_cleanup_snapshot,
    )
    from app.services.speech_cleanup import SpeechCleanupFailure  # noqa: PLC0415

    plan = assembly_plan if isinstance(assembly_plan, Mapping) else {}
    try:
        provenance = narration_speech_cleanup(narration)
    except ValueError as exc:
        raise SpeechCleanupFailure("snapshot_mismatch", "invalid cleaned narration") from exc
    if plan.get("speech_cleanup_contract") != "required_v1":
        if provenance is not None:
            raise SpeechCleanupFailure(
                "snapshot_mismatch", "cleaned narration without a required cleanup contract"
            )
        return None
    if provenance is None:
        if not has_preflight_snapshot(plan):
            return None
        raise SpeechCleanupFailure("snapshot_mismatch", "the confirmed narration is not cleaned")
    try:
        snapshot = hydrate_job_speech_cleanup_snapshot(plan)
    except SpeechCleanupSnapshotError as exc:
        raise SpeechCleanupFailure("snapshot_mismatch", exc.detail) from exc
    if (
        snapshot.source_kind != "voiceover"
        or snapshot.analysis_id != provenance.analysis_id
        or snapshot.storage_path != provenance.source_gcs_path
        or snapshot.generation != provenance.source_generation
        or cut_fingerprint(snapshot) != provenance.cut_sha256
    ):
        raise SpeechCleanupFailure(
            "snapshot_mismatch", "cleaned narration differs from the Job snapshot"
        )
    return snapshot.outcome_context(analysis_view="full_clip")
