"""Validate and apply an immutable speech-cleanup preflight snapshot.

The render task is a consumer, never a second detector::

    Job.assembly_plan
      ├─ required_v1 ─> extract private snapshot ─> validate schema + identity
      │                                              │
      │                                              ├─ exact timed words ─> captions
      │                                              └─ exact CutPlan ─────> FFmpeg
      ├─ off_v1 ─────> no cleanup analysis or CutPlan application
      └─ legacy_auto ─> historical render-time detector path

The facade is deliberately independent of ORM and Celery.  It accepts only the
already-immutable Job mapping, validates the outer source envelope against the
typed analysis payload, and exposes the existing ``Word``/``CutPlan`` types used
by the renderers.  It never imports or calls transcription, silence detection,
mixed-gap detection, or retake detection.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from app.pipeline.silence_cut import CutPlan, is_filler_token, plan_summary, remap_words
from app.pipeline.speech_cleanup_analysis import SpeechCleanupAnalysisResult
from app.pipeline.transcribe import Transcript, Word

SpeechCleanupSourceKind = Literal["voiceover", "embedded_spine"]
SpeechCleanupAnalysisView = Literal["full_clip", "talking_head_spine_capped"]

SNAPSHOT_SCHEMA_VERSION = 1
PREFLIGHT_JOB_CONTRACT_FIELD = "speech_cleanup_preflight_contract"
PREFLIGHT_JOB_CONTRACT_VALUE = "snapshot_v1"
SUPPORTED_SNAPSHOT_ENGINE_VERSIONS = frozenset({"preflight-v1-2026-09-05"})
MAX_SNAPSHOT_WINDOW_S = 300.0
AUDIO_APPLY_TIMEOUT_S = 300
_FLOAT_TOLERANCE_S = 1e-3
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_OUTER_KEYS = frozenset(
    {
        "schema_version",
        "analysis_id",
        "engine_version",
        "detector_version",
        "source",
        "analysis",
    }
)
_SOURCE_KEYS = frozenset(
    {
        "kind",
        "media_identity",
        "storage_path",
        "generation",
        "window_start_s",
        "window_end_s",
        "source_policy_fingerprint",
    }
)


class SpeechCleanupSnapshotError(ValueError):
    """The required Job snapshot is absent, malformed, or for another source."""

    def __init__(self, detail: str) -> None:
        self.reason = "snapshot_mismatch"
        self.detail = detail
        super().__init__(f"speech cleanup snapshot mismatch: {detail}")


class SpeechCleanupAudioApplyError(RuntimeError):
    """The accepted CutPlan could not be applied to narration audio."""


def _required_mapping(value: object, *, detail: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpeechCleanupSnapshotError(detail)
    return value


def _required_token(value: object, *, detail: str, maximum: int = 2048) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SpeechCleanupSnapshotError(detail)
    return value


def _required_float(value: object, *, detail: str) -> float:
    if isinstance(value, bool):
        raise SpeechCleanupSnapshotError(detail)
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SpeechCleanupSnapshotError(detail) from exc
    if not math.isfinite(result):
        raise SpeechCleanupSnapshotError(detail)
    return result


@dataclass(frozen=True, slots=True)
class HydratedSpeechCleanupSnapshot:
    """Validated preflight evidence in the renderers' existing native types."""

    analysis_id: str
    engine_version: str
    detector_version: str
    source_kind: SpeechCleanupSourceKind
    media_identity: str
    storage_path: str
    generation: str
    window_start_s: float
    window_end_s: float
    source_policy_fingerprint: str
    analysis: SpeechCleanupAnalysisResult
    # Job-local binding is attached only when hydrating the immutable Job
    # envelope.  The preflight row itself is independent of render clip slots.
    job_source_slot: int | None = None
    job_source_instance_id: str | None = None

    @property
    def window_duration_s(self) -> float:
        return self.window_end_s - self.window_start_s

    @property
    def cut_plan(self) -> CutPlan:
        return self.analysis.cut_plan.to_cut_plan()

    def require_source(
        self,
        *,
        kind: SpeechCleanupSourceKind,
        media_identity: str | None = None,
        storage_path: str | None = None,
        window_start_s: float | None = None,
        window_duration_s: float | None = None,
    ) -> HydratedSpeechCleanupSnapshot:
        """Fence application to the source/window selected by the renderer."""

        if self.source_kind != kind:
            raise SpeechCleanupSnapshotError("source_kind")
        if media_identity is not None and self.media_identity != media_identity:
            raise SpeechCleanupSnapshotError("media_identity")
        if storage_path is not None and self.storage_path != storage_path:
            raise SpeechCleanupSnapshotError("storage_path")
        if window_start_s is not None and not math.isclose(
            self.window_start_s,
            float(window_start_s),
            rel_tol=0,
            abs_tol=_FLOAT_TOLERANCE_S,
        ):
            raise SpeechCleanupSnapshotError("window_start")
        if window_duration_s is not None and not math.isclose(
            self.window_duration_s,
            float(window_duration_s),
            rel_tol=0,
            abs_tol=_FLOAT_TOLERANCE_S,
        ):
            raise SpeechCleanupSnapshotError("window_duration")
        return self

    def source_words(self) -> list[Word]:
        return [
            Word(
                text=word.text,
                start_s=word.start_s,
                end_s=word.end_s,
                confidence=word.confidence if word.confidence is not None else 1.0,
                segment_avg_logprob=word.segment_avg_logprob,
                segment_no_speech_prob=word.segment_no_speech_prob,
            )
            for word in self.analysis.timed_words
        ]

    def transcript(self, *, apply_cut: bool) -> Transcript:
        """Return exact source words, optionally remapped through the accepted plan."""

        words = self.source_words()
        if apply_cut:
            words = [
                Word(
                    text=item["text"],
                    start_s=float(item["start_s"]),
                    end_s=float(item["end_s"]),
                    confidence=1.0,
                )
                for item in remap_words(words, self.cut_plan)
                if not is_filler_token(item["text"])
            ]
        return Transcript(
            words=words,
            full_text=" ".join(word.text for word in words),
            low_confidence=self.analysis.safety_signals.transcript_low_confidence,
            language=self.analysis.language,
        )

    def outcome_context(
        self,
        *,
        analysis_view: SpeechCleanupAnalysisView,
    ) -> dict[str, Any]:
        plan = self.cut_plan
        return {
            "analysis_attempt_id": self.analysis_id,
            "analysis_view": analysis_view,
            "detector_version": self.detector_version,
            "source_tag": hashlib.sha256(
                (
                    f"speech-cleanup-preflight-source-tag-v1:{self.source_policy_fingerprint}"
                ).encode()
            ).hexdigest()[:16],
            "selected_plan": self.analysis.safety_signals.selected_plan,
            "candidate_status": self.analysis.safety_signals.candidate_status,
            "output_removal_count": len(plan.removed),
            "output_removed_ms": max(0, int(round(plan.time_saved_s * 1000))),
        }

    def legacy_analysis_entry(
        self,
        *,
        analysis_view: SpeechCleanupAnalysisView,
    ) -> dict[str, Any]:
        """Adapt the snapshot to the established renderer application contract."""

        return {
            "failed": False,
            "words": self.source_words(),
            "language": self.analysis.language,
            "plan": self.cut_plan,
            "retake_span_count": sum(
                finding.category == "retake" for finding in self.analysis.findings
            ),
            "review_candidates": [],
            "cut_video_path": None,
            "speech_cleanup_outcome_context": self.outcome_context(analysis_view=analysis_view),
        }

    def summary(self) -> dict[str, Any]:
        return plan_summary(self.cut_plan, original_duration_s=self.window_duration_s)


