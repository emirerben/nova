"""Authoritative immutable draft revisions for Kria runtime v2.

Draft bodies are full, bounded snapshots.  Writers serialize on the owning
PlanItem, validate the exact current Job generation, clear the previous head,
and insert a new head.  Nothing in this module renders or talks to a broker.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kria.api_schemas import DraftSnapshotOut
from app.kria.runtime import RuntimeFailure, _append_event, _owned_thread, _promote_queued_successor
from app.models import (
    ContentPlan,
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    PlanItem,
)

MAX_DRAFT_BYTES = 2 * 1024 * 1024


class KriaDraftDocument(BaseModel):
    """Wire-safe draft body shared by agent and editor clients."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=2, ge=2, le=2)
    kind: Literal["initial", "editor", "strategy"]
    intent: str = Field(default="", max_length=4000)
    edit_format: str = Field(default="montage", min_length=1, max_length=80)
    editor_payload: dict[str, Any] | None = None
    strategy: dict[str, Any] | None = None
    changes: list[str] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> KriaDraftDocument:
        if self.kind == "editor" and self.editor_payload is None:
            raise ValueError("editor drafts require editor_payload")
        if self.kind != "editor" and self.editor_payload is not None:
            raise ValueError("only editor drafts can contain editor_payload")
        if self.kind == "strategy" and self.strategy is None:
            raise ValueError("strategy drafts require strategy")
        if self.kind != "strategy" and self.strategy is not None:
            raise ValueError("only strategy drafts can contain strategy")
        return self


@dataclass(frozen=True)
class DraftTarget:
    thread: CreationThread
    item: PlanItem
    job: Job | None
    variant_key: str
    generation_id: str | None


def canonical_snapshot(document: KriaDraftDocument) -> tuple[dict[str, Any], str]:
    snapshot = document.model_dump(mode="json")
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_DRAFT_BYTES:
        raise RuntimeFailure(
            422,
            "draft_too_large",
            "This edit is too large to save as one draft.",
            phase="tool",
            recovery="ask_user",
        )
    return snapshot, hashlib.sha256(encoded).hexdigest()


def draft_etag(snapshot_hash: str) -> str:
    return f'"{snapshot_hash}"'


def _variant(job: Job, variant_key: str) -> dict[str, Any] | None:
    return next(
        (
            value
            for value in (job.assembly_plan or {}).get("variants") or []
            if isinstance(value, dict) and str(value.get("variant_id") or "") == variant_key
        ),
        None,
    )


async def _target(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    lock_item: bool,
) -> DraftTarget:
    thread = await _owned_thread(
        db,
        thread_id=thread_id,
        creator_id=creator_id,
        lock=False,
        require_active=False,
    )
    if thread.active_plan_item_id is None:
        raise RuntimeFailure(
            409,
            "draft_target_missing",
            "This project does not have an editable video yet.",
            phase="tool",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )
    statement = (
        select(PlanItem)
        .join(ContentPlan, ContentPlan.id == PlanItem.content_plan_id)
        .where(
            PlanItem.id == thread.active_plan_item_id,
            ContentPlan.user_id == creator_id,
        )
    )
    if lock_item:
        statement = statement.with_for_update()
    item = (await db.execute(statement)).scalar_one_or_none()
    if item is None:
        raise RuntimeFailure(404, "draft_target_missing", "Editable video not found", phase="tool")
    job: Job | None = None
    generation_id: str | None = None
    variant_key = "initial"
    current_job_id = item.current_job_id
    if current_job_id is not None:
        job = (
            await db.execute(
                select(Job).where(
                    Job.id == current_job_id,
                    Job.user_id == creator_id,
                    Job.content_plan_item_id == item.id,
                )
            )
        ).scalar_one_or_none()
        if job is None:
            raise RuntimeFailure(
                409,
                "draft_target_stale",
                "The current render target changed.",
                phase="tool",
                recovery="refresh_replan",
                current_revision=int(thread.revision),
            )
        state = dict(thread.state or {})
        selected_variant_id = state.get("selected_variant_id")
        preferred_candidates: list[str] = []
        if isinstance(selected_variant_id, str) and selected_variant_id.strip():
            preferred_candidates.append(selected_variant_id.strip())
        session = (
            await db.get(CreatorAgentSession, thread.active_creator_agent_session_id)
            if thread.active_creator_agent_session_id is not None
            else None
        )
        if (
            session is not None
            and session.target_job_id == job.id
            and isinstance(session.target_variant_id, str)
            and session.target_variant_id not in preferred_candidates
        ):
            preferred_candidates.append(session.target_variant_id)
        variants = [
            value
            for value in (job.assembly_plan or {}).get("variants") or []
            if isinstance(value, dict) and value.get("variant_id")
        ]
        selected = next(
            (
                value
                for preferred in preferred_candidates
                for value in variants
                if str(value.get("variant_id")) == preferred
            ),
            variants[0] if len(variants) == 1 else None,
        )
        if selected is None:
            raise RuntimeFailure(
                409,
                "draft_variant_ambiguous",
                "Choose which video version to edit before continuing.",
                phase="tool",
                recovery="ask_user",
                current_revision=int(thread.revision),
            )
        variant_key = str(selected["variant_id"])
        generation_id = str(selected.get("render_generation_id") or "") or None
    return DraftTarget(
        thread=thread,
        item=item,
        job=job,
        variant_key=variant_key,
        generation_id=generation_id,
    )


