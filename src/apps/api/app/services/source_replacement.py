"""Re-recording a take: the pure rules behind `POST /creation-threads/{id}/media/replace`.

A replacement swaps ONE attached speaker clip for a new upload, keeping the clip count and
position. The route owns the transaction; this module owns the decisions that need no I/O so
they can be exercised against every shape of item.

The old ready Job is never rewritten. It keeps the take it was rendered from, and the next
strategy draft mints a NEW Job with its own source identities. `SourceReplacement` is the
marker that tells the planner "the Job this item points at was built from a take the creator
has since replaced", so a follow-up plans a fresh strategy instead of editing the old cuts.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from app.services.active_narration_source import NARRATED_EDIT_FORMATS
from app.services.creator_capabilities import canonical_pool_media_id

SOURCE_REPLACEMENT_STATE_KEY: Final = "source_replacement"
MEDIA_REPLACED_EVENT: Final = "media_replaced"
REASON_RETAKE: Final = "retake"

# Variant render states meaning a render is on its way. A reburn or an editor Save keeps the
# Job terminal while it runs, so the Job status alone cannot say "wait".
_RENDER_IN_FLIGHT_VARIANT_STATUSES: Final = frozenset({"rendering", "pending", "awaiting_device"})
_DEVICE_IN_FLIGHT_PHASES: Final = frozenset({"awaiting_device", "syncing"})
_DEVICE_RECORDS_FIELD: Final = "_device_render_v1"


@dataclass(frozen=True, slots=True)
class SourceReplacement:
    """The thread-state record of the latest take swap."""

    replaced_media_id: str
    media_id: str
    # The Job that was current when the swap committed; None when nothing had rendered yet.
    superseded_job_id: str | None
    revision: int

    def to_state(self) -> dict[str, Any]:
        return {
            "replaced_media_id": self.replaced_media_id,
            "media_id": self.media_id,
            "superseded_job_id": self.superseded_job_id,
            "revision": self.revision,
        }

    @classmethod
    def from_state(cls, state: Mapping[str, Any] | None) -> SourceReplacement | None:
        raw = (state or {}).get(SOURCE_REPLACEMENT_STATE_KEY)
        if not isinstance(raw, Mapping):
            return None
        replaced, media = raw.get("replaced_media_id"), raw.get("media_id")
        if not isinstance(replaced, str) or not isinstance(media, str):
            return None
        job_id = raw.get("superseded_job_id")
        revision = raw.get("revision")
        return cls(
            replaced_media_id=replaced,
            media_id=media,
            superseded_job_id=job_id if isinstance(job_id, str) else None,
            revision=revision if isinstance(revision, int) else 0,
        )


def source_replacement_supersedes(
    state: Mapping[str, Any] | None, current_job_id: uuid.UUID | str | None
) -> bool:
    """True while the item's current Job is the one a take swap superseded.

    It goes false by itself once a fresh strategy claim points the item at a new Job, so no
    code ever has to clear the marker.
    """

    replacement = SourceReplacement.from_state(state)
    if replacement is None or replacement.superseded_job_id is None or current_job_id is None:
        return False
    return replacement.superseded_job_id == str(current_job_id)


def render_in_flight(assembly_plan: Mapping[str, Any] | None) -> bool:
    """A variant is rendering, or a phone export of one is still on its way."""

    plan = assembly_plan or {}
    for variant in plan.get("variants") or []:
        if (
            isinstance(variant, Mapping)
            and variant.get("render_status") in _RENDER_IN_FLIGHT_VARIANT_STATUSES
        ):
            return True
    records = plan.get(_DEVICE_RECORDS_FIELD)
    if isinstance(records, Mapping):
        for record in records.values():
            status = record.get("status") if isinstance(record, Mapping) else None
            if isinstance(status, Mapping) and status.get("phase") in _DEVICE_IN_FLIGHT_PHASES:
                return True
    return False


def rendered_speaker_media_ids(assembly_plan: Mapping[str, Any] | None) -> frozenset[str] | None:
    """The clips the current Job's variants rendered as the speaker, or None if none recorded.

    Speaker choice happens at render time (`generative_build._classify_phone_talking_clips`),
    so the receipt a variant wrote is the only server-side evidence of who spoke.
    """

    from app.kria.protected_targets import PROTECTED_TEXT_FIELD  # noqa: PLC0415
    from app.tasks.generative_build import (  # noqa: PLC0415
        PHONE_TALKING_HEAD_FIELD,
        PHONE_TALKING_MULTI_CLIP_FIELD,
    )

    found: set[str] = set()
    for variant in (assembly_plan or {}).get("variants") or []:
        if not isinstance(variant, Mapping):
            continue
        multi = variant.get(PHONE_TALKING_MULTI_CLIP_FIELD)
        if isinstance(multi, Mapping):
            found.update(str(i) for i in multi.get("speaker_media_ids") or [] if i)
        for field in (PHONE_TALKING_HEAD_FIELD, PROTECTED_TEXT_FIELD):
            receipt = variant.get(field)
            if isinstance(receipt, Mapping) and receipt.get("speaker_media_id"):
                found.add(str(receipt["speaker_media_id"]))
    return frozenset(found) or None


@dataclass(frozen=True, slots=True)
class RetakeRejection:
    status_code: int
    code: str
    message: str


def _video_clips(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in assignments if row.get("kind", "video") == "video"]


def retake_format_supported(
    edit_format: str | None, audio_mode: str | None, voiceover_gcs_path: str | None
) -> bool:
    """The item speaks from an embedded clip: Talking, or a narrated item with no voiceover."""

    fmt = str(edit_format or "").strip().lower()
    voiceover_active = bool(voiceover_gcs_path) and str(audio_mode or "").lower() == "voiceover"
    return fmt == "subtitled" or (fmt in NARRATED_EDIT_FORMATS and not voiceover_active)


def check_retake_target(
    *,
    edit_format: str | None,
    audio_mode: str | None,
    voiceover_gcs_path: str | None,
    assignments: list[dict[str, Any]],
    thread_media: list[dict[str, Any]],
    replaced_media_id: str,
    speaker_media_ids: frozenset[str] | None,
) -> RetakeRejection | None:
    """Refuse a swap that could leave the item with no speaker or the wrong one.

    Mirrors the render contract: a `subtitled` item (or a narrated one with no active
    voiceover) speaks from an embedded clip. A one-clip item has an unambiguous speaker;
    with more clips only a clip the last render used as a speaker may be re-recorded, because
    swapping a cutaway would leave the old speaker AND the new take.
    """

    if not retake_format_supported(edit_format, audio_mode, voiceover_gcs_path):
        return RetakeRejection(
            422,
            "retake_unsupported_format",
            "Only a clip you talk on can be re-recorded.",
        )
    clips = _video_clips(assignments)
    if not any(row.get("media_id") == replaced_media_id for row in clips):
        other = next((m for m in thread_media if m.get("media_id") == replaced_media_id), None)
        names_non_clip = (
            other is not None and (other.get("kind") != "video" or other.get("role") == "song")
        ) or canonical_pool_media_id(replaced_media_id) is not None
        if names_non_clip:
            return RetakeRejection(
                422, "retake_target_not_clip", "Only a video take can be re-recorded."
            )
        return RetakeRejection(409, "retake_target_missing", "That take is no longer attached.")
    if len(clips) > 1:
        if speaker_media_ids is None:
            return RetakeRejection(
                409,
                "retake_speaker_unknown",
                "Make the video first, then re-record the clip you talk on.",
            )
        if replaced_media_id not in speaker_media_ids:
            return RetakeRejection(
                409,
                "retake_target_not_speaker",
                "That clip is not the one you talk on.",
            )
    return None