def hydrate_speech_cleanup_snapshot(value: object) -> HydratedSpeechCleanupSnapshot:
    """Validate one raw ``preflight_snapshot`` mapping and hydrate its CutPlan."""

    outer = _required_mapping(value, detail="missing_snapshot")
    if set(outer) != _OUTER_KEYS or outer.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise SpeechCleanupSnapshotError("outer_schema")

    analysis_id = _required_token(outer.get("analysis_id"), detail="analysis_id", maximum=64)
    try:
        uuid.UUID(analysis_id)
    except ValueError as exc:
        raise SpeechCleanupSnapshotError("analysis_id") from exc
    engine_version = _required_token(
        outer.get("engine_version"), detail="engine_version", maximum=100
    )
    if engine_version not in SUPPORTED_SNAPSHOT_ENGINE_VERSIONS:
        raise SpeechCleanupSnapshotError("engine_version")
    detector_version = _required_token(
        outer.get("detector_version"), detail="detector_version", maximum=100
    )

    source = _required_mapping(outer.get("source"), detail="source")
    if set(source) != _SOURCE_KEYS:
        raise SpeechCleanupSnapshotError("source_schema")
    kind = source.get("kind")
    if kind not in {"voiceover", "embedded_spine"}:
        raise SpeechCleanupSnapshotError("source_kind")
    media_identity = _required_token(source.get("media_identity"), detail="media_identity")
    storage_path = _required_token(source.get("storage_path"), detail="storage_path")
    generation = _required_token(source.get("generation"), detail="generation", maximum=256)
    window_start = _required_float(source.get("window_start_s"), detail="window_start")
    window_end = _required_float(source.get("window_end_s"), detail="window_end")
    if (
        window_start < 0
        or window_end <= window_start
        or window_end - window_start > MAX_SNAPSHOT_WINDOW_S + _FLOAT_TOLERANCE_S
    ):
        raise SpeechCleanupSnapshotError("source_window")
    fingerprint = _required_token(
        source.get("source_policy_fingerprint"),
        detail="source_policy_fingerprint",
        maximum=64,
    )
    if not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise SpeechCleanupSnapshotError("source_policy_fingerprint")

    try:
        analysis = SpeechCleanupAnalysisResult.from_payload(
            _required_mapping(outer.get("analysis"), detail="analysis")
        )
    except (SpeechCleanupSnapshotError, ValidationError, TypeError, ValueError) as exc:
        raise SpeechCleanupSnapshotError("analysis_schema") from exc
    if analysis.source_fingerprint != fingerprint:
        raise SpeechCleanupSnapshotError("analysis_source_fingerprint")
    if analysis.detector_version != detector_version:
        raise SpeechCleanupSnapshotError("analysis_detector_version")
    if not math.isclose(
        analysis.source_window_start_s,
        window_start,
        rel_tol=0,
        abs_tol=_FLOAT_TOLERANCE_S,
    ) or not math.isclose(
        analysis.source_window_end_s,
        window_end,
        rel_tol=0,
        abs_tol=_FLOAT_TOLERANCE_S,
    ):
        raise SpeechCleanupSnapshotError("analysis_source_window")
    duration = window_end - window_start
    if any(word.end_s > duration + _FLOAT_TOLERANCE_S for word in analysis.timed_words):
        raise SpeechCleanupSnapshotError("analysis_word_window")

    return HydratedSpeechCleanupSnapshot(
        analysis_id=analysis_id,
        engine_version=engine_version,
        detector_version=detector_version,
        source_kind=kind,
        media_identity=media_identity,
        storage_path=storage_path,
        generation=generation,
        window_start_s=window_start,
        window_end_s=window_end,
        source_policy_fingerprint=fingerprint,
        analysis=analysis,
    )