async def _head(
    db: AsyncSession, *, item_id: uuid.UUID, variant_key: str, lock: bool
) -> CreatorEditDraft | None:
    statement = select(CreatorEditDraft).where(
        CreatorEditDraft.item_id == item_id,
        CreatorEditDraft.variant_key == variant_key,
        CreatorEditDraft.is_head.is_(True),
    )
    if lock:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


async def _insert_revision(
    db: AsyncSession,
    *,
    target: DraftTarget,
    document: KriaDraftDocument,
    parent: CreatorEditDraft | None,
) -> CreatorEditDraft:
    snapshot, snapshot_hash = canonical_snapshot(document)
    latest_revision = int(
        (
            await db.execute(
                select(func.coalesce(func.max(CreatorEditDraft.draft_revision), -1)).where(
                    CreatorEditDraft.item_id == target.item.id,
                    CreatorEditDraft.variant_key == target.variant_key,
                )
            )
        ).scalar_one()
    )
    if parent is not None:
        parent.is_head = False
    row = CreatorEditDraft(
        creator_id=target.thread.creator_id,
        thread_id=target.thread.id,
        item_id=target.item.id,
        variant_key=target.variant_key,
        base_job_id=target.job.id if target.job is not None else None,
        base_generation_id=target.generation_id,
        draft_revision=latest_revision + 1,
        parent_draft_id=parent.id if parent is not None else None,
        snapshot_json=snapshot,
        snapshot_hash=snapshot_hash,
        is_head=True,
    )
    db.add(row)
    await db.flush()
    return row


def _out(row: CreatorEditDraft, *, can_undo: bool) -> DraftSnapshotOut:
    if row.snapshot_json is None:
        raise RuntimeFailure(
            410,
            "draft_pruned",
            "This old draft body is no longer available.",
            phase="tool",
            recovery="refresh_replan",
        )
    return DraftSnapshotOut(
        draft_id=str(row.id),
        item_id=str(row.item_id),
        variant_key=row.variant_key,
        draft_revision=row.draft_revision,
        snapshot_hash=row.snapshot_hash,
        etag=draft_etag(row.snapshot_hash),
        base_job_id=str(row.base_job_id) if row.base_job_id else None,
        base_generation_id=row.base_generation_id,
        snapshot=dict(row.snapshot_json),
        can_undo=can_undo,
        created_at=row.created_at,
    )


def _editor_snapshot(variant: dict[str, Any], generation_id: str | None) -> dict[str, Any]:
    """Project only editor-owned state; omit media URLs and renderer internals."""

    keys = (
        "text_elements",
        "caption_cues",
        "captions_enabled",
        "caption_style",
        "caption_font",
        "caption_y_frac",
        "caption_size_px",
        "caption_color",
        "caption_highlight_color",
        "caption_stroke_width",
        "audio_mix",
        "music_track_id",
        "music_window",
        "background_music",
        "lyrics",
        "orientation",
        "sound_effects",
        "media_overlays",
        "visual_blocks",
        "motion_scenes",
        "camera_effects",
        "carousel_moment",
    )
    sections = {key: variant[key] for key in keys if key in variant}
    timeline = variant.get("user_timeline") or variant.get("ai_timeline")
    if isinstance(timeline, dict) and isinstance(timeline.get("slots"), list):
        sections["timeline_slots"] = timeline["slots"]
    return {"base_generation": generation_id or "", "sections": sections}


async def read_or_bootstrap_draft(
    db: AsyncSession, *, thread_id: uuid.UUID, creator_id: uuid.UUID
) -> DraftSnapshotOut:
    target = await _target(db, thread_id=thread_id, creator_id=creator_id, lock_item=True)
    head = await _head(db, item_id=target.item.id, variant_key=target.variant_key, lock=True)
    if head is None:
        state = dict(target.thread.state or {})
        document = KriaDraftDocument(
            kind="initial" if target.job is None else "editor",
            intent=str(state.get("intent") or "")[:4000],
            edit_format=str(target.item.edit_format or state.get("edit_format") or "montage"),
            editor_payload=(
                _editor_snapshot(
                    dict(_variant(target.job, target.variant_key) or {}), target.generation_id
                )
                if target.job is not None
                else None
            ),
            changes=[],
        )
        head = await _insert_revision(db, target=target, document=document, parent=None)
        await db.commit()
    return _out(head, can_undo=head.parent_draft_id is not None)


