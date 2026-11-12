"""Authoritative immutable draft revisions for Kria runtime v2.

Draft bodies are full, bounded snapshots.  Writers serialize on the owning
PlanItem, validate the exact current Job generation, clear the previous head,
and insert a new head.  Nothing in this module renders or talks to a broker.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.text_element import resolve_narrated_storyboard_rows
from app.db_locks import CONTENT_PLAN_LOCK
from app.kria.api_schemas import DraftSnapshotOut
from app.kria.brief_binding import BriefBinding
from app.kria.editor_head import (
    MAX_RESTAGE_CHAIN,
    RESTAMP_REASONS,
    is_head_current,
    restage_marker,
    variant_baseline,
)
from app.kria.runtime import (
    RuntimeFailure,
    _append_event,
    _owned_thread,
    _promote_queued_successor,
    _thread_say,
)
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
from app.services.length_policy import creator_duration

MAX_DRAFT_BYTES = 2 * 1024 * 1024


def _without_none(value: object) -> object:
    """``value`` with every None dict entry left out, at any depth: a stored strategy and a
    client's copy of it may spell an unset key either way."""
    if isinstance(value, Mapping):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_none(item) for item in value]
    return value


def _sanitize_strategy_duration_provenance(
    raw: dict[str, Any],
    *,
    authoritative_answers: list[dict[str, Any]] | None = None,
    authoritative_duration_s: float | None = None,
    authoritative_pacing_requested: bool | None = None,
    head_strategy: Mapping[str, Any] | None = None,
    brief_binding: BriefBinding | None = None,
) -> dict[str, Any]:
    """Persist only server-derived duration approval provenance.

    Draft snapshots are client-writable. A client may propose a duration, but
    must not claim it was explicitly approved by supplying the server-owned
    marker. The client-supplied duration key is a proposal only: the proof and
    exact target must come from an approved binding or a prior server stamp.

    ``choice_answers`` (KRI-476) are server-owned the same way: whatever the client wrote
    is dropped and only the head draft's own answers (the planner gate wrote them) stay.
    So are ``planner_declines`` (KRI-1097 wave 2, approval-card lines): only the head
    draft's (``head_strategy``, the Main Creator parse wrote them), and of those only the ones
    whose field this save leaves as the head had it. A decline names a value the plan does not
    use, so after an edit to that field it would be stale ("bounce" is not used, under a title
    the client set to fade).
    """

    binding_answers = (
        brief_binding.choice_answers if brief_binding is not None else authoritative_answers or ()
    )
    proven = creator_duration(
        brief=brief_binding.brief if brief_binding is not None else None,
        authoritative_answers=binding_answers,
    )
    proven_duration_s = proven.seconds if proven is not None else authoritative_duration_s
    head = head_strategy or {}
    head_declines = head.get("planner_declines")
    strategy = CreativeStrategy.model_validate(
        {
            **{
                key: value
                for key, value in raw.items()
                if key
                not in (
                    "target_duration_requested",
                    "creator_duration_s",
                    "choice_answers",
                    "selected_music_section",
                    "pacing_requested",
                    "planner_declines",
                )
            },
            **({"planner_declines": head_declines} if head_declines else {}),
        }
    )
    if strategy.planner_declines:
        saved = strategy.model_dump(mode="json", exclude_none=True)
        kept = [
            decline
            for decline in strategy.planner_declines
            if _without_none(saved.get(decline.field)) == _without_none(head.get(decline.field))
        ]
        strategy = strategy.model_copy(update={"planner_declines": kept or None})
    update: dict[str, Any] = {
        "target_duration_requested": True if proven_duration_s is not None else None,
        "creator_duration_s": proven_duration_s,
        "choice_answers": authoritative_answers or None,
        # Server-owned like the duration marker: only the head draft's own stamp survives.
        "pacing_requested": True if authoritative_pacing_requested is True else None,
    }
    if brief_binding is not None:
        length_plan = brief_binding.media_snapshot.get("length_plan")
        if isinstance(length_plan, dict):
            update["selected_music_section"] = length_plan.get("licensed_music")
    if proven_duration_s is not None:
        update["target_duration_s"] = proven_duration_s
        if proven is not None and proven.bound == "at_most":
            # KRI-847: "at most N seconds" caps the plan; the model's own shorter pick (from the
            # footage) stays, a longer one (or the 24 s default) is held to the ceiling.
            update["target_duration_s"] = min(strategy.target_duration_s, proven_duration_s)
    strategy = strategy.model_copy(update=update)
    return strategy.model_dump(mode="json", exclude_none=True)


