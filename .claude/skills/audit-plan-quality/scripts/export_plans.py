#!/usr/bin/env python3
"""Mode B: read-only export of recent real content plans for auditing.

SELECTs the most recent ContentPlans with their items + source persona and writes
the same plans JSON shape generate_plans.py produces, so the grader is mode-blind.
NO mutation — only SELECTs. Safe to point at prod via `fly proxy 5432 -a nova-video`.

Run from src/apps/api with the shared test venv + DATABASE_URL (repo-root .env, or
the proxied prod URL):

    cd src/apps/api && /Users/.../.venv-test/bin/python \
      ../../../.claude/skills/audit-plan-quality/scripts/export_plans.py \
      --limit 40 --out /tmp/plan-audit/plans.json
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _envload import load_dotenv  # noqa: E402


# Render-state derivation mirrors app/routes/plan_items.py::derive_item_status.
_READY = {"variants_ready", "variants_ready_partial", "done", "clips_ready"}
_FAILED = {
    "variants_failed", "matching_failed", "no_labeled_tracks",
    "processing_failed", "posting_failed", "cancelled",
}


def _derive_status(item) -> str:
    job = item.current_job
    if job is None:
        return item.item_status
    if job.status in _READY:
        return "ready"
    if job.status in _FAILED:
        return "failed"
    return "generating"


async def _run(limit: int, out_path: Path) -> int:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.database import AsyncSessionLocal
    from app.models import ContentPlan, PlanItem

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ContentPlan)
            .options(
                selectinload(ContentPlan.items).selectinload(PlanItem.current_job),
                selectinload(ContentPlan.persona),
            )
            .order_by(ContentPlan.created_at.desc())
            .limit(limit)
        )
        rows = result.scalars().all()

        plans = []
        for cp in rows:
            persona_blob = (cp.persona.persona if cp.persona else None) or {}
            items = [
                {
                    "day_index": it.day_index,
                    "theme": it.theme,
                    "idea": it.idea,
                    "filming_suggestion": it.filming_suggestion or "",
                    "rationale": it.rationale or "",
                    "render_status": _derive_status(it),
                }
                for it in sorted(cp.items, key=lambda x: x.day_index)
            ]
            plans.append(
                {
                    "persona_id": str(cp.id),
                    "is_control": False,
                    "persona": persona_blob,
                    "plan_status": cp.plan_status,
                    "prompt_version": cp.prompt_version,
                    "items": items,
                    "error": None,
                }
            )

    out = {
        "mode": "prod",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "plans": plans,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    n_items = sum(len(p["items"]) for p in plans)
    print(f"Exported {len(plans)} plans ({n_items} items) → {out_path}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Export recent real content plans (read-only).")
    ap.add_argument("--limit", type=int, default=40, help="Most recent N plans (default 40).")
    ap.add_argument("--out", required=True, help="Where to write the plans JSON.")
    args = ap.parse_args()

    sys.path.insert(0, os.getcwd())
    load_dotenv()
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set. Local: repo-root .env. Prod: fly proxy + set it.", file=sys.stderr)
        return 2
    return asyncio.run(_run(args.limit, Path(args.out).resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
