"""Ephemeral, actor-owned source context for order replacement readers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.kria.order_replacement import OrderReplacementProof, OrderReplacementProposal
    from app.kria.prepared_execution import SourceIdentity
    from app.services.phone_sources import PhoneSourceBinding


@dataclass(frozen=True)
class OrderReplacementSourceContext:
    sources: tuple[SourceIdentity, ...]
    phone_bindings: tuple[PhoneSourceBinding, ...]
    creator_id: str


def load_brief_order_source_context_sync(db, *, brief, item_id, creator_id):
    proofs = (
        [req.order_replacement for req in brief.live() if req.order_replacement] if brief else []
    )
    if len(proofs) != 1 or item_id is None:
        return None
    try:
        return load_order_replacement_source_context_sync(
            db, item_id=item_id, creator_id=creator_id, proposal=proofs[0]
        )
    except ValueError:
        return None


def _proposal(value: OrderReplacementProof | OrderReplacementProposal):
    from app.kria.order_replacement import OrderReplacementProof, OrderReplacementProposal

    if isinstance(value, OrderReplacementProof):
        value = value.proposal
    if not isinstance(value, OrderReplacementProposal):
        raise ValueError("Order source context requires a typed proposal or proof")
    return value


def _item_query(item_id: Any, creator_id: Any):
    from sqlalchemy import select

    from app.models import ContentPlan, PlanItem

    return (
        select(PlanItem)
        .join(ContentPlan, PlanItem.content_plan_id == ContentPlan.id)
        .where(
            PlanItem.id == uuid.UUID(str(item_id)),
            ContentPlan.user_id == uuid.UUID(str(creator_id)),
        )
        .execution_options(populate_existing=True)
    )


def _visual_query(item: Any, creator_id: Any, proposal: Any):
    from sqlalchemy import select

    from app.models import PlanItemAsset

    ids = set()
    for moment in proposal.moments:
        if moment.source_slot.startswith("visual:"):
            asset_id = uuid.UUID(moment.source_slot.removeprefix("visual:"))
            if moment.source_slot != f"visual:{asset_id}" or moment.media_id != f"asset-{asset_id}":
                raise ValueError("Order visual identity is not canonical")
            ids.add(asset_id)
    return select(PlanItemAsset).where(
        PlanItemAsset.plan_item_id == item.id,
        PlanItemAsset.user_id == uuid.UUID(str(creator_id)),
        PlanItemAsset.id.in_(ids),
    ).execution_options(populate_existing=True), ids


def _context(item: Any, creator_id: Any, proposal: Any, sources: list, assets: list, ids: set):
    from app.kria.media_sources import is_analysis_proxy_path
    from app.kria.prepared_execution import SourceIdentity
    from app.services.phone_sources import bind_phone_sources

    if {asset.id for asset in assets} != ids:
        raise ValueError("Order visual is not attached to this creator's item")
    kinds = {}
    assignments = item.clip_assignments or []
    selected_slots = {moment.source_slot for moment in proposal.moments}
    for source in sources:
        if source.slot.startswith("clip:") and source.slot in selected_slots:
            rows = [
                r
                for r in assignments
                if isinstance(r, dict) and r.get("gcs_path", r.get("storage_path")) == source.path
            ]
            if len(rows) != 1:
                raise ValueError("Order clip requires one current assignment")
            kinds[source.slot] = rows[0].get("kind", "video")
    for asset in assets:
        if is_analysis_proxy_path(asset.gcs_path):
            raise ValueError("Order visual requires an original source, not a proxy")
        if asset.kind not in {"image", "video"} or not asset.gcs_generation:
            raise ValueError("Order visual lacks a current kind or generation")
        if asset.media_status != "ready":
            raise ValueError("Order visual media is unavailable")
        source = SourceIdentity(
            slot=f"visual:{asset.id}",
            media_id=f"asset-{asset.id}",
            path=asset.gcs_path,
            generation=str(asset.gcs_generation),
            role="original",
            duration_s=asset.duration_s,
        )
        sources.append(source)
        kinds[source.slot] = asset.kind
    by_slot = {source.slot: source for source in sources}
    proxy_paths = []
    for moment in proposal.moments:
        source = by_slot.get(moment.source_slot)
        if source is None or kinds.get(moment.source_slot) != moment.kind:
            raise ValueError("Order source slot or kind is no longer available")
        if source.role == "analysis_proxy" and source.path not in proxy_paths:
            proxy_paths.append(source.path)
    bindings = bind_phone_sources(assignments, proxy_paths) if proxy_paths else ()
    return OrderReplacementSourceContext(tuple(sources), bindings, str(creator_id))


def load_order_replacement_source_context_sync(
    db: Any,
    *,
    item_id: Any,
    creator_id: Any,
    proposal: OrderReplacementProof | OrderReplacementProposal,
) -> OrderReplacementSourceContext:
    from app.kria.prepared_execution import load_source_identities_sync

    proposal = _proposal(proposal)
    item = db.execute(_item_query(item_id, creator_id)).scalar_one_or_none()
    if item is None:
        raise ValueError("Order source item is not owned by this creator")
    query, ids = _visual_query(item, creator_id, proposal)
    assets = list(db.execute(query).scalars())
    sources = load_source_identities_sync(db, item, uuid.UUID(str(creator_id)))
    return _context(item, creator_id, proposal, sources, assets, ids)


async def load_order_replacement_source_context(
    db: Any,
    *,
    item_id: Any,
    creator_id: Any,
    proposal: OrderReplacementProof | OrderReplacementProposal,
) -> OrderReplacementSourceContext:
    from app.kria.prepared_execution import load_source_identities

    proposal = _proposal(proposal)
    item = (await db.execute(_item_query(item_id, creator_id))).scalar_one_or_none()
    if item is None:
        raise ValueError("Order source item is not owned by this creator")
    query, ids = _visual_query(item, creator_id, proposal)
    assets = list((await db.execute(query)).scalars())
    sources = await load_source_identities(db, item, uuid.UUID(str(creator_id)))
    return _context(item, creator_id, proposal, sources, assets, ids)