class KriaDraftDocument(BaseModel):
    """Wire-safe draft body shared by agent and editor clients."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=2, ge=2, le=2)
    kind: Literal["initial", "editor", "strategy"]
    intent: str = Field(default="", max_length=4000)
    edit_format: str = Field(default="montage", min_length=1, max_length=80)
    editor_payload: dict[str, Any] | None = None
    # KRI-218: this turn's text before/after (compiler `text_diff`); receipts read it.
    editor_text_diff: list[dict[str, Any]] | None = Field(default=None, max_length=200)
    strategy: dict[str, Any] | None = None
    changes: list[str] = Field(default_factory=list, max_length=24)
    # Opaque id of the client editor state this draft was built on (echoed, never
    # recomputed). Absent for drafts that predate it, so their hashes do not change.
    client_state_id: str | None = Field(default=None, max_length=64)
    brief_binding: BriefBinding | None = None
    brief_coverage: dict[str, Any] | None = None
    prepared_collection_story: dict[str, Any] | None = None
    # A head the server moved onto a newer generation records which row it moved and why
    # (``editor_head.restage_marker``); nothing infers a restage from other fields.
    restage: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> KriaDraftDocument:
        if self.kind == "editor" and self.editor_payload is None:
            raise ValueError("editor drafts require editor_payload")
        if self.kind != "editor" and self.editor_payload is not None:
            raise ValueError("only editor drafts can contain editor_payload")
        if self.kind != "editor" and self.editor_text_diff is not None:
            raise ValueError("only editor drafts can contain editor_text_diff")
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
    if snapshot.get("editor_text_diff") is None:
        # Absent stays absent so existing drafts keep their snapshot hash / ETag.
        snapshot.pop("editor_text_diff", None)
    if snapshot.get("client_state_id") is None:
        snapshot.pop("client_state_id", None)
    for field in ("brief_binding", "brief_coverage", "prepared_collection_story", "restage"):
        if snapshot.get(field) is None:
            snapshot.pop(field, None)
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
        # The join locks the plan row too, so it takes the plan's lock mode.
        statement = statement.with_for_update(**CONTENT_PLAN_LOCK)
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
        generation_id = variant_baseline(selected) or None
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


async def editor_variant_target(
    db: AsyncSession,
    *,
    item: PlanItem,
    job: Job,
    creator_id: uuid.UUID,
    variant_key: str,
) -> DraftTarget:
    """Resolve an already-locked, owned editor target without thread selection.

    The editor-commit route owns the PlanItem/Job locks. A direct legacy editor
    link may not have an owning conversation yet; linking it here follows the
    same item-scoped rule as ``creation-threads/for-editor`` and dispatches no
    agent or render. The explicit variant is never inferred from chat state.
    """
    if (
        job.user_id != creator_id
        or job.content_plan_item_id != item.id
        or item.current_job_id != job.id
        or _variant(job, variant_key) is None
    ):
        raise RuntimeFailure(404, "draft_target_missing", "Editable video not found", phase="tool")
    thread = (
        await db.execute(
            select(CreationThread)
            .where(
                CreationThread.creator_id == creator_id,
                CreationThread.active_plan_item_id == item.id,
            )
            .order_by(CreationThread.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if thread is None:
        thread = CreationThread(
            creator_id=creator_id,
            runtime_version=1,
            content_plan_id=item.content_plan_id,
            active_plan_item_id=item.id,
            active_job_id=job.id,
            title=str(item.idea or "Untitled video")[:200],
            state={"edit_format": item.edit_format or "montage"},
        )
        db.add(thread)
        await db.flush()
    elif thread.active_job_id not in (None, job.id):
        raise RuntimeFailure(
            409, "draft_target_stale", "The project changed. Reopen it before saving.", phase="tool"
        )
    variant = _variant(job, variant_key) or {}
    return DraftTarget(
        thread=thread,
        item=item,
        job=job,
        variant_key=variant_key,
        generation_id=variant_baseline(variant) or None,
    )


async def stage_editor_variant_draft(
    db: AsyncSession, *, target: DraftTarget, document: KriaDraftDocument
) -> DraftSnapshotOut:
    """Stage a saved editor draft in the caller's transaction; never render."""
    parent = await _head(db, item_id=target.item.id, variant_key=target.variant_key, lock=True)
    row = await _insert_revision(db, target=target, document=document, parent=parent)
    return _out(row, can_undo=parent is not None)


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


