"""Single source-aware writer for PlanItem narration media and policy.

Callers must acquire the PlanItem row with ``FOR UPDATE`` before invoking this
facade.  The helper deliberately performs no commit and no broker call: it
returns a durable scheduling intent that the caller publishes only after the
transaction containing the media change has committed.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from app.models import PlanItem
from app.services.active_narration_source import (
    ActiveNarrationRequest,
    ActiveNarrationResolution,
    RegisteredNarrationMedia,
    resolve_active_narration_source,
)

_UNSET: Final = object()

# CI's sole-writer guard consumes this explicit list. Metadata-only analysis
# writers may update fields inside individual assignment dictionaries, but the
# ordered source vector and these policy fields have one owner.
PROTECTED_PLAN_ITEM_MEDIA_FIELDS = frozenset(
    {
        "clip_gcs_paths",
        "clip_assignments",
        "voiceover_gcs_path",
        "voiceover_generation",
        "voiceover_duration_s",
        "edit_format",
        "audio_mode",
    }
)


def current_detector_policy() -> str:
    """Return the stable policy token included in every source fingerprint."""

    from app.services.speech_cleanup_preflight import (
        SPEECH_CLEANUP_ENGINE_VERSION,
        SPEECH_CLEANUP_MAX_DURATION_S,
        SPEECH_CLEANUP_MIXED_GAP_MODE,
        SPEECH_CLEANUP_OVER_BUDGET_POLICY,
    )
    from app.services.speech_cleanup_selection import DETECTOR_VERSION

    return ":".join(
        (
            SPEECH_CLEANUP_ENGINE_VERSION,
            DETECTOR_VERSION,
            f"mixed-gap={SPEECH_CLEANUP_MIXED_GAP_MODE}",
            f"over-budget={SPEECH_CLEANUP_OVER_BUDGET_POLICY}",
            f"max-duration={SPEECH_CLEANUP_MAX_DURATION_S:g}",
        )
    )


@dataclass(frozen=True)
class MediaMutationResult:
    """Outcome of one locked media mutation.

    ``schedule`` is true only when a new active narration fingerprint exists
    and differs from the prior one. Visual-only changes under an unchanged
    active voiceover therefore preserve both analysis and choice.
    """

    source_changed: bool
    previous: ActiveNarrationResolution
    current: ActiveNarrationResolution
    schedule: bool

    @property
    def source_policy_fingerprint(self) -> str | None:
        return self.current.source.source_policy_fingerprint if self.current.source else None


def _media_id(value: dict[str, Any], index: int) -> str:
    explicit = str(value.get("media_id") or "").strip()
    if explicit:
        return explicit
    path = str(value.get("gcs_path") or "")
    return hashlib.sha256(f"legacy:{index}:{path}".encode()).hexdigest()[:32]


def _registered_clips(item: PlanItem) -> tuple[RegisteredNarrationMedia, ...]:
    assignments = [
        value
        for value in (getattr(item, "clip_assignments", None) or [])
        if isinstance(value, dict) and value.get("gcs_path")
    ]
    if not assignments:
        assignments = [
            {"gcs_path": path, "media_id": f"legacy-{index}"}
            for index, path in enumerate(getattr(item, "clip_gcs_paths", None) or [])
            if path
        ]
    registered: list[RegisteredNarrationMedia] = []
    for index, value in enumerate(assignments):
        generation = (
            value.get("storage_generation")
            or value.get("generation")
            or value.get("duration_probe_generation")
        )
        registered.append(
            RegisteredNarrationMedia(
                media_id=_media_id(value, index),
                storage_path=str(value["gcs_path"]),
                # Legacy rows without a registered object generation are not
                # safe to analyze: the path could now point at different bytes.
                generation=str(generation or ""),
                media_kind="video",
                duration_s=float(value.get("duration_s") or 0.0),
                # Registration currently proves the MIME kind. The analysis
                # worker performs the bounded authoritative audio probe.
                has_audio=value.get("has_audio", True) is not False,
                speech_coverage=value.get("speech_coverage"),
                trim_start_s=float(value.get("trim_start_s") or 0.0),
                trim_end_s=(
                    float(value["trim_end_s"]) if value.get("trim_end_s") is not None else None
                ),
                manifest_identity=str(value.get("manifest_identity") or _media_id(value, index)),
                foreground=value.get("foreground", True) is not False,
            )
        )
    return tuple(registered)


def active_narration_request(
    item: PlanItem,
    *,
    detector_policy: str,
) -> ActiveNarrationRequest:
    """Build the renderer-aligned resolver input from persisted PlanItem data."""

    voiceover_path = str(getattr(item, "voiceover_gcs_path", None) or "").strip()
    voiceover_generation = str(getattr(item, "voiceover_generation", None) or "").strip()
    voiceover = None
    if voiceover_path:
        identity = hashlib.sha256(
            f"voiceover:{voiceover_path}:{voiceover_generation}".encode()
        ).hexdigest()[:32]
        voiceover = RegisteredNarrationMedia(
            media_id=identity,
            storage_path=voiceover_path,
            generation=voiceover_generation,
            media_kind="audio",
            duration_s=float(getattr(item, "voiceover_duration_s", None) or 0.0),
            has_audio=True,
            manifest_identity=identity,
            foreground=True,
        )
    return ActiveNarrationRequest(
        edit_format=str(getattr(item, "edit_format", "") or ""),
        audio_mode=str(getattr(item, "audio_mode", "") or ""),
        detector_policy=detector_policy,
        clips=_registered_clips(item),
        voiceover=voiceover,
    )


def resolve_item_narration(
    item: PlanItem,
    *,
    detector_policy: str,
) -> ActiveNarrationResolution:
    return resolve_active_narration_source(
        active_narration_request(item, detector_policy=detector_policy)
    )


def _normalize_assignments(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in assignments:
        if not isinstance(raw, dict):
            raise ValueError("clip assignment must be an object")
        path = str(raw.get("gcs_path") or "").strip()
        if not path:
            raise ValueError("clip assignment requires gcs_path")
        if path in seen:
            raise ValueError("duplicate clip gcs_path")
        seen.add(path)
        entry = dict(raw)
        entry["gcs_path"] = path
        if not entry.get("media_id"):
            entry["media_id"] = uuid.uuid4().hex
        normalized.append(entry)
    if len(normalized) > 50:
        raise ValueError("too many clips")
    return normalized


# chat/editor command
#        |
#        v
# lock PlanItem -> resolve old source -> mutate -> resolve new source
#        |                                  |
#        +---------- same transaction ------+
#                         |
#       supersede stale analysis + clear stale consent
#                         |
#                         v
#               post-commit scheduling intent
def mutate_plan_item_media(
    item: PlanItem,
    *,
    detector_policy: str,
    clip_assignments: list[dict[str, Any]] | object = _UNSET,
    voiceover_gcs_path: str | None | object = _UNSET,
    voiceover_generation: str | None | object = _UNSET,
    voiceover_duration_s: float | None | object = _UNSET,
    edit_format: str | object = _UNSET,
    audio_mode: str | object = _UNSET,
    current_analysis: Any | None = None,
    now: datetime | None = None,
) -> MediaMutationResult:
    """Mutate protected fields and atomically invalidate a stale analysis.

    ``current_analysis`` must be the row selected with the same transaction's
    current-row predicate.  Passing it keeps this facade ORM-agnostic enough for
    route and task callers while ensuring the supersession and media write share
    one commit.
    """

    previous = resolve_item_narration(item, detector_policy=detector_policy)
    # Footage identity is tracked separately from the narration fingerprint.
    # The fingerprint is None whenever no narration source resolves (montage
    # formats, clips without audio), so it cannot carry the legacy
    # "media changed -> approved proposal is stale" contract on its own.
    from app.services.speech_cleanup import (  # noqa: PLC0415
        main_footage_identity,
        reconcile_consent,
    )

    previous_footage = main_footage_identity(item)
    if clip_assignments is not _UNSET:
        normalized = _normalize_assignments(list(clip_assignments))
        item.clip_assignments = normalized
        # Preserve attach order in the rich assignment vector: the renderer's
        # narrative ordering code intentionally derives guide order from the
        # live filming guide instead of trusting client attach order. Keep the
        # long-standing slots-first compatibility order only in the legacy
        # flat path list.
        item.clip_gcs_paths = [
            *[entry["gcs_path"] for entry in normalized if entry.get("shot_id") is not None],
            *[entry["gcs_path"] for entry in normalized if entry.get("shot_id") is None],
        ]
    if voiceover_gcs_path is not _UNSET:
        item.voiceover_gcs_path = voiceover_gcs_path
    if voiceover_generation is not _UNSET:
        item.voiceover_generation = voiceover_generation
    if voiceover_duration_s is not _UNSET:
        item.voiceover_duration_s = voiceover_duration_s
    if edit_format is not _UNSET:
        item.edit_format = str(edit_format)
    if audio_mode is not _UNSET:
        item.audio_mode = str(audio_mode)

    current = resolve_item_narration(item, detector_policy=detector_policy)
    previous_fingerprint = previous.source.source_policy_fingerprint if previous.source else None
    current_fingerprint = current.source.source_policy_fingerprint if current.source else None
    changed = previous_fingerprint != current_fingerprint
    # The narration fingerprint is authoritative whenever a source resolves on
    # either side: it already distinguishes "the voiceover still owns the
    # speech, only the visuals moved" from a real speech change. It carries no
    # information only when NO source resolves before or after -- montage
    # formats, clips whose generation is not registered yet, multi-clip narrated
    # items without speech coverage. Fall back to footage identity there, and
    # only there, so a replacement cannot look unchanged.
    footage_only = previous.source is None and current.source is None
    footage_changed = footage_only and main_footage_identity(item) != previous_footage
    if changed:
        settled_at = now or datetime.now(UTC)
        if (
            current_analysis is not None
            and getattr(current_analysis, "superseded_at", None) is None
        ):
            current_analysis.superseded_at = settled_at
        # False is only the renderer compatibility mirror. The durable analysis
        # decision is authoritative and is cleared by superseding its row.
        item.speech_cleanup_enabled = False
        item.speech_cleanup_notice = None
    if footage_changed:
        # Legacy opt-in consent still becomes a required_v1 contract via
        # speech_cleanup.contract_for_item, so consent granted for the old
        # footage must not survive a replacement and cut bytes the creator never
        # approved. reconcile_consent is a no-op when consent was never given.
        reconcile_consent(item, previous_footage)
    if changed or footage_changed:
        try:
            from app.services.edit_proposals import mark_edit_proposal_stale

            mark_edit_proposal_stale(item)
        except ImportError:
            # Keep this service importable in schema/migration tooling.
            pass
    return MediaMutationResult(
        source_changed=changed,
        previous=previous,
        current=current,
        schedule=bool(changed and current.source is not None),
    )


def publish_preflight_after_commit(analysis_id: str | uuid.UUID | None) -> bool:
    """Best-effort fast-path publication; Beat owns durable recovery."""

    if analysis_id is None:
        return False
    try:
        from app.tasks.speech_cleanup_analysis import analyze_speech_cleanup

        analyze_speech_cleanup.apply_async(args=[str(analysis_id)])
    except Exception:
        return False
    return True
