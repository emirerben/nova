"""Exact approval lineage for reviewing a strategy's initial native output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.kria import prepared_execution as prepared
from app.kria.brief_binding import BriefBinding
from app.kria.drafts import KriaDraftDocument, canonical_snapshot
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    build_render_contract,
    read_render_contract,
)
from app.services.device_render import device_record, device_status, verify_device_record_contract
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding, bind_phone_sources

if TYPE_CHECKING:
    from app.kria.render_review_context import ReviewRows


@dataclass(frozen=True)
class InitialRenderAuthority:
    job_id: str
    variant_id: str
    requirement_generation: str
    published_attempt: str
    recipe_revision: int
    recipe_digest: str
    binding_digest: str
    prepared_id: str
    prepared_attempt_id: str


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def resolve_initial_render_authority(rows: ReviewRows) -> InitialRenderAuthority:
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
    document = KriaDraftDocument.model_validate(d.snapshot_json)
    _require(document.kind == "strategy", "initial_review_requires_strategy")
    _require(
        t.runtime_version == 2
        and t.active_plan_item_id == i.id
        and t.active_creator_agent_session_id == s.id
        and i.content_plan_id == p.id
        and p.user_id == j.user_id == s.creator_id == t.creator_id == d.creator_id == a.creator_id
        and i.current_job_id == j.id == s.target_job_id == e.target_job_id
        and j.content_plan_item_id == i.id == s.plan_item_id == d.item_id
        and p.ownership_epoch
        == s.ownership_epoch
        == j.content_plan_ownership_epoch
        == e.target_ownership_epoch
        == a.target_ownership_epoch,
        "initial_review_ownership_changed",
    )
    _require(
        e.tool_name == "render.request"
        and e.status == "completed"
        and a.status == "consumed"
        and e.target_thread_id == t.id == turn.thread_id == d.thread_id == a.thread_id
        and e.session_id == s.id == turn.session_id == a.session_id
        and e.turn_id == turn.id == a.turn_id
        and str(e.id) in a.execution_ids
        and e.target_draft_id == d.id == a.draft_id
        and e.target_draft_revision == d.draft_revision == a.draft_revision,
        "initial_review_approval_changed",
    )
    result = e.result or {}
    _require(
        result.get("approval_id") == str(a.id)
        and result.get("job_id") == str(j.id)
        and e.external_task_id == str(j.id)
        and result.get("dispatch_outcome") in {"dispatched", "already_active"}
        and result.get("outcome") == "ready"
        and not result.get("editor_prep"),
        "initial_review_dispatch_receipt_changed",
    )
    _, draft_hash = canonical_snapshot(document)
    _require(draft_hash == d.snapshot_hash, "initial_review_draft_changed")
    binding = BriefBinding.model_validate(result.get("brief_binding"))
    binding.resolve(t.id)
    job_binding = BriefBinding.model_validate((j.assembly_plan or {}).get("creator_brief_binding"))
    job_binding.resolve(t.id)
    _require(
        binding.state == "pinned"
        and bool(binding.creator_request.strip())
        and document.brief_binding is not None
        and binding.digest == document.brief_binding.digest == job_binding.digest
        and result.get("creator_request") == binding.creator_request,
        "initial_review_binding_changed",
    )
    variant_id = e.target_variant_id
    _require(
        bool(variant_id) and variant_id == result.get("variant_id") == s.target_variant_id,
        "initial_review_variant_changed",
    )
    variants = [v for v in j.assembly_plan.get("variants", []) if v.get("variant_id") == variant_id]
    _require(len(variants) == 1, "initial_review_variant_ambiguous")
    variant = variants[0]
    record, status = device_record(j, variant_id), device_status(j, variant_id)
    identity = status.request.identity
    generation = j.assembly_plan.get("creator_generation_id")
    publication = record.get("published_attempt")
    _require(
        bool(generation)
        and record.get("requirement_generation") == generation
        and status.phase == "published"
        and variant.get("render_destination") == "device"
        and variant.get("render_status") == "ready"
        and record.get("base_generation") == publication
        and bool(publication)
        and publication
        == status.published_generation
        == variant.get("render_generation_id")
        == e.target_generation_id
        == result.get("render_generation_id")
        == s.target_generation_id
        and identity.job_id == j.id
        and identity.variant_id == variant_id,
        "initial_review_publication_changed",
    )
    row = rows.prepared
    _require(
        row is not None and a.prepared_execution_id == row.id and row.invalidated_at is None,
        "initial_review_prepared_missing",
    )
    snapshot = prepared.parse(row)
    _require(
        snapshot.approval_id == str(a.id)
        and snapshot.draft_id == str(d.id)
        and snapshot.draft_revision == d.draft_revision
        and snapshot.draft_snapshot_hash == draft_hash
        and snapshot.creator_id == str(t.creator_id)
        and snapshot.thread_id == str(t.id)
        and snapshot.draft_kind == "strategy"
        and snapshot.binding_digest == binding.digest,
        "initial_review_prepared_changed",
    )
    receipt = result.get(prepared.RESULT_FIELD) or {}
    stamp = prepared.variant_stamp(j.assembly_plan, variant_id) or {}
    _require(
        receipt.get("id") == stamp.get("id") == str(row.id)
        and receipt.get("digest") == stamp.get("digest") == snapshot.digest
        and bool(receipt.get("attempt_id"))
        and receipt.get("attempt_id") == stamp.get("attempt_id")
        and receipt.get("approval_id") == stamp.get("approval_id") == str(a.id)
        and receipt.get("draft_id") == stamp.get("draft_id") == str(d.id)
        and receipt.get("draft_revision") == stamp.get("draft_revision") == d.draft_revision
        and receipt.get("binding_digest") == stamp.get("binding_digest") == binding.digest
        and stamp.get("generation_id") == generation
        and record.get("prepared_execution") == prepared.variant_fence(j.assembly_plan, variant_id),
        "initial_review_attempt_changed",
    )
    contract = read_render_contract({CONTRACT_FIELD: record.get("contract_snapshot")})
    expected = build_render_contract({}, generation_id=generation, brief=binding.brief)
    _require(
        contract is not None
        and contract.generation_id == generation
        and contract.brief_digest == expected.brief_digest
        and contract.digest == record.get("contract_digest") == stamp.get("contract_digest"),
        "initial_review_contract_changed",
    )
    paths = (j.all_candidates or {}).get("clip_paths") or []
    sources = tuple(
        PhoneSourceBinding.model_validate(v) for v in j.assembly_plan.get(PHONE_SOURCES_FIELD, [])
    )
    _require(
        bool(sources) and sources == bind_phone_sources(i.clip_assignments, paths),
        "initial_review_sources_changed",
    )
    current_sources = prepared.source_identities(i, asset_generations={}, library_sources={})
    _require(
        [source.fence() for source in current_sources]
        == [source.fence() for source in snapshot.sources],
        "initial_review_source_windows_changed",
    )
    approved_sources = {source.path: source for source in snapshot.sources}
    _require(
        all(
            source.proxy_path in approved_sources
            and approved_sources[source.proxy_path].media_id == source.media_id
            and approved_sources[source.proxy_path].generation == source.generation
            for source in sources
        ),
        "initial_review_approved_sources_changed",
    )
    verify_device_record_contract(j, record, status)
    return InitialRenderAuthority(
        str(j.id),
        variant_id,
        generation,
        publication,
        identity.recipe_revision,
        identity.recipe_digest,
        binding.digest,
        str(row.id),
        receipt["attempt_id"],
    )