def _out(row: CreatorEditDraft, *, can_undo: bool, head_current: bool = True) -> DraftSnapshotOut:
    if row.snapshot_json is None:
        raise RuntimeFailure(
            410,
            "draft_pruned",
            "This old draft body is no longer available.",
            phase="tool",
            recovery="refresh_replan",
        )
    payload = row.snapshot_json.get("editor_payload")
    base_generation = (
        str(payload.get("base_generation") or "")
        if isinstance(payload, dict)
        else row.base_generation_id
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
        head_current=head_current,
        base_generation=base_generation,
    )


def _editor_snapshot(variant: dict[str, Any], generation_id: str | None) -> dict[str, Any]:
    """Project only editor-owned state; omit media URLs and renderer internals."""

    keys = (
        "text_elements",
        "speech_timing_basis",
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
        "drawings",
        "carousel_moment",
        "speaker_background",
    )
    sections = {key: variant[key] for key in keys if key in variant}
    if isinstance(sections.get("text_elements"), list):
        # Storyboard bars carry their burned look, as the status route serves them.
        sections["text_elements"] = resolve_narrated_storyboard_rows(sections["text_elements"])
    timeline = variant.get("user_timeline") or variant.get("ai_timeline")
    if isinstance(timeline, dict) and isinstance(timeline.get("slots"), list):
        sections["timeline_slots"] = timeline["slots"]
        if timeline.get("split_screen_sections"):
            sections["split_screen_sections"] = timeline["split_screen_sections"]
    return {"base_generation": generation_id or "", "sections": sections}


def bootstrap_editor_payload(
    job: Any, variant_key: str, generation_id: str | None
) -> dict[str, Any]:
    """A fresh draft's editor payload for ``job``'s variant.

    Starts from the variant as the status route serves it: a never-Saved phone Talking
    edit keeps its speaker background, sounds and cards only in the pinned device recipe,
    which the route projects on read (KRI-1165).
    """
    from app.routes.generative_jobs import (  # noqa: PLC0415
        _augment_variant_with_phone_subtitled_editor_sections,
    )

    variant = dict(_variant(job, variant_key) or {})
    return _editor_snapshot(
        _augment_variant_with_phone_subtitled_editor_sections(job, variant), generation_id
    )


def bootstrap_document(
    *,
    state: Mapping[str, Any],
    item: PlanItem,
    job: Any,  # noqa: ANN401
    variant_key: str,
    generation_id: str | None,
) -> KriaDraftDocument:
    """The draft a read creates for a variant that has no head."""
    return KriaDraftDocument(
        kind="initial" if job is None else "editor",
        intent=str(state.get("intent") or "")[:4000],
        edit_format=str(item.edit_format or state.get("edit_format") or "montage"),
        editor_payload=(
            bootstrap_editor_payload(job, variant_key, generation_id) if job is not None else None
        ),
        changes=[],
    )