async def write_draft(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    expected_revision: int,
    expected_etag: str,
    snapshot: dict[str, Any],
) -> DraftSnapshotOut:
    target = await _target(db, thread_id=thread_id, creator_id=creator_id, lock_item=True)
    head = await _head(db, item_id=target.item.id, variant_key=target.variant_key, lock=True)
    if head is None:
        raise RuntimeFailure(
            409,
            "draft_missing",
            "Refresh the project before saving this edit.",
            phase="tool",
            recovery="refresh_replan",
        )
    if head.draft_revision != expected_revision or draft_etag(head.snapshot_hash) != expected_etag:
        raise RuntimeFailure(
            409,
            "draft_stale",
            "A newer edit is already saved. Refresh before applying yours.",
            phase="tool",
            recovery="refresh_replan",
            current_revision=int(target.thread.revision),
        )
    if head.base_job_id != (target.job.id if target.job is not None else None) or (
        head.base_generation_id != target.generation_id
    ):
        raise RuntimeFailure(
            409,
            "draft_base_stale",
            "The rendered video changed while this draft was open.",
            phase="tool",
            recovery="refresh_replan",
            current_revision=int(target.thread.revision),
        )
    document = KriaDraftDocument.model_validate(snapshot)
    row = await _insert_revision(db, target=target, document=document, parent=head)
    await db.commit()
    return _out(row, can_undo=True)


async def undo_draft(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    expected_revision: int,
) -> tuple[DraftSnapshotOut, str | None]:
    target = await _target(db, thread_id=thread_id, creator_id=creator_id, lock_item=True)
    pending_refs = list(
        (
            await db.execute(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.thread_id == target.thread.id,
                    CreatorAgentApproval.status == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    turn_ids = sorted({approval.turn_id for approval in pending_refs}, key=str)
    turns = {
        turn.id: turn
        for turn in (
            (
                await db.execute(
                    select(CreatorAgentTurn)
                    .where(CreatorAgentTurn.id.in_(turn_ids))
                    .order_by(CreatorAgentTurn.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
            if turn_ids
            else []
        )
    }
    head = await _head(db, item_id=target.item.id, variant_key=target.variant_key, lock=True)
    if head is None or head.draft_revision != expected_revision:
        raise RuntimeFailure(
            409,
            "draft_stale",
            "A newer edit is already saved. Refresh before undoing.",
            phase="tool",
            recovery="refresh_replan",
            current_revision=int(target.thread.revision),
        )
    if head.parent_draft_id is None:
        raise RuntimeFailure(
            409,
            "draft_undo_unavailable",
            "There is no earlier draft to restore.",
            phase="tool",
            recovery="none",
            current_revision=int(target.thread.revision),
        )
    parent = (
        await db.execute(
            select(CreatorEditDraft).where(CreatorEditDraft.id == head.parent_draft_id)
        )
    ).scalar_one_or_none()
    if parent is None or parent.snapshot_json is None:
        raise RuntimeFailure(
            409,
            "draft_undo_unavailable",
            "The earlier draft is no longer available.",
            phase="tool",
            recovery="refresh_replan",
        )
    document = KriaDraftDocument.model_validate(parent.snapshot_json)
    row = await _insert_revision(db, target=target, document=document, parent=head)
    approvals = list(
        (
            await db.execute(
                select(CreatorAgentApproval)
                .where(
                    CreatorAgentApproval.thread_id == target.thread.id,
                    CreatorAgentApproval.draft_id == head.id,
                    CreatorAgentApproval.status == "pending",
                )
                .order_by(CreatorAgentApproval.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    execution_ids = sorted(
        {
            uuid.UUID(str(execution_id))
            for approval in approvals
            for execution_id in approval.execution_ids
        },
        key=str,
    )
    executions = list(
        (
            await db.execute(
                select(CreatorAgentExecution)
                .where(CreatorAgentExecution.id.in_(execution_ids))
                .order_by(CreatorAgentExecution.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
        if execution_ids
        else []
    )
    thread = await _owned_thread(
        db,
        thread_id=target.thread.id,
        creator_id=creator_id,
        lock=True,
        require_active=False,
    )
    now = datetime.now(UTC)
    for approval in approvals:
        approval.status = "cancelled"
    for execution in executions:
        if execution.status == "awaiting_approval":
            execution.status = "cancelled"
            execution.completed_at = now
    for approval in approvals:
        turn = turns.get(approval.turn_id)
        if turn is not None and turn.status == "awaiting_approval":
            turn.status = "completed"
            turn.completed_at = now
    await _append_event(
        db,
        thread,
        role="assistant",
        event_type="draft_undone",
        content="I restored the previous draft. Nothing was rendered.",
        payload={
            "draft_id": str(row.id),
            "draft_revision": row.draft_revision,
            "restored_from_draft_id": str(parent.id),
            "cancelled_approval_ids": [str(approval.id) for approval in approvals],
        },
    )
    result = _out(row, can_undo=True)
    owner_thread_id = thread.id
    await db.commit()
    successor_id = await _promote_queued_successor(db, thread_id=owner_thread_id)
    return result, successor_id
