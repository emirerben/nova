"""Structured evidence a cloud renderer emits about what it actually produced.

KRI-470 / PR-E.  A creator's approved requirements (order, exact text, audio,
narration) are intent.  The cloud verifier (``services/cloud_render_contract``)
only accepts evidence derived from the render itself: the timeline the renderer
built, the audio graph it mixed and the text layers it burned -- never a copy of
the contract or a plan field.

These models are the shared vocabulary of the guided-story receipt
(``pipeline/guided_story.GuidedStoryRenderReceipt``) and the classic renderers'
receipt.  Every field is optional and ``None`` means "this renderer did not
produce that evidence", which the verifier reports as ``evidence_missing``;
``[]`` means "produced, and there was nothing".  The helpers here are pure so
they can be unit-checked without ffmpeg.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EVIDENCE_SCHEMA_VERSION = 1

TextRole = Literal["opening", "closing", "clip", "any"]
SourceAudioState = Literal["audible", "muted"]

# A text layer's start/end come from the renderer's own clock; allow a few
# frames of rounding when comparing them with picture windows.
TEXT_WINDOW_TOLERANCE_S = 0.1

# Keys the evidence adds to a render receipt (all optional, omitted when None
# so receipts written before this PR keep their exact stored shape).
EVIDENCE_RECEIPT_KEYS = (
    "actual_clip_order",
    "picture_timeline",
    "source_audio_ids",
    "source_audio_state",
    "source_audio_reason",
    "text_evidence",
)


class CloudPictureSegment(BaseModel):
    """One contiguous stretch of the output that shows one source."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    media_id: str = Field(min_length=1)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)


class CloudTextEvidence(BaseModel):
    """One text layer the renderer burned, as measured on its own output."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    role: TextRole
    text: str = Field(min_length=1)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    media_id: str | None = None


def normalize_text(value: object) -> str:
    """The comparison form of on-screen text (wrapping is layout, not words)."""

    return " ".join(unicodedata.normalize("NFC", str(value)).split())


def collapse_adjacent(ids: Iterable[str]) -> list[str]:
    """The order sources first appear in, merging immediate repeats only.

    A clip shown twice in a row is one entry; the same clip returning later is
    a second entry, so a repeat can never masquerade as the approved order.
    """

    out: list[str] = []
    for item in ids:
        if not out or out[-1] != item:
            out.append(item)
    return out


def picture_timeline(
    segments: Iterable[tuple[str, float, float]],
) -> list[dict[str, Any]]:
    """Validated picture segments as plain dicts (rounded to the millisecond)."""

    return [
        CloudPictureSegment(
            media_id=media_id, start_s=round(float(start), 3), end_s=round(float(end), 3)
        ).model_dump(mode="json")
        for media_id, start, end in segments
    ]


def audio_evidence(
    audible_media_ids: Sequence[str], *, reason: str | None = None
) -> dict[str, Any]:
    """The camera-audio half of the evidence.

    ``audible_media_ids`` are the sources whose own sound is mixed into the
    output; empty means muted, and ``reason`` records why (narration or music
    replaced it, the creator muted it, no source had sound).
    """

    ids = list(dict.fromkeys(audible_media_ids))
    return {
        "source_audio_ids": ids,
        "source_audio_state": "audible" if ids else "muted",
        **({"source_audio_reason": reason} if reason else {}),
    }


def text_evidence_row(
    *,
    role: TextRole,
    text: str,
    start_s: float,
    end_s: float,
    media_id: str | None = None,
) -> dict[str, Any]:
    return CloudTextEvidence(
        role=role,
        text=text,
        start_s=round(float(start_s), 3),
        end_s=round(float(end_s), 3),
        media_id=media_id,
    ).model_dump(mode="json", exclude_none=True)


def media_ids_by_gcs_path(assembly: Mapping[str, Any]) -> dict[str, str]:
    """``gcs_path -> media_id`` from the approved media snapshot, if present.

    The classic renderers see storage paths; the contract speaks media ids.  The
    snapshot bound at approval is the only authority that links the two, so an
    unmapped path yields no entry and the evidence for it stays absent.
    """

    binding = assembly.get("creator_brief_binding")
    snapshot = binding.get("media_snapshot") if isinstance(binding, Mapping) else None
    rows = snapshot.get("clip_assignments") if isinstance(snapshot, Mapping) else None
    out: dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        path = row.get("gcs_path") or row.get("storage_path")
        media_id = row.get("media_id")
        if isinstance(path, str) and path and isinstance(media_id, str) and media_id:
            out.setdefault(path, media_id)
    return out


def classic_render_receipt(
    *,
    actual_duration_s: float,
    narration_applied: bool,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """The receipt a classic cloud renderer persists as ``render_receipt``.

    ``verified`` here means only that the renderer measured its own output and
    is reporting what it found; the contract verifier decides what that proves.
    """

    receipt: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "adapter": "cloud_classic",
        "verified": True,
        "actual_duration_s": round(float(actual_duration_s), 3),
        "narration_applied": bool(narration_applied),
    }
    for key in EVIDENCE_RECEIPT_KEYS:
        if evidence.get(key) is not None:
            receipt[key] = evidence[key]
    return receipt


def classic_slot_evidence(
    slots: Iterable[tuple[str, float]],
    *,
    clip_id_to_gcs: Mapping[str, str],
    clip_id_to_local: Mapping[str, str],
    probe_map: Mapping[str, Any],
    media_ids_by_gcs: Mapping[str, str],
) -> dict[str, Any]:
    """Picture order and per-slot camera audio of an assembled classic cut.

    ``slots`` are ``(clip_id, rendered_duration_s)`` in output order, taken from
    the post-resolution plans the assembler actually rendered.  Everything is
    keyed to approved media ids; one slot that cannot be attributed (a spliced
    synthetic clip, an unmapped path) makes the whole picture evidence absent
    rather than partial.  ``slot_audio_media_ids`` lists the used sources whose
    probe shows an audio stream (``None`` when any probe is unknown).
    """

    empty: dict[str, Any] = {
        "picture_timeline": None,
        "actual_clip_order": None,
        "slot_audio_media_ids": None,
    }
    cursor = 0.0
    segments: list[tuple[str, float, float]] = []
    with_audio: list[str] = []
    audio_known = True
    for clip_id, duration_s in slots:
        gcs = clip_id_to_gcs.get(clip_id)
        media_id = media_ids_by_gcs.get(gcs) if gcs else None
        if media_id is None or duration_s <= 0:
            return empty
        segments.append((media_id, cursor, cursor + duration_s))
        cursor += duration_s
        has_audio = getattr(probe_map.get(clip_id_to_local.get(clip_id, "")), "has_audio", None)
        if has_audio is None:
            audio_known = False
        elif has_audio:
            with_audio.append(media_id)
    if not segments:
        return empty
    return {
        "picture_timeline": picture_timeline(segments),
        "actual_clip_order": collapse_adjacent(media_id for media_id, _s, _e in segments),
        "slot_audio_media_ids": list(dict.fromkeys(with_audio)) if audio_known else None,
    }