def committed_document(
    *,
    state: Mapping[str, Any],
    item: PlanItem,
    job: Any,
    variant_key: str,  # noqa: ANN401
) -> KriaDraftDocument:
    """The variant's committed state as a draft: ``bootstrap_document`` on its current baseline.

    A v2 variant's sections come from the builder its own Save uses (``editor_sections``), which
    carries every lane a Save pins; the bootstrap key set is smaller and would drop them.
    """
    baseline = variant_baseline(_variant(job, variant_key) or {})
    document = bootstrap_document(
        state=state, item=item, job=job, variant_key=variant_key, generation_id=baseline
    )
    if (_variant(job, variant_key) or {}).get("editor_state_version") == 2:
        from app.services.editor_empty_drafts import editor_sections  # noqa: PLC0415

        document = document.model_copy(
            update={
                "editor_payload": {
                    "base_generation": baseline,
                    "sections": editor_sections(job, variant_key),
                    "editor_state_version": 2,
                }
            }
        )
    return document


async def _row(db: AsyncSession, draft_id: uuid.UUID | None) -> CreatorEditDraft | None:
    if draft_id is None:
        return None
    return (
        await db.execute(select(CreatorEditDraft).where(CreatorEditDraft.id == draft_id))
    ).scalar_one_or_none()


async def _undo_target(db: AsyncSession, head: CreatorEditDraft) -> tuple[uuid.UUID | None, bool]:
    """The draft Undo restores, and whether phone-publish restamps lie between it and the head.

    Restage rows are the server moving the head, not creator edits, so Undo looks past them. An
    approval's render holds the edit, so Undo across one is unavailable (committed work is not
    undone by draft undo): ``(None, False)``. A head with no restage row is ``main``'s case.
    """
    row: CreatorEditDraft | None = head
    restamped = False
    for _ in range(MAX_RESTAGE_CHAIN):
        marker = restage_marker(row)
        if marker is None:
            return row.parent_draft_id, restamped
        if marker.get("reason") not in RESTAMP_REASONS:
            return None, False
        restamped = True
        row = await _row(db, row.parent_draft_id)
        if row is None:
            return None, False
    return None, False


async def can_undo(db: AsyncSession, head: CreatorEditDraft) -> bool:
    """The one definition of ``can_undo``, used by the draft and the timeline reads."""
    return (await _undo_target(db, head))[0] is not None


def _current(target: DraftTarget, head: CreatorEditDraft) -> bool:
    """``editor_head.is_head_current`` for the target's job and variant."""
    if target.job is None:
        return head.base_job_id is None and head.base_generation_id == target.generation_id
    variant = _variant(target.job, target.variant_key)
    if variant is None:
        return head.base_job_id == target.job.id and head.base_generation_id == target.generation_id
    return is_head_current(target.job, variant, head)


async def read_or_bootstrap_draft(
    db: AsyncSession, *, thread_id: uuid.UUID, creator_id: uuid.UUID
) -> DraftSnapshotOut:
    target = await _target(db, thread_id=thread_id, creator_id=creator_id, lock_item=True)
    head = await _head(db, item_id=target.item.id, variant_key=target.variant_key, lock=True)
    if head is None:
        document = bootstrap_document(
            state=dict(target.thread.state or {}),
            item=target.item,
            job=target.job,
            variant_key=target.variant_key,
            generation_id=target.generation_id,
        )
        head = await _insert_revision(db, target=target, document=document, parent=None)
        await db.commit()
    return _out(head, can_undo=await can_undo(db, head), head_current=_current(target, head))


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
    if not _current(target, head):
        raise RuntimeFailure(
            409,
            "draft_base_stale",
            "The rendered video changed while this draft was open.",
            phase="tool",
            recovery="refresh_replan",
            current_revision=int(target.thread.revision),
        )
    snapshot = dict(snapshot)
    for field in ("brief_binding", "brief_coverage", "prepared_collection_story"):
        authoritative = (head.snapshot_json or {}).get(field)
        if field in snapshot and snapshot[field] != authoritative:
            raise RuntimeFailure(
                422,
                "draft_authority_read_only",
                "Refresh the draft before saving this edit.",
                phase="tool",
                recovery="refresh_replan",
            )
        snapshot[field] = authoritative
    if snapshot.get("kind") == "strategy" and isinstance(snapshot.get("strategy"), dict):
        head_strategy = (head.snapshot_json or {}).get("strategy")
        binding = snapshot.get("brief_binding")
        snapshot["strategy"] = _sanitize_strategy_duration_provenance(
            snapshot["strategy"],
            authoritative_answers=(
                head_strategy.get("choice_answers") if isinstance(head_strategy, dict) else None
            ),
            authoritative_duration_s=(
                head_strategy.get("creator_duration_s")
                if isinstance(head_strategy, dict)
                and isinstance(head_strategy.get("creator_duration_s"), (int, float))
                else None
            ),
            authoritative_pacing_requested=(
                head_strategy.get("pacing_requested") if isinstance(head_strategy, dict) else None
            ),
            head_strategy=head_strategy if isinstance(head_strategy, dict) else None,
            brief_binding=(
                BriefBinding.model_validate(binding) if isinstance(binding, dict) else None
            ),
        )
    document = KriaDraftDocument.model_validate(snapshot)
    row = await _insert_revision(db, target=target, document=document, parent=head)
    await db.commit()
    return _out(row, can_undo=True, head_current=_current(target, row))