# detail card -> generate(analysis_id, choice?, revision)
#                     |
#        lock + validate + atomic decision/Job row
#                     |
#         no_findings + null -> checked_no_change
#         keep -> off_v1 / declined
#         bypass -> off_v1 / bypassed_unchecked
#         clean -> required_v1 Job
#                     + _speech_cleanup_internal.preflight_snapshot
#                     |
#                   COMMIT
#                     |
#          post-commit broker publish OR reconcile
#                     |
#              validate -> FFmpeg apply
#                     |
#         generation-bound receipt OR fail closed
def hydrate_job_speech_cleanup_snapshot(
    assembly_plan: object,
) -> HydratedSpeechCleanupSnapshot:
    """Extract the private preflight snapshot from an immutable Job plan."""

    plan = _required_mapping(assembly_plan, detail="assembly_plan")
    private = _required_mapping(
        plan.get("_speech_cleanup_internal"),
        detail="missing_private_namespace",
    )
    if "preflight_snapshot" not in private:
        raise SpeechCleanupSnapshotError("missing_snapshot")
    snapshot = hydrate_speech_cleanup_snapshot(private["preflight_snapshot"])
    binding = private.get("source_binding")
    marker = plan.get(PREFLIGHT_JOB_CONTRACT_FIELD)
    if marker is not None and marker != PREFLIGHT_JOB_CONTRACT_VALUE:
        raise SpeechCleanupSnapshotError("job_contract")
    if snapshot.source_kind != "embedded_spine":
        if binding is not None:
            raise SpeechCleanupSnapshotError("unexpected_source_binding")
        return snapshot
    if binding is None:
        # Compatibility for snapshots created during the first staged rollout.
        # New snapshot_v1 Jobs always carry a source binding, and therefore fail
        # closed if that binding is stripped or malformed.
        if marker == PREFLIGHT_JOB_CONTRACT_VALUE:
            raise SpeechCleanupSnapshotError("missing_source_binding")
        return snapshot
    binding_map = _required_mapping(binding, detail="source_binding")
    if set(binding_map) != {"source_slot", "source_instance_id"}:
        raise SpeechCleanupSnapshotError("source_binding_schema")
    source_slot = binding_map.get("source_slot")
    if isinstance(source_slot, bool) or not isinstance(source_slot, int) or source_slot < 0:
        raise SpeechCleanupSnapshotError("source_slot")
    source_instance_id = _required_token(
        binding_map.get("source_instance_id"),
        detail="source_instance_id",
        maximum=64,
    )
    try:
        source_instance_id = str(uuid.UUID(source_instance_id))
    except ValueError as exc:
        raise SpeechCleanupSnapshotError("source_instance_id") from exc
    return HydratedSpeechCleanupSnapshot(
        analysis_id=snapshot.analysis_id,
        engine_version=snapshot.engine_version,
        detector_version=snapshot.detector_version,
        source_kind=snapshot.source_kind,
        media_identity=snapshot.media_identity,
        storage_path=snapshot.storage_path,
        generation=snapshot.generation,
        window_start_s=snapshot.window_start_s,
        window_end_s=snapshot.window_end_s,
        source_policy_fingerprint=snapshot.source_policy_fingerprint,
        analysis=snapshot.analysis,
        job_source_slot=source_slot,
        job_source_instance_id=source_instance_id,
    )


