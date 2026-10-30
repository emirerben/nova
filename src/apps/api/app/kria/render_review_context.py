"""Read-only capture of approved intent and one verified native publication.

VerifiedArtifactReceipt is a trusted server-internal attestation input. Constructing
this type does not verify bytes. The storage verifier adapter is deliberately absent;
client upload hashes and publication success alone cannot supply this capability.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text

from app.database import sync_session
from app.kria.brief_binding import BriefBinding
from app.kria.drafts import KriaDraftDocument, canonical_snapshot
from app.kria.prepared_execution import PreparedExecutionSnapshot
from app.models import (
    ContentPlan,
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    CreatorPreparedExecution,
    Job,
    PlanItem,
)
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    build_render_contract,
    read_render_contract,
)
from app.services.device_render import _recipe_digest, device_record, device_status


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class VerifiedArtifactReceipt(FrozenModel):
    """Supplied only by a trusted server byte verifier, never a request body."""

    verification_id: str = Field(min_length=1)
    verification_source: Literal["server_storage_read"] = "server_storage_read"
    execution_id: str
    job_id: str
    variant_id: str
    published_attempt: str
    object_path: str = Field(min_length=1)
    object_version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_count: int = Field(gt=0)
    recipe_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class TimestampedObservation(FrozenModel):
    evidence_id: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    source: Literal["provider_observation", "direct_frame", "actual_render_bytes"]
    observer: str = Field(min_length=1)
    observation: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_s < self.start_s:
            raise ValueError("Observation interval is reversed")
        return self


class RenderReviewPacket(FrozenModel):
    execution_id: str
    approval_id: str
    draft_id: str
    draft_revision: int
    draft_hash: str
    job_id: str
    variant_id: str
    requirement_generation: str
    published_attempt: str
    original_request: str
    binding_json: str
    binding_digest: str
    approved_plan_json: str
    recipe_json: str
    recipe_digest: str
    contract_json: str
    verified_artifact: VerifiedArtifactReceipt
    observations: tuple[TimestampedObservation, ...]
    evidence_coverage: tuple[
        Literal["provider_observation", "direct_frame", "actual_render_bytes"], ...
    ]
    review_model_received_visual_input: Literal[False] = False
    packet_digest: str


class CaptureResult(FrozenModel):
    status: Literal["ready", "unavailable"]
    reason: str | None = None
    packet: RenderReviewPacket | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "ready" and (self.packet is None or self.reason is not None):
            raise ValueError("Ready capture requires a packet and no refusal reason")
        if self.status == "unavailable" and (not self.reason or self.packet is not None):
            raise ValueError("Unavailable capture requires a reason and no packet")
        return self


@dataclass(frozen=True)
class ReviewRows:
    plan: ContentPlan
    item: PlanItem
    job: Job
    thread: CreationThread
    session: CreatorAgentSession
    turn: CreatorAgentTurn
    draft: CreatorEditDraft
    approval: CreatorAgentApproval
    execution: CreatorAgentExecution
    prepared: CreatorPreparedExecution | None = None


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def build_render_review_context(
    rows: ReviewRows,
    *,
    verified_artifact: VerifiedArtifactReceipt | None,
    observations: tuple[TimestampedObservation, ...] = (),
) -> CaptureResult:
    if verified_artifact is None:
        return CaptureResult(status="unavailable", reason="artifact_verification_missing")
    try:
        p, i, j, t, s, turn, d, a, e = (
            rows.plan,
            rows.item,
            rows.job,
            rows.thread,
            rows.session,
            rows.turn,
            rows.draft,
            rows.approval,
            rows.execution,
        )
        _require(
            t.runtime_version == 2
            and t.active_plan_item_id == i.id
            and t.active_creator_agent_session_id == s.id
            and i.content_plan_id == p.id
            and p.user_id
            == j.user_id
            == s.creator_id
            == t.creator_id
            == d.creator_id
            == a.creator_id
            and i.current_job_id == j.id == s.target_job_id == e.target_job_id
            and j.content_plan_item_id == i.id == s.plan_item_id == d.item_id
            and p.ownership_epoch
            == s.ownership_epoch
            == j.content_plan_ownership_epoch
            == e.target_ownership_epoch
            == a.target_ownership_epoch,
            "ownership_changed",
        )
        _require(
            e.tool_name == "render.request"
            and e.status in {"dispatched", "completed"}
            and e.target_thread_id == t.id == turn.thread_id == d.thread_id == a.thread_id
            and e.session_id == s.id == turn.session_id == a.session_id
            and e.turn_id == turn.id == a.turn_id
            and e.target_draft_id == d.id == a.draft_id
            and e.target_draft_revision == d.draft_revision == a.draft_revision
            and a.status in {"approved", "consumed"}
            and str(e.id) in a.execution_ids
            and (e.result or {}).get("approval_id") == str(a.id),
            "approval_changed",
        )
        document = KriaDraftDocument.model_validate(d.snapshot_json)
        _, draft_hash = canonical_snapshot(document)
        _require(draft_hash == d.snapshot_hash, "draft_changed")
        binding = BriefBinding.model_validate((e.result or {}).get("brief_binding"))
        _require(
            document.brief_binding is not None
            and document.brief_binding.digest == binding.digest
            and binding.thread_id == str(t.id)
            and binding.state == "pinned"
            and bool(binding.creator_request.strip()),
            "approved_binding_missing_or_changed",
        )
        variant_id = e.target_variant_id
        _require(
            bool(variant_id)
            and d.variant_key == variant_id == a.target_variant_id
            and a.target_job_id == j.id,
            "variant_changed",
        )
        variant = next(v for v in j.assembly_plan["variants"] if v.get("variant_id") == variant_id)
        record = device_record(j, variant_id)
        status = device_status(j, variant_id)
        identity = status.request.identity
        generation = record.get("requirement_generation")
        _require(
            status.phase == "published"
            and variant.get("render_status") == "ready"
            and variant.get("render_generation_id")
            == record.get("published_attempt")
            == status.published_generation
            and generation == e.target_generation_id
            and bool(generation),
            "publication_changed",
        )
        digest = _recipe_digest(status.request.recipe)
        _require(
            str(identity.job_id) == str(j.id)
            and identity.variant_id == variant_id
            and identity.recipe_digest == digest == record.get("recipe_digest")
            and identity.recipe_revision
            == (e.result or {}).get("editor_prep", {}).get("device_recipe_revision"),
            "recipe_changed",
        )
        contract = read_render_contract({CONTRACT_FIELD: record.get("contract_snapshot")})
        expected = build_render_contract({}, generation_id=generation, brief=binding.brief)
        _require(
            contract is not None
            and contract.generation_id == generation
            and contract.brief_digest == expected.brief_digest
            and contract.digest == record.get("contract_digest"),
            "contract_changed",
        )
        if a.prepared_execution_id is not None:
            prepared = rows.prepared
            _require(
                prepared is not None
                and prepared.id == a.prepared_execution_id
                and prepared.invalidated_at is None,
                "prepared_execution_missing",
            )
            snapshot = PreparedExecutionSnapshot.model_validate(prepared.snapshot)
            _require(
                snapshot.digest == prepared.snapshot_digest
                and snapshot.approval_id == str(a.id)
                and snapshot.draft_id == str(d.id)
                and snapshot.draft_revision == d.draft_revision
                and snapshot.draft_snapshot_hash == draft_hash
                and snapshot.binding_digest == binding.digest
                and snapshot.creator_id == str(t.creator_id)
                and snapshot.thread_id == str(t.id),
                "prepared_execution_changed",
            )
        proof = verified_artifact
        attempt = record.get("attempts", {}).get(record["published_attempt"], {})
        _require(
            proof.execution_id == str(e.id)
            and proof.job_id == str(j.id)
            and proof.variant_id == variant_id
            and proof.published_attempt == record["published_attempt"]
            and proof.object_path == variant.get("video_path") == attempt.get("path")
            and proof.sha256 == attempt.get("sha256")
            and proof.byte_count == attempt.get("size")
            and proof.recipe_digest == digest,
            "verified_artifact_changed",
        )
        _require(
            all(o.artifact_sha256 == proof.sha256 for o in observations)
            and len({o.evidence_id for o in observations}) == len(observations),
            "observation_artifact_changed",
        )
        data = dict(
            execution_id=str(e.id),
            approval_id=str(a.id),
            draft_id=str(d.id),
            draft_revision=d.draft_revision,
            draft_hash=draft_hash,
            job_id=str(j.id),
            variant_id=variant_id,
            requirement_generation=generation,
            published_attempt=proof.published_attempt,
            original_request=binding.creator_request,
            binding_json=binding.model_dump_json(),
            binding_digest=binding.digest,
            approved_plan_json=_json(d.snapshot_json),
            recipe_json=status.request.recipe.model_dump_json(),
            recipe_digest=digest,
            contract_json=contract.model_dump_json(),
            verified_artifact=proof.model_dump(mode="json"),
            observations=[o.model_dump(mode="json") for o in observations],
            evidence_coverage=sorted({o.source for o in observations}),
            review_model_received_visual_input=False,
        )
        return CaptureResult(
            status="ready",
            packet=RenderReviewPacket(
                **data, packet_digest=hashlib.sha256(_json(data).encode()).hexdigest()
            ),
        )
    except (ValueError, TypeError, KeyError, AttributeError, StopIteration) as exc:
        return CaptureResult(
            status="unavailable",
            reason=str(exc) if type(exc) is ValueError else "capture_inputs_missing_or_malformed",
        )


def capture_render_review_context(
    execution_id: uuid.UUID,
    *,
    verified_artifact: VerifiedArtifactReceipt | None,
    observations: tuple[TimestampedObservation, ...] = (),
) -> CaptureResult:
    """Capture a coherent read-only database snapshot; close before any future review call."""
    if verified_artifact is None:
        return CaptureResult(status="unavailable", reason="artifact_verification_missing")
    with sync_session() as db:
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        db.execute(text("SET TRANSACTION READ ONLY"))
        try:
            e = db.get(CreatorAgentExecution, execution_id)
            a = db.get(CreatorAgentApproval, uuid.UUID(e.result["approval_id"]))
            s = db.get(CreatorAgentSession, e.session_id)
            i = db.get(PlanItem, s.plan_item_id)
            rows = ReviewRows(
                plan=db.get(ContentPlan, i.content_plan_id),
                item=i,
                job=db.get(Job, e.target_job_id),
                thread=db.get(CreationThread, e.target_thread_id),
                session=s,
                turn=db.get(CreatorAgentTurn, e.turn_id),
                draft=db.get(CreatorEditDraft, e.target_draft_id),
                approval=a,
                execution=e,
                prepared=db.get(CreatorPreparedExecution, a.prepared_execution_id)
                if a.prepared_execution_id
                else None,
            )
            return build_render_review_context(
                rows, verified_artifact=verified_artifact, observations=observations
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            return CaptureResult(status="unavailable", reason="capture_inputs_missing_or_malformed")