POINTER_SYNC = "pointer_sync"


def pointer_sync_record(
    session: CreatorAgentSession, generation: str | None
) -> dict[str, Any] | None:
    """What an editor-op draft is about to do to the session's pointer, or None when nothing.

    An editor draft is built on the saved video, so committing one moves the pointer (the video
    the agent last read) onto it. The draft keeps the value it replaced, on its own execution,
    so Undo can put it back: the plan the head returns to was drafted against the old video and
    holds none of what a native Save changed, which is exactly what ``pinned.save_unread`` asks
    the pointer about. Written by every path that re-syncs the pointer for a draft.
    """
    if str(session.target_generation_id or "") == str(generation or ""):
        return None
    return {
        "session_id": str(session.id),
        "from": session.target_generation_id,
        "to": generation,
    }


def _pointer_record(result: Any) -> dict[str, Any] | None:  # noqa: ANN401
    record = result.get(POINTER_SYNC) if isinstance(result, dict) else None
    if not isinstance(record, dict) or not record.get("session_id"):
        return None
    return record


async def _pointer_record_of(
    db: AsyncSession, draft: CreatorEditDraft | None, expected_revision: int
) -> dict[str, Any] | None:
    """The pointer footprint of ``draft`` when it is the revision being undone, read unlocked.

    A draft never changes after it is minted, so the unlocked read is the locked one once
    ``undo_draft`` has checked that the head is that revision. It is read early because the
    session must be locked before the turns and the head (Plan -> PlanItem -> Job -> Session ->
    Turn -> Draft -> Approval -> Thread).
    """
    source_id = getattr(draft, "source_execution_id", None)
    if draft is None or source_id is None or draft.draft_revision != expected_revision:
        return None
    result = (
        await db.execute(
            select(CreatorAgentExecution.result).where(CreatorAgentExecution.id == source_id)
        )
    ).scalar_one_or_none()
    return _pointer_record(result)


def _restore_pointer(session: CreatorAgentSession | None, record: dict[str, Any]) -> None:
    """Put the pointer back where the undone draft found it, if nothing moved it since.

    A pointer that is no longer where the draft left it was moved by something that read the
    video after (a Kria render's completion): that read stands, and so does its pointer.
    """
    if session is None or str(session.id) != str(record.get("session_id")):
        return
    if str(session.target_generation_id or "") == str(record.get("to") or ""):
        session.target_generation_id = record.get("from")


def _footprint_record(execution: CreatorAgentExecution | None) -> dict[str, Any] | None:
    record = (execution.result or {}).get("pinned_undo") if execution is not None else None
    return dict(record) if isinstance(record, dict) else None