def apply_speech_cleanup_to_audio(
    snapshot: HydratedSpeechCleanupSnapshot,
    source_path: str,
    output_path: str,
    *,
    timeout_s: int = AUDIO_APPLY_TIMEOUT_S,
) -> str:
    """Render the accepted keep segments into one narration-audio artifact."""

    if not source_path or not output_path or timeout_s <= 0:
        raise SpeechCleanupAudioApplyError("invalid audio application input")
    plan = snapshot.cut_plan
    if not plan.keep_segments:
        raise SpeechCleanupAudioApplyError("accepted CutPlan has no keep segments")

    chains: list[str] = []
    labels: list[str] = []
    for index, (start_s, end_s) in enumerate(plan.keep_segments):
        absolute_start = snapshot.window_start_s + float(start_s)
        absolute_end = snapshot.window_start_s + float(end_s)
        label = f"keep{index}"
        labels.append(f"[{label}]")
        chains.append(
            f"[0:a]atrim=start={absolute_start:.6f}:end={absolute_end:.6f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
    chains.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[outa]")
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        source_path,
        "-filter_complex",
        ";".join(chains),
        "-map",
        "[outa]",
        "-vn",
        "-c:a",
        "pcm_s16le",
        "-ar",
        "48000",
        "-y",
        output_path,
    ]
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable and argv, never a shell
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SpeechCleanupAudioApplyError("speech cleanup audio application failed") from exc
    if (
        result.returncode != 0
        or not os.path.exists(output_path)
        or os.path.getsize(output_path) <= 0
    ):
        raise SpeechCleanupAudioApplyError("speech cleanup audio application failed")
    return output_path
