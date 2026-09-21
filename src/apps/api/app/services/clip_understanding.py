"""Single read/write path for the shared clip understanding record (KRI-127).

Writers: ``understanding_payload`` builds the block the video analyzer persists
under ``PlanItemAsset.analysis["understanding"]``.

Readers: ``clip_record`` projects ANY stored analysis (new, legacy video v<=7,
image) into one ``ClipUnderstanding``. The chat agent, the planner and the clip
matchers must read clips through it so they can never drift apart again.
"""

from __future__ import annotations

from typing import Any

from app.schemas.clip_understanding import (
    UNDERSTANDING_KEY,
    ClipMomentNote,
    ClipPeople,
    ClipSpeech,
    ClipUnderstanding,
)


def _moment_notes(raw: object) -> list[ClipMomentNote]:
    notes: list[ClipMomentNote] = []
    if not isinstance(raw, list):
        return notes
    for m in raw:
        if not isinstance(m, dict):
            continue
        try:
            start_s = float(m.get("start_s", 0.0) or 0.0)
            end_s = float(m.get("end_s", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        notes.append(
            ClipMomentNote(start_s=start_s, end_s=end_s, description=m.get("description") or "")
        )
    return notes


def understanding_payload(meta: Any, *, best_moments: list[dict] | None = None) -> dict[str, Any]:
    """Build the persisted ``understanding`` block from an analyzer ``ClipMeta``.

    ``getattr``-tolerant so a degraded/legacy meta still yields a valid block.
    """
    transcript = str(getattr(meta, "transcript", "") or "")
    speaks_to_camera = bool(getattr(meta, "speaks_to_camera", False))
    record = ClipUnderstanding(
        kind="video",
        subject=getattr(meta, "detected_subject", "") or "",
        summary=getattr(meta, "summary", "") or "",
        setting=getattr(meta, "setting", "") or "",
        activity=getattr(meta, "activity", "") or "",
        people=ClipPeople(
            count=getattr(meta, "people_count", None),
            speaks_to_camera=speaks_to_camera,
            note=getattr(meta, "people_note", "") or "",
        ),
        speech=ClipSpeech(
            has_speech=bool(transcript.strip()),
            to_camera=speaks_to_camera and bool(transcript.strip()),
            transcript=transcript,
        ),
        brands=list(getattr(meta, "brands", None) or []),
        # ClipMeta names these `clip_*` on purpose: talking_head_assembler reads
        # `content_type`/`audio_type` via getattr and must keep seeing defaults.
        content_type=getattr(meta, "clip_content_type", "") or "",
        audio_type=getattr(meta, "clip_audio_type", "") or "",
        notable_moments=_moment_notes(best_moments or []),
    )
    return record.model_dump()


def clip_record(analysis: dict[str, Any] | None, *, kind: str = "video") -> ClipUnderstanding:
    """Project a stored analysis dict into the shared record. Never raises."""
    media_kind = "image" if kind == "image" else "video"
    if not isinstance(analysis, dict) or not analysis:
        return ClipUnderstanding(kind=media_kind)

    block = analysis.get(UNDERSTANDING_KEY)
    if isinstance(block, dict) and block:
        merged = {**block, "kind": media_kind}
        if not merged.get("subject"):
            merged["subject"] = analysis.get("subject") or ""
        if not merged.get("summary"):
            merged["summary"] = analysis.get("description") or ""
        try:
            return ClipUnderstanding.model_validate(merged)
        except ValueError:
            pass  # malformed block: fall through to the legacy projection

    # Legacy shapes. For videos written before the shared record existed,
    # `on_screen_text` held the SPOKEN transcript (autoplace._analyze_video);
    # for images it really is on-screen text.
    is_video_analysis = media_kind == "video" and analysis.get("source") != "image_metadata"
    transcript = analysis.get("transcript") or (
        analysis.get("on_screen_text") if is_video_analysis else ""
    )
    return ClipUnderstanding(
        kind=media_kind,
        subject=analysis.get("subject") or "",
        summary=analysis.get("description") or "",
        speech=ClipSpeech(has_speech=bool(transcript), transcript=transcript or ""),
        on_screen_text="" if is_video_analysis else (analysis.get("on_screen_text") or ""),
        brands=analysis.get("brands") or [],
        notable_moments=_moment_notes(analysis.get("best_moments")),
    )
