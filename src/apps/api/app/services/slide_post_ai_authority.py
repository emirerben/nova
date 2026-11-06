"""Admission lineage for prepared SlidePost AI; no current inputs are substituted."""

from __future__ import annotations

import copy

from sqlalchemy import select

from app.models import PlanItemAsset


async def authority_snapshot(db, item, plan, thread, *, lock_sources=False):
    statement = (
        select(PlanItemAsset)
        .where(PlanItemAsset.plan_item_id == item.id)
        .order_by(PlanItemAsset.id)
        .execution_options(populate_existing=True)
    )
    if lock_sources:
        statement = statement.with_for_update(read=True, nowait=True)
    assets = (await db.scalars(statement)).all()
    return copy.deepcopy(
        {
            "plan_id": str(plan.id),
            "owner_id": str(plan.user_id),
            "ownership_epoch": int(plan.ownership_epoch or 0),
            "item_id": str(item.id),
            "project_id": str(thread.id),
            "thread_status": thread.status,
            "thread_state": thread.state,
            "active_item_id": str(thread.active_plan_item_id),
            "draft": item.slide_post,
            "format": item.edit_format,
            "theme": item.theme,
            "idea": item.idea,
            "notes": item.notes,
            "assets": [
                {
                    name: (str(value) if name.endswith("id") and value is not None else value)
                    for name in (
                        "id",
                        "user_id",
                        "plan_item_id",
                        "kind",
                        "media_status",
                        "gcs_path",
                        "gcs_generation",
                        "library_asset_id",
                        "library_source_id",
                        "analysis",
                        "user_context",
                    )
                    for value in [getattr(asset, name)]
                }
                for asset in assets
            ],
        }
    )
