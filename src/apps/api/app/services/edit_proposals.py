"""Pure state transitions for guided-edit proposals."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from app.models import PlanItem
from app.schemas.edit_proposal import (
    ApprovedProposalSnapshot,
    EditProposal,
    EditProposalSnapshot,
    ProposalBrief,
    parse_edit_proposal,
)


class ProposalConflictError(ValueError):
    pass


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
