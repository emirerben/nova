"""One-off: re-run analyze_template_task on templates with under-baked recipes.

Use case: 10 of 14 prod templates failed the creative_direction 50-word floor
when Phase 1 fixture-export ran. Their `recipe_cached` was produced by an
earlier template-recipe agent revision, so creative_direction is essentially
absent.

This script:
  1. Resolves a list of template ids or slugs from CLI args (or auto-detects
     by walking VideoTemplate rows whose recipe_cached.creative_direction is
     under the 50-word floor when --auto-detect is passed).
  2. Snapshots the current recipe_cached JSON to
     /tmp/template_recipe_backup_<slug>.json so any operator-set fields can be
     restored if reanalysis regresses them. (See Reanalysis-preserves-overrides
     TODO.)
  3. Dispatches `analyze_template_task.delay(template_id)` for each. The task
     uses single-pass mode (the production default since Phase 2).
  4. With --watch, polls until every dispatched template reaches `ready` or
     `failed`.

Run:
    cd src/apps/api
    .venv/bin/python scripts/reanalyze_underbaked_templates.py --auto-detect --watch
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.models import VideoTemplate  # noqa: E402

CREATIVE_DIRECTION_WORD_FLOOR = 50
BACKUP_DIR = Path("/tmp")
POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 30 * 60


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in (name or "").lower()).strip("_") or "unnamed"


async def _resolve_templates(
    session, requested: list[str], auto_detect: bool
) -> list[VideoTemplate]:
    if auto_detect:
        all_ready = (
            (
                await session.execute(
                    select(VideoTemplate).where(VideoTemplate.analysis_status == "ready")
                )
            )
            .scalars()
            .all()
        )
        candidates: list[VideoTemplate] = []
        for tpl in all_ready:
            recipe = tpl.recipe_cached or {}
            if not isinstance(recipe, dict):
                continue
            text = (recipe.get("creative_direction") or "").strip()
            if len(text.split()) < CREATIVE_DIRECTION_WORD_FLOOR:
                candidates.append(tpl)
        return candidates

    if not requested:
        return []
    by_id = (
        (await session.execute(select(VideoTemplate).where(VideoTemplate.id.in_(requested))))
        .scalars()
        .all()
    )
    found_ids = {t.id for t in by_id}
    missing = [r for r in requested if r not in found_ids]
    if missing:
        all_templates = (await session.execute(select(VideoTemplate))).scalars().all()
        slug_lookup = {_slug(t.name): t for t in all_templates}
        for slug in missing:
            tpl = slug_lookup.get(slug)
            if tpl and tpl.id not in found_ids:
                by_id.append(tpl)
                found_ids.add(tpl.id)
    return list(by_id)


def _backup(tpl: VideoTemplate) -> Path:
    payload = {
        "template_id": tpl.id,
        "template_name": tpl.name,
        "snapshot_taken_at": datetime.now(UTC).isoformat(),
        "recipe_cached": tpl.recipe_cached,
        "analysis_status": tpl.analysis_status,
    }
    target = BACKUP_DIR / f"template_recipe_backup_{_slug(tpl.name)}.json"
    target.write_text(json.dumps(payload, indent=2, default=str))
    return target


def _dispatch(template_id: str) -> None:
    from app.tasks.template_orchestrate import analyze_template_task  # noqa: PLC0415

    analyze_template_task.delay(template_id)


async def _poll_status(template_ids: list[str]) -> dict[str, str]:
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while True:
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(VideoTemplate.id, VideoTemplate.analysis_status).where(
                        VideoTemplate.id.in_(template_ids)
                    )
                )
            ).all()
        statuses = {tid: status for tid, status in rows}
        terminal = {tid for tid, s in statuses.items() if s in ("ready", "failed")}
        pending = {tid for tid in template_ids if tid not in terminal}
        print(
            f"  [{datetime.now(UTC).strftime('%H:%M:%S')}] "
            f"ready={len(terminal)}/{len(template_ids)} pending={len(pending)}"
        )
        if not pending:
            return statuses
        if time.monotonic() > deadline:
            print(f"  timeout after {POLL_TIMEOUT_S}s; pending: {pending}")
            return statuses
        await asyncio.sleep(POLL_INTERVAL_S)


async def run(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as session:
        templates = await _resolve_templates(session, args.templates, args.auto_detect)

    if not templates:
        print("No templates resolved. Pass ids/slugs as args or --auto-detect.")
        return 1

    print(f"Resolved {len(templates)} template(s):")
    for tpl in templates:
        print(f"  - {tpl.id} ({tpl.name})")

    if args.dry_run:
        print("\n[dry-run] would snapshot + dispatch each. Exiting without changes.")
        return 0

    print("\nSnapshotting recipe_cached to /tmp/template_recipe_backup_<slug>.json …")
    for tpl in templates:
        path = _backup(tpl)
        print(f"  - {path}")

    print("\nDispatching analyze_template_task for each (single-pass mode, Phase 2 default) …")
    dispatched_ids: list[str] = []
    for tpl in templates:
        _dispatch(tpl.id)
        dispatched_ids.append(tpl.id)
        print(f"  - dispatched {tpl.id}")

    if args.watch:
        print(f"\nPolling status every {POLL_INTERVAL_S}s (timeout {POLL_TIMEOUT_S}s)…")
        statuses = await _poll_status(dispatched_ids)
        print("\nFinal statuses:")
        for tid, s in statuses.items():
            print(f"  - {tid}: {s}")
        failed = [tid for tid, s in statuses.items() if s != "ready"]
        if failed:
            print(f"\n{len(failed)} template(s) did NOT reach ready: {failed}")
            return 2
    else:
        print("\nDone dispatching. Run with --watch to poll until completion,")
        print("or check `fly logs -a nova-video --process-group worker` to monitor progress.")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "templates",
        nargs="*",
        help=(
            "Template ids or slugs to reanalyze (e.g. 'morocco impressing_myself'). "
            "Slugs are resolved by lowercasing the template name."
        ),
    )
    parser.add_argument(
        "--auto-detect",
        action="store_true",
        help=(
            f"Auto-pick all `ready` templates whose recipe_cached.creative_direction "
            f"has fewer than {CREATIVE_DIRECTION_WORD_FLOOR} words."
        ),
    )
    parser.add_argument(
        "--watch", action="store_true", help="Poll status until all reach ready/failed."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Resolve and report; do not dispatch."
    )
    args = parser.parse_args()

    rc = asyncio.run(run(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