def _stages_a_change(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """The footprint of a tap that wrote a requirement or flipped a setting, else None.

    A staged approval that ends without a render takes back what its tap wrote besides the draft.
    A tap that wrote nothing else (an editor op with no creative brief) has only the card's
    identity on record, which is for Undo alone: the draft it wrote is still the head after the
    approval ends, so its card stays spent.
    """
    if record is None or not (record.get("requirement_ids") or record.get("setting")):
        return None
    return record


def _withdraw_footprint(
    db: Any, *, item: PlanItem | None, thread: CreationThread, record: dict[str, Any]
) -> None:
    """Take back what a tapped suggestion card did besides writing the draft it staged.

    ``record`` is the tap's footprint (``pinned_undo`` on the draft's execution): the brief
    requirements the turn wrote and, for a card that flips a setting, the values it held before.
    A staged tap that ends without a render (Undo, a deny, an expiry, a newer video) must not
    leave them: the typed requirement the creator never typed would stay live (it blocks the
    next approval and hides the card, KRI-129) and the setting would keep cutting speech the
    creator turned down.

    Only what is still the tap's is taken back: a requirement a later turn or the creator rewrote
    has a different ``source_turn_id``, and a setting they flipped themselves is theirs. The
    caller holds the item (when the record carries a setting) and the thread row locked, which
    orders the brief version. ``db`` is a sync session: async callers reach it through
    ``AsyncSession.run_sync``, so the sweeper's sync transaction and the API's share one body.
    """
    from app.services.suggestions import pinned  # noqa: PLC0415

    cleared = pinned.unmark_consumed(
        thread.state,
        str(record.get("set_id") or ""),
        str(record.get("id") or ""),
        record.get("turn_id"),
    )
    if cleared is not None:
        thread.state = cleared
    print_ = record.get("print")
    if print_ and not record.get("hidden_before"):
        # A GET that found the tap's applied mark folded its print into the list that outlives
        # the ring; the card is an offer again, so that memory of it goes too.
        unfolded = pinned.without_kept_print(thread.state, str(print_))
        if unfolded is not None:
            thread.state = unfolded
    setting = record.get("setting")
    if isinstance(setting, dict) and item is not None:
        from app.services.suggestions.kinds.filler_words import (  # noqa: PLC0415
            undo_item_setting,
        )

        undo_item_setting(item, setting)
    ids = [str(i) for i in record.get("requirement_ids") or []]
    if not ids:
        return
    from app.kria.brief import (  # noqa: PLC0415
        BriefUpdate,
        apply_updates,
        load_latest_brief_sync,
        removed_target_ids,
    )
    from app.models import CreativeBriefVersion  # noqa: PLC0415

    thread_id = thread.id
    prior = load_latest_brief_sync(db, thread_id)
    turn_id = str(record.get("turn_id") or "")
    mine = {req.id for req in (prior.live() if prior else []) if req.source_turn_id == turn_id}
    removals = [
        BriefUpdate(operation="remove", target_requirement_id=i, expected_version=prior.version)
        for i in ids
        if prior is not None and i in mine
    ]
    if not removals:
        return
    merged = apply_updates(
        prior, removals, source_turn_id=None, server_removed_ids=removed_target_ids(removals)
    )
    next_version = (
        int(
            db.execute(
                select(func.coalesce(func.max(CreativeBriefVersion.version), 0)).where(
                    CreativeBriefVersion.thread_id == thread_id
                )
            ).scalar_one()
        )
        + 1
    )
    db.add(
        CreativeBriefVersion(
            thread_id=thread_id,
            version=next_version,
            requirements=[req.model_dump(mode="json") for req in merged.requirements],
            source_turn_id=None,
        )
    )
    db.flush()


async def _withdraw_pinned_tap(
    db: AsyncSession, *, item: PlanItem, thread: CreationThread, undone: CreatorEditDraft
) -> None:
    """Undo of the draft ``undone``: its tap's footprint (see ``_withdraw_footprint``)."""
    source_id = getattr(undone, "source_execution_id", None)
    if source_id is None:
        return
    record = _footprint_record(await db.get(CreatorAgentExecution, source_id))
    if record is None:
        return
    await db.run_sync(
        lambda sync_db: _withdraw_footprint(sync_db, item=item, thread=thread, record=record)
    )


async def staged_tap_footprint(
    db: AsyncSession, approval: CreatorAgentApproval
) -> dict[str, Any] | None:
    """The footprint of the tapped card whose draft ``approval`` would render, or None.

    An unlocked read, for the callers that must lock the plan item before the session (a record
    that carries a setting means the item is written): the row it reads never changes after the
    draft is minted.
    """
    draft_id = getattr(approval, "draft_id", None)
    if draft_id is None:
        return None
    draft = await db.get(CreatorEditDraft, draft_id)
    source_id = getattr(draft, "source_execution_id", None)
    if source_id is None:
        return None
    return _stages_a_change(_footprint_record(await db.get(CreatorAgentExecution, source_id)))


def staged_tap_footprint_sync(db: Any, approval: CreatorAgentApproval) -> dict[str, Any] | None:
    """``staged_tap_footprint`` for the sync sweeper."""
    draft_id = getattr(approval, "draft_id", None)
    if draft_id is None:
        return None
    draft = db.get(CreatorEditDraft, draft_id)
    source_id = getattr(draft, "source_execution_id", None)
    if source_id is None:
        return None
    return _stages_a_change(_footprint_record(db.get(CreatorAgentExecution, source_id)))


async def withdraw_staged_tap(
    db: AsyncSession,
    *,
    approval: CreatorAgentApproval,
    item: PlanItem | None,
    thread: CreationThread,
) -> bool:
    """A staged approval ends without a render: take back its tap. True when a tap was withdrawn.

    The caller holds ``thread`` locked, and ``item`` when ``staged_tap_footprint`` carries a
    setting. Nothing here commits.
    """
    record = await staged_tap_footprint(db, approval)
    if record is None:
        return False
    await db.run_sync(
        lambda sync_db: _withdraw_footprint(sync_db, item=item, thread=thread, record=record)
    )
    return True


def withdraw_staged_tap_sync(
    db: Any, *, record: dict[str, Any], item: PlanItem | None, thread: CreationThread
) -> None:
    """``withdraw_staged_tap`` for the sync sweeper, which read ``record`` before it locked."""
    _withdraw_footprint(db, item=item, thread=thread, record=record)


async def undo_draft(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    expected_revision: int,
) -> tuple[DraftSnapshotOut, str | None]:
    target = await _target(db, thread_id=thread_id, creator_id=creator_id, lock_item=True)
    pointer = await _pointer_record_of(
        db,
        await _head(db, item_id=target.item.id, variant_key=target.variant_key, lock=False),
        expected_revision,
    )
    session = (
        (
            await db.execute(
                select(CreatorAgentSession)
                .where(CreatorAgentSession.id == uuid.UUID(str(pointer["session_id"])))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if pointer is not None
        else None
    )
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
    parent_id, restamped = await _undo_target(db, head)
    if parent_id is None:
        raise RuntimeFailure(
            409,
            "draft_undo_unavailable",
            "There is no earlier draft to restore.",
            phase="tool",
            recovery="none",
            current_revision=int(target.thread.revision),
        )
    parent = await _row(db, parent_id)
    if parent is None or parent.snapshot_json is None:
        raise RuntimeFailure(
            409,
            "draft_undo_unavailable",
            "The earlier draft is no longer available.",
            phase="tool",
            recovery="refresh_replan",
        )
    document = KriaDraftDocument.model_validate(parent.snapshot_json).model_copy(
        update={"restage": None}
    )
    if restamped and isinstance(document.editor_payload, dict):
        # The restamps between the head and the restored draft only re-stamped the same document.
        document = document.model_copy(
            update={
                "editor_payload": {
                    **document.editor_payload,
                    "base_generation": target.generation_id or "",
                }
            }
        )
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
    await _withdraw_pinned_tap(db, item=target.item, thread=thread, undone=head)
    if pointer is not None:
        _restore_pointer(session, pointer)
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
        content=_thread_say(
            thread,
            en="I restored the previous draft. Nothing was rendered.",
            tr="Önceki taslağı geri getirdim. Yeni bir video oluşturulmadı.",
        ),
        payload={
            "draft_id": str(row.id),
            "draft_revision": row.draft_revision,
            "restored_from_draft_id": str(parent.id),
            "cancelled_approval_ids": [str(approval.id) for approval in approvals],
        },
    )
    result = _out(row, can_undo=True, head_current=_current(target, row))
    owner_thread_id = thread.id
    await db.commit()
    successor_id = await _promote_queued_successor(db, thread_id=owner_thread_id)
    return result, successor_id
