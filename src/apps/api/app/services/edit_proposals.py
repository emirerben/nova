"""Pure state transitions for guided-edit proposals."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from app.models import PlanItem
from app.schemas.edit_proposal import (
    EDIT_CONVERSATION_MAX_TURNS,
    ApprovedProposalSnapshot,
    ConversationPhase,
    EditConversationAttempt,
    EditConversationTurn,
    EditProposal,
    EditProposalSnapshot,
    ProposalBrief,
    parse_edit_proposal,
)


class ProposalConflictError(ValueError):
    pass


EDIT_CONVERSATION_ATTEMPT_TTL_S = 90


def reserve_edit_conversation_attempt(
    item: PlanItem,
    *,
    expected_version: int,
    now: datetime | None = None,
) -> tuple[EditProposal, str]:
    """Reserve one model call without holding a DB lock while it runs."""

    current = parse_edit_proposal(item.edit_proposal)
    timestamp = now or datetime.now(UTC)
    active = current.conversation_attempt if current else None
    if active is not None:
        age_s = max(0.0, (timestamp - active.started_at).total_seconds())
        if age_s < EDIT_CONVERSATION_ATTEMPT_TTL_S:
            raise ProposalConflictError("Kria is already thinking about this edit.")
        valid_expected_versions = {current.proposal_version}
        if active.placeholder:
            # The synthetic first-turn envelope is visible after a reload as
            # version 1, while the abandoned request originally expected 0.
            # Either view may safely reclaim the same empty placeholder.
            valid_expected_versions.add(active.expected_proposal_version)
    else:
        valid_expected_versions = {current.proposal_version if current else 0}
    if expected_version not in valid_expected_versions:
        actual_for_request = current.proposal_version if current else 0
        raise ProposalConflictError(
            f"The edit plan changed in another tab (expected version {expected_version}, "
            f"found {actual_for_request})."
        )
    if current and current.status in {"analyzing", "drafting"}:
        raise ProposalConflictError("Kria is already building this edit plan.")

    token = str(uuid.uuid4())
    reserved_version = current.proposal_version if current else 1
    attempt = EditConversationAttempt(
        token=token,
        expected_proposal_version=expected_version,
        reserved_proposal_version=reserved_version,
        started_at=timestamp,
        placeholder=current is None,
    )
    proposal = (
        current.model_copy(update={"conversation_attempt": attempt})
        if current
        else EditProposal(
            proposal_version=reserved_version,
            generation_attempt_id=f"brief-{uuid.uuid4()}",
            status="briefing",
            conversation_attempt=attempt,
        )
    )
    item.edit_proposal = proposal.model_dump(mode="json")
    return proposal, token


def require_edit_conversation_attempt(item: PlanItem, *, token: str) -> EditProposal:
    """Fence the final write to the exact reservation and proposal version."""

    current = parse_edit_proposal(item.edit_proposal)
    attempt = current.conversation_attempt if current else None
    if (
        current is None
        or attempt is None
        or attempt.token != token
        or current.proposal_version != attempt.reserved_proposal_version
    ):
        raise ProposalConflictError("The edit plan changed while Kria was thinking.")
    return current


def release_edit_conversation_attempt(item: PlanItem, *, token: str) -> bool:
    """Release a live reservation without disturbing a concurrent winner."""

    current = parse_edit_proposal(item.edit_proposal)
    attempt = current.conversation_attempt if current else None
    if current is None or attempt is None or attempt.token != token:
        return False
    if (
        attempt.placeholder
        and current.proposal_version == attempt.reserved_proposal_version
        and not current.conversation
        and current.draft is None
        and current.last_approved is None
    ):
        item.edit_proposal = None
    else:
        item.edit_proposal = current.model_copy(update={"conversation_attempt": None}).model_dump(
            mode="json"
        )
    return True


def save_edit_conversation_turn(
    item: PlanItem,
    *,
    expected_version: int,
    brief: ProposalBrief,
    user_message: str,
    agent_reply: str,
    suggestions: list[str],
    ready_to_plan: bool,
    conversation_phase: ConversationPhase = "briefing",
    revised_snapshot: EditProposalSnapshot | None = None,
) -> EditProposal:
    """Persist one user/agent exchange under the proposal CAS contract.

    A reply that revises a current proposal returns it to ``draft`` so the
    creator must approve the changed wording and sequence again. A briefing
    exchange never starts analysis by itself.
    """

    current = parse_edit_proposal(item.edit_proposal)
    actual = current.proposal_version if current else 0
    if actual != expected_version:
        raise ProposalConflictError(
            f"The edit plan changed in another tab (expected version {expected_version}, "
            f"found {actual})."
        )
    if current and current.status in {"analyzing", "drafting"}:
        raise ProposalConflictError("Kria is already building this edit plan.")

    # A failed render attempt or stale media must not erase the creator's
    # direction. Analysis/drafting states have already been rejected above, so
    # every remaining proposal state is a valid continuation of the same chat.
    prior_turns = list(current.conversation) if current is not None else []
    conversation = [
        *prior_turns,
        EditConversationTurn(role="user", phase=conversation_phase, content=user_message.strip()),
        EditConversationTurn(
            role="agent",
            phase=conversation_phase,
            content=agent_reply.strip(),
            suggestions=suggestions[:3],
        ),
    ][-EDIT_CONVERSATION_MAX_TURNS:]
    status = (
        "draft"
        if revised_snapshot is not None
        else (current.status if current and current.status in {"draft", "approved"} else "briefing")
    )
    proposal = EditProposal(
        proposal_version=actual + 1,
        generation_attempt_id=(
            current.generation_attempt_id if current else f"brief-{uuid.uuid4()}"
        ),
        media_digest=current.media_digest if current else None,
        status=status,
        brief=brief,
        conversation=conversation,
        brief_ready=ready_to_plan,
        conversation_attempt=None,
        draft=(
            revised_snapshot
            if revised_snapshot is not None
            else (current.draft if current else None)
        ),
        last_approved=current.last_approved if current else None,
        failure=None,
    )
    item.edit_proposal = proposal.model_dump(mode="json")
    return proposal


def clip_ref_matches(ref, assignment: dict | None) -> bool:  # noqa: ANN001
    return bool(
        assignment
        and assignment.get("gcs_path") == ref.gcs_path
        and str(assignment.get("media_id")) == ref.media_id
    )


def asset_ref_matches(ref, asset) -> bool:  # noqa: ANN001
    return bool(
        asset is not None
        and asset.gcs_path == ref.gcs_path
        and str(asset.gcs_generation or "legacy") == ref.generation
        and asset.kind == ref.kind
    )


def media_generations_match_sync(refs) -> bool:  # noqa: ANN001
    """Check exact media generations with bounded parallel storage reads."""

    from app import storage  # noqa: PLC0415

    media_refs = list(refs)
    if not media_refs:
        return True
    try:
        with ThreadPoolExecutor(max_workers=min(8, len(media_refs))) as executor:
            expected_by_future = {
                executor.submit(storage.object_metadata, ref.gcs_path): ref.generation
                for ref in media_refs
            }
            return all(
                future.result().generation == expected_by_future[future]
                for future in as_completed(expected_by_future)
            )
    except Exception:  # noqa: BLE001 - missing/replaced media is stale
        return False


def begin_proposal_attempt(item: PlanItem, *, brief: ProposalBrief | None = None) -> EditProposal:
    current = parse_edit_proposal(item.edit_proposal)
    proposal = EditProposal(
        proposal_version=(current.proposal_version + 1 if current else 1),
        generation_attempt_id=str(uuid.uuid4()),
        media_digest=None,
        status="analyzing",
        brief=brief or ProposalBrief(),
        conversation=current.conversation if current else [],
        brief_ready=current.brief_ready if current else False,
        draft=None,
        last_approved=current.last_approved if current else None,
        failure=None,
    )
    item.edit_proposal = proposal.model_dump(mode="json")
    return proposal


def require_expected_version(item: PlanItem, expected: int) -> EditProposal:
    proposal = parse_edit_proposal(item.edit_proposal)
    if proposal is None or proposal.proposal_version != expected:
        actual = proposal.proposal_version if proposal else 0
        raise ProposalConflictError(
            f"The edit plan changed in another tab (expected version {expected}, found {actual})."
        )
    return proposal


def save_proposal_draft(
    item: PlanItem,
    *,
    expected_version: int,
    snapshot: EditProposalSnapshot,
) -> EditProposal:
    current = require_expected_version(item, expected_version)
    proposal = current.model_copy(
        update={
            "proposal_version": current.proposal_version + 1,
            "status": "draft",
            "draft": snapshot,
            "failure": None,
        }
    )
    item.edit_proposal = proposal.model_dump(mode="json")
    return proposal


def approve_proposal(item: PlanItem, *, expected_version: int) -> EditProposal:
    current = require_expected_version(item, expected_version)
    if current.status != "draft" or current.draft is None or current.media_digest is None:
        raise ProposalConflictError("Only a current draft can be approved.")
    approved_version = current.proposal_version + 1
    approved = ApprovedProposalSnapshot(
        proposal_version=approved_version,
        media_digest=current.media_digest,
        approved_at=datetime.now(UTC),
        snapshot=current.draft,
    )
    proposal = current.model_copy(
        update={
            "proposal_version": approved_version,
            "status": "approved",
            "last_approved": approved,
            "failure": None,
        }
    )
    item.edit_proposal = proposal.model_dump(mode="json")
    return proposal


def mark_edit_proposal_stale(item: PlanItem) -> bool:
    """Retain both snapshots while invalidating approval after a media change."""

    current = parse_edit_proposal(item.edit_proposal)
    if current is None or current.status == "stale":
        return False
    stale = current.model_copy(
        update={
            "proposal_version": current.proposal_version + 1,
            "status": "stale",
            "failure": None,
        }
    )
    item.edit_proposal = stale.model_dump(mode="json")
    return True


def proposal_generate_error(item: PlanItem) -> str | None:
    proposal = parse_edit_proposal(item.edit_proposal)
    if proposal is None:
        return "proposal_required"
    if proposal.status in {"analyzing", "drafting"}:
        return "proposal_analyzing"
    if proposal.status == "stale":
        return "proposal_stale"
    if proposal.status != "approved" or proposal.last_approved is None:
        return "proposal_draft"
    return None


def validate_approved_proposal_media_sync(  # noqa: ANN001
    session, item: PlanItem, *, owner_id: uuid.UUID
) -> tuple[str | None, dict | None]:
    """Task-side trust boundary for Generate under the PlanItem lock.

    Returns an explicit proposal error code or the exact approved payload to
    snapshot into the Job. Storage generations are re-read here so an object
    replacement cannot ride a previously approved filename.
    """

    from sqlalchemy import select  # noqa: PLC0415

    from app.models import PlanItemAsset  # noqa: PLC0415
    from app.schemas.edit_proposal import canonical_media_digest  # noqa: PLC0415

    error = proposal_generate_error(item)
    if error:
        return error, None
    proposal = parse_edit_proposal(item.edit_proposal)
    assert proposal is not None and proposal.last_approved is not None
    approved = proposal.last_approved
    if canonical_media_digest(approved.snapshot.media) != approved.media_digest:
        return "proposal_stale", None

    clip_by_id = {
        str(a.get("media_id")): a
        for a in (item.clip_assignments or [])
        if isinstance(a, dict) and a.get("media_id") and a.get("gcs_path")
    }
    asset_ids: list[uuid.UUID] = []
    try:
        asset_ids = [
            uuid.UUID(ref.media_id) for ref in approved.snapshot.media if ref.lane == "asset"
        ]
    except ValueError:
        return "proposal_stale", None
    assets = {
        str(row.id): row
        for row in session.execute(
            select(PlanItemAsset).where(
                PlanItemAsset.id.in_(asset_ids),
                PlanItemAsset.plan_item_id == item.id,
                PlanItemAsset.user_id == owner_id,
                PlanItemAsset.status == "ready",
            )
        )
        .scalars()
        .all()
    }
    for ref in approved.snapshot.media:
        if ref.lane == "clip":
            assignment = clip_by_id.get(ref.media_id)
            if not clip_ref_matches(ref, assignment):
                return "proposal_stale", None
        else:
            asset = assets.get(ref.media_id)
            if not asset_ref_matches(ref, asset):
                return "proposal_stale", None
    if not media_generations_match_sync(approved.snapshot.media):
        return "proposal_stale", None
    return None, approved.model_dump(mode="json")
