"""Saved zero-clip edits, separate from the last nonempty render program.

CreatorEditDraft owns the immutable editor snapshot. A small variant reference
makes the saved draft visible to every editor read and fences obsolete render
work without ever asking a renderer to compile an empty program.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import replace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kria.api_schemas import DraftSnapshotOut
from app.kria.drafts import (
    KriaDraftDocument,
    _editor_snapshot,
    _out,
    editor_variant_target,
    stage_editor_variant_draft,
)
from app.models import CreatorEditDraft, Job, PlanItem
from app.services.phone_editor import _StagedJob

_DIRECT_SECTIONS = (
    "text_elements",
    "caption_cues",
    "caption_meta",
    "captions_enabled",
    "caption_style",
    "caption_font",
    "caption_size_px",
    "caption_color",
    "caption_text_color",
    "caption_highlight_color",
    "caption_stroke_width",
    "caption_shadow_enabled",
    "caption_editor_style",
    "caption_margin_v",
    "caption_y_frac",
    "voiceover_caption_style",
    "voiceover_caption_font",
    "voiceover_bed_level",
    "audio_mix",
    "music_track_id",
    "music_window",
    "music_start_s",
    "background_music",
    "background_music_treatment",
    "lyrics",
    "lyrics_enabled",
    "lyric_line_overrides",
    "lyric_line_suppressions",
    "mix",
    "original_audio_level",
    "smart_music_treatment",
    "orientation",
    "sound_effects",
    "media_overlays",
    "visual_blocks",
    "motion_scenes",
    "motion_runtime_hash",
    "camera_effects",
    "custom_effects",
    "narration_label_text_elements",
    "narration_label_receipt",
    "carousel_moment",
    "text_elements_user_edited",
    "text_elements_materialized_from",
    "geometry_materialized_at_version",
    "ai_text_tombstones",
)


def is_empty_editor_variant(variant: dict[str, Any]) -> bool:
    return variant.get("editor_state") == "empty"


def _variant(job: Any, variant_id: str) -> dict[str, Any]:
    from app.routes.generative_jobs import _find_variant  # noqa: PLC0415

    variant = _find_variant(job, variant_id)
    if variant is None:
        raise HTTPException(404, detail="Variant not found")
    return variant


def _reference(variant: dict[str, Any]) -> uuid.UUID | None:
    reference = variant.get("editor_draft")
    if not is_empty_editor_variant(variant) or not isinstance(reference, dict):
        return None
    try:
        return uuid.UUID(str(reference.get("draft_id")))
    except (ValueError, TypeError, AttributeError):
        return None


async def attach_saved_editor_drafts(db: AsyncSession, job: Job | None) -> None:
    """Load pinned snapshots once; never overwrite ORM variant JSON on a read."""
    if job is None:
        return
    variants = [v for v in (job.assembly_plan or {}).get("variants") or [] if isinstance(v, dict)]
    references = {_reference(v): v for v in variants if _reference(v) is not None}
    if not references:
        return
    rows = (
        (
            await db.execute(
                select(CreatorEditDraft).where(
                    CreatorEditDraft.id.in_(references),
                    CreatorEditDraft.creator_id == job.user_id,
                    CreatorEditDraft.item_id == job.content_plan_item_id,
                    CreatorEditDraft.base_job_id == job.id,
                )
            )
        )
        .scalars()
        .all()
    )
    snapshots = {}
    for row in rows:
        variant = references.get(row.id)
        if (
            variant is None
            or str(row.variant_key) != str(variant.get("variant_id"))
            or row.base_generation_id != variant.get("render_generation_id")
            or row.snapshot_json is None
        ):
            continue
        snapshots[str(row.variant_key)] = _out(row, can_undo=row.parent_draft_id is not None)
    job._saved_editor_drafts = snapshots


def saved_editor_draft(job: Job, variant_id: str) -> DraftSnapshotOut:
    draft = getattr(job, "_saved_editor_drafts", {}).get(variant_id)
    if draft is None:
        raise HTTPException(409, detail="editor_draft_unavailable")
    return draft


def public_empty_editor_variant(job: Job, variant: dict[str, Any]) -> dict[str, Any]:
    """An empty draft must never masquerade as the last-good rendered video."""
    if not is_empty_editor_variant(variant):
        return variant
    result = dict(variant)
    draft = getattr(job, "_saved_editor_drafts", {}).get(str(variant.get("variant_id")))
    result["editor_draft"] = draft.model_dump(mode="json") if draft else None
    result["editor_state"] = "empty"
    result["render_status"] = "draft"
    for key in (
        "output_url",
        "download_url",
        "base_video_url",
        "preview_url",
        "thumbnail_url",
        "poster_url",
        "base_poster_url",
        "video_path",
        "base_video_path",
        "poster_path",
        "base_poster_path",
    ):
        result[key] = None
    result["duration_s"] = 0.0
    result["playback_available"] = False
    result["export_available"] = False
    if draft:
        sections = (draft.snapshot.get("editor_payload") or {}).get("sections") or {}
        for key in _DIRECT_SECTIONS:
            if key in sections:
                result[key] = copy.deepcopy(sections[key])
        result["user_timeline"] = {"slots": [], "total_duration_s": 0.0}
    return result


def stage_saved_draft_baseline(job: Job, variant_id: str, body: Any) -> _StagedJob:
    """Validate the current saved baseline before overlaying its editor lanes."""
    from app.routes.generative_jobs import variant_render_baseline  # noqa: PLC0415

    variant = _variant(job, variant_id)
    if body.editor_state_version != 1:
        raise HTTPException(409, detail="editor_draft_requires_supported_client")
    if body.base_generation != variant_render_baseline(variant):
        raise HTTPException(409, detail="baseline_conflict")
    draft = saved_editor_draft(job, variant_id)
    sections = (draft.snapshot.get("editor_payload") or {}).get("sections") or {}
    staged = _StagedJob(job)
    effective = _variant(staged, variant_id)
    for key in _DIRECT_SECTIONS:
        if key in sections:
            effective[key] = copy.deepcopy(sections[key])
    if "timeline_slots" in sections:
        # The saved snapshot is the authoritative baseline for a second
        # delete; the public empty projection intentionally exposes no clips.
        effective["user_timeline"] = {"slots": copy.deepcopy(sections["timeline_slots"])}
    # Retain the last nonempty render recipe solely as a source/identity
    # baseline. Callers must explicitly provide a new timeline to render.
    effective.pop("editor_state", None)
    effective.pop("editor_draft", None)
    effective["editor_timeline_mode"] = "authored"
    return staged


def stage_initial_authored_baseline(
    job: Job, variant_id: str, timeline_slots: list[dict[str, Any]]
) -> _StagedJob:
    """Turn a server-owned legacy cut into a staged creator timeline.

    This is used only for an explicit deletion request.  It preserves all
    effective layers while giving the authored compiler the canonical source
    rows that the editor displayed (including native composite receipts).
    """
    # Resolve server-projected guided/phone lanes before the authored marker
    # disables those legacy projections. The staged copy then owns every lane.
    sections = editor_sections(job, variant_id)
    staged = _StagedJob(job)
    effective = _variant(staged, variant_id)
    for key in _DIRECT_SECTIONS:
        if key in sections:
            effective[key] = copy.deepcopy(sections[key])
    effective["user_timeline"] = {"slots": copy.deepcopy(timeline_slots)}
    effective["editor_timeline_mode"] = "authored"
    return staged


def editor_sections(job: Job, variant_id: str) -> dict[str, Any]:
    from app.routes.generative_jobs import (  # noqa: PLC0415
        _guided_text_state_for_response,
        _guided_v2_revision,
        project_phone_subtitled_editor_sections,
        variant_render_baseline,
    )

    variant = _variant(job, variant_id)
    payload = _editor_snapshot(variant, variant_render_baseline(variant))
    sections = copy.deepcopy(payload["sections"])
    for key in _DIRECT_SECTIONS:
        if key in variant:
            sections[key] = copy.deepcopy(variant[key])
    if (
        variant.get("render_destination") == "device"
        and variant.get("editor_timeline_mode") != "authored"
    ):
        phone_sections = project_phone_subtitled_editor_sections(job.assembly_plan or {}, variant)
        for key, value in (phone_sections or {}).items():
            if key not in variant:
                sections[key] = copy.deepcopy(value)
    # Snapshot the effective editor lane, not the sparse legacy render dict.
    # Sequence/intro and baked lyric rows are lazily projected on reads; without
    # materializing them here they would disappear when an empty draft reopens.
    # Do this after the direct-copy loop so raw legacy `text_elements` cannot
    # overwrite the effective lane. Explicit user-cleared [] remains [] through
    # merge_projected_text_elements_for_variant's user-edited/tombstone path.
    from app.agents._schemas.text_element import (  # noqa: PLC0415
        merge_projected_text_elements_for_variant,
    )

    sections["text_elements"] = (
        merge_projected_text_elements_for_variant(variant, include_lyric_projection=True) or []
    )
    for key in (
        "text_elements_user_edited",
        "text_elements_materialized_from",
        "geometry_materialized_at_version",
        "ai_text_tombstones",
    ):
        if key in variant:
            sections[key] = copy.deepcopy(variant[key])
    revision = (
        _guided_v2_revision(job, variant)
        if variant.get("editor_timeline_mode") != "authored"
        and variant.get("resolved_archetype") == "guided_story"
        else None
    )
    if revision:
        # The revision is authoritative even when the guided editor rollout is
        # disabled. Supply its read projection explicitly so approved labels
        # and pinned caption presentation survive the mode switch.
        guided_text = _guided_text_state_for_response(
            job, {**variant, "guided_edit_revision": revision}
        )
        for key in (
            "sound_effects",
            "media_overlays",
            "visual_blocks",
            "motion_scenes",
            "custom_effects",
            "caption_meta",
            "orientation",
        ):
            if key in revision:
                sections[key] = copy.deepcopy(revision[key])
        if guided_text is not None:
            text, labels, receipt = guided_text
            sections["text_elements"] = copy.deepcopy(text)
            sections["narration_label_text_elements"] = copy.deepcopy(labels)
            sections["narration_label_receipt"] = copy.deepcopy(receipt)
        else:
            sections["text_elements"] = copy.deepcopy(revision.get("text_elements") or [])
        sections["text_elements_user_edited"] = True
        caption_meta = revision.get("caption_meta") or {}
        if "style" in caption_meta:
            sections["voiceover_caption_style"] = caption_meta["style"]
        if "y_frac" in caption_meta:
            sections["caption_y_frac"] = caption_meta["y_frac"]
        audio = revision.get("audio") or {}
        if audio.get("mode") == "track":
            sections["music_track_id"] = audio["track_id"]
            sections["music_start_s"] = audio.get("start_s", 0)
            sections["music_window"] = {
                "start_s": audio.get("start_s", 0),
                "end_s": audio.get("end_s"),
            }
            sections["mix"] = audio.get("level", 1)
        elif audio.get("mode") == "none":
            sections["music_track_id"] = None
            sections["music_window"] = None
            sections["music_start_s"] = 0
        sections["revision_number"] = revision.get("revision_number")
        sections["revision_hash"] = revision.get("state_hash")
    return sections


def prepare_empty_editor_sections(
    job: Job, variant_id: str, body: Any, *, validation_arguments: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, bool]]:
    """Validate every changed non-timeline lane using the normal Save path.

    The staging copy retains the last valid visual program for validation of
    timed layers. No mutation or render from that copy is published. Empty
    timelines never enter the nonempty guided/phone render schemas.
    """
    from app.routes.generative_jobs import (  # noqa: PLC0415
        EditorCommitSections,
        _prepare_editor_commit,
    )

    staged = _StagedJob(job)
    validation_body = body.model_copy(update={"timeline_slots": None})
    relevant = body.model_dump(exclude_none=True, exclude_defaults=True)
    for key in (
        "base_generation",
        "editor_state_version",
        "deletions",
        "timeline_slots",
        "guided_revision_number",
        "copilot_receipt_ids",
        "accepted_suggestion_ids",
    ):
        relevant.pop(key, None)
    flags = {key: False for key in EditorCommitSections.model_fields}
    if relevant or getattr(body, "_lyric_line_suppressions", None) is not None:
        prep = _prepare_editor_commit(staged, variant_id, validation_body, **validation_arguments)
        flags.update(prep["sections"])
    sections = editor_sections(staged, variant_id)
    sections["timeline_slots"] = []
    # Explicit null and empty lists are meaningful; retain them through the
    # full snapshot instead of allowing read-time fallbacks to materialize.
    if body.remove_music:
        sections["music_track_id"] = None
        sections["music_window"] = None
    if "carousel_moment" in body.model_fields_set and body.carousel_moment is None:
        sections["carousel_moment"] = None
    flags["timeline"] = body.timeline_slots is not None
    return sections, flags


async def stage_empty_editor_commit(
    db: AsyncSession,
    *,
    item: PlanItem,
    job: Job,
    variant_id: str,
    creator_id: uuid.UUID,
    sections: dict[str, Any],
    section_flags: dict[str, bool],
) -> tuple[dict[str, Any], DraftSnapshotOut]:
    """Stage a new immutable draft and generation fence in one DB transaction."""
    generation = uuid.uuid4().hex
    target = await editor_variant_target(
        db, item=item, job=job, creator_id=creator_id, variant_key=variant_id
    )
    target = replace(target, generation_id=generation)
    document = KriaDraftDocument(
        kind="editor",
        edit_format=item.edit_format or "montage",
        editor_payload={
            "base_generation": generation,
            "editor_state": "empty",
            "sections": sections,
        },
        changes=["Saved an empty timeline"],
    )
    draft = await stage_editor_variant_draft(db, target=target, document=document)
    assembly = copy.deepcopy(job.assembly_plan or {})
    variant = next(v for v in assembly["variants"] if v.get("variant_id") == variant_id)
    variant.update(
        editor_state="empty",
        editor_draft={"draft_id": draft.draft_id},
        render_generation_id=generation,
        render_status="draft",
        ok=True,
    )
    # A late render/device completion must not complete the saved draft.
    for key in (
        "render_task_id",
        "render_started_at",
        "editor_render_attempt",
        "error",
        "error_class",
    ):
        variant.pop(key, None)
    job.assembly_plan = assembly
    # Removing a device-bound or cloud variant must not leave the parent job
    # looking in-flight after its only active render became a local draft.
    statuses = [
        row.get("render_status") for row in assembly.get("variants") or [] if isinstance(row, dict)
    ]
    terminal = {"ready", "draft", "failed"}
    if statuses and all(value in terminal for value in statuses):
        if all(value == "failed" for value in statuses):
            job.status = "variants_failed"
        elif any(value == "failed" for value in statuses):
            job.status = "variants_ready_partial"
        else:
            job.status = "variants_ready"
    return {
        "generation": generation,
        "sections": section_flags,
        "expected_duration_s": 0.0,
        "has_render_section": False,
        "editor_state": "empty",
    }, draft
