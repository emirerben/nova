"""Export agent eval fixtures from the local/prod database.

Walks `VideoTemplate.recipe_cached` rows and writes one JSON fixture per template
under `tests/fixtures/agent_evals/{template_recipe,creative_direction}/prod_snapshots/`.

Each fixture is a self-contained replay payload:
    {
      "agent": "nova.compose.template_recipe",
      "prompt_version": "exported",
      "input": {...},
      "raw_text": "<json-serialized recipe>",
      "output": {...},
      "meta": {"template_id": "...", "template_name": "...", "exported_at": "..."}
    }

Each fixture is run through the agent + structural checks before being written.
Fixtures that fail structural validation are SKIPPED and logged — they represent
real quality issues in the source row (e.g. creative_direction strings shorter
than 50 words from templates analyzed before two-pass mode shipped). Re-run the
agent on those templates to repopulate `recipe_cached`, then re-export.

Pass `--include-failing` to write them anyway (useful for testing the eval).

Note on clip_metadata: best_moments are not persisted in JobClip / Job.all_candidates
(only the chosen window is). To produce clip_metadata fixtures, run the agent
live on a sample clip via `--eval-mode=live` and capture the recording, OR add
hand-crafted fixtures under `tests/fixtures/agent_evals/clip_metadata/golden/`.

Run:  cd src/apps/api && .venv/bin/python scripts/export_eval_fixtures.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# Bootstrap imports when run directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.models import VideoTemplate  # noqa: E402

FIXTURES_ROOT = (
    Path(__file__).parent.parent / "tests" / "fixtures" / "agent_evals"
)


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_") or "unnamed"


def _build_template_recipe_fixture(tpl: VideoTemplate) -> dict | None:
    recipe = tpl.recipe_cached
    if not isinstance(recipe, dict) or not recipe.get("slots"):
        return None
    raw_text = json.dumps(recipe)
    return {
        "agent": "nova.compose.template_recipe",
        "prompt_version": "exported",
        "input": {
            "file_uri": tpl.gcs_path or f"templates/{tpl.id}/reference.mp4",
            "file_mime": "video/mp4",
            "analysis_mode": "single",
            "creative_direction": "",
            "black_segments": [],
        },
        "raw_text": raw_text,
        "output": recipe,
        "meta": {
            "source": "VideoTemplate.recipe_cached",
            "template_id": tpl.id,
            "template_name": tpl.name,
            "exported_at": datetime.now(UTC).isoformat(),
        },
    }


def _build_creative_direction_fixture(tpl: VideoTemplate) -> dict | None:
    recipe = tpl.recipe_cached
    if not isinstance(recipe, dict):
        return None
    text = (recipe.get("creative_direction") or "").strip()
    if not text:
        return None
    return {
        "agent": "nova.compose.creative_direction",
        "prompt_version": "exported",
        "input": {
            "file_uri": tpl.gcs_path or f"templates/{tpl.id}/reference.mp4",
            "file_mime": "video/mp4",
        },
        "raw_text": text,
        "output": {"text": text},
        "meta": {
            "source": "VideoTemplate.recipe_cached.creative_direction",
            "template_id": tpl.id,
            "template_name": tpl.name,
            "exported_at": datetime.now(UTC).isoformat(),
        },
    }


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))


def _validate_payload(payload: dict) -> list[str]:
    """Run the same structural checks the eval suite would. Empty list = pass."""
    # Local imports so this script can run without the eval test dependencies
    # in environments where tests/ isn't on the path.
    from tests.evals.runners.eval_runner import (  # noqa: PLC0415
        CassetteModelClient,
        Fixture,
        run_eval,
    )

    fixture = Fixture(
        path=Path("/dev/null"),
        agent=payload["agent"],
        prompt_version=payload.get("prompt_version", ""),
        input=payload["input"],
        raw_text=payload["raw_text"],
        output=payload.get("output", {}),
        meta=payload.get("meta", {}),
    )
    result = run_eval(fixture, model_client=CassetteModelClient(payload["raw_text"]))
    if result.error:
        return [result.error]
    return result.structural_failures


async def export_all(
    *, only: str | None = None, dry_run: bool = False, include_failing: bool = False
) -> dict[str, int]:
    counts = {"template_recipe": 0, "creative_direction": 0, "skipped": 0, "rejected": 0}
    async with AsyncSessionLocal() as session:
        stmt = select(VideoTemplate).where(VideoTemplate.analysis_status == "ready")
        result = await session.execute(stmt)
        templates = result.scalars().all()

    for tpl in templates:
        slug = _slug(tpl.name)

        if only in (None, "template_recipe"):
            payload = _build_template_recipe_fixture(tpl)
            if payload is None:
                counts["skipped"] += 1
            else:
                failures = [] if include_failing else _validate_payload(payload)
                if failures:
                    print(f"[reject] template_recipe/{slug}: {failures[0]}")
                    counts["rejected"] += 1
                else:
                    target = (
                        FIXTURES_ROOT / "template_recipe" / "prod_snapshots" / f"{slug}.json"
                    )
                    if dry_run:
                        print(f"[dry-run] would write {target}")
                    else:
                        _write(target, payload)
                    counts["template_recipe"] += 1

        if only in (None, "creative_direction"):
            payload = _build_creative_direction_fixture(tpl)
            if payload is None:
                # Templates analyzed before two-pass mode landed don't have this field.
                continue
            failures = [] if include_failing else _validate_payload(payload)
            if failures:
                print(f"[reject] creative_direction/{slug}: {failures[0]}")
                counts["rejected"] += 1
                continue
            target = (
                FIXTURES_ROOT / "creative_direction" / "prod_snapshots" / f"{slug}.json"
            )
            if dry_run:
                print(f"[dry-run] would write {target}")
            else:
                _write(target, payload)
            counts["creative_direction"] += 1

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        choices=["template_recipe", "creative_direction"],
        default=None,
        help="Export only one agent's fixtures (default: both).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print targets without writing."
    )
    parser.add_argument(
        "--include-failing",
        action="store_true",
        help="Write fixtures even if they fail structural validation (debug only).",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL", "<not set>")
    print(f"Reading from DATABASE_URL host: {db_url.split('@')[-1].split('/')[0]}")

    counts = asyncio.run(
        export_all(
            only=args.only,
            dry_run=args.dry_run,
            include_failing=args.include_failing,
        )
    )
    print(
        f"\nExported: {counts['template_recipe']} template_recipe, "
        f"{counts['creative_direction']} creative_direction "
        f"(skipped {counts['skipped']} with empty recipes, "
        f"rejected {counts['rejected']} that failed structural validation)"
    )
    print(f"Output: {FIXTURES_ROOT}")


if __name__ == "__main__":
    main()
