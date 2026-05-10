"""Backfill `required_inputs` on the prod Dimples Passport Travel Vlog template.

Why this exists separately from seed_dimples_passport_brazil.py:
  - seed_*.py scripts run only in dev (the prod template was created via the
    admin UI, per that seed file's docstring).
  - This script is a one-off, idempotent backfill safe to run once against
    each environment after deploying the substitution-heuristic + seed updates.
  - We deliberately don't bundle this into an Alembic migration: data
    migrations coupled to seed-author intent are fragile and hard to undo.

Run: cd src/apps/api && .venv/bin/python scripts/backfill_dimples_passport_inputs.py --yes
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from urllib.parse import urlparse

# Bootstrap imports when run directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.models import VideoTemplate  # noqa: E402

# Source REQUIRED_INPUTS + TEMPLATE_NAME from the seed so this stays in lockstep
# with the dev seed's contract — single source of truth.
_SEED_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "seed_dimples_passport_brazil.py"
)
_spec = importlib.util.spec_from_file_location("seed_dimples_passport_brazil", _SEED_PATH)
_seed = importlib.util.module_from_spec(_spec)
sys.modules["seed_dimples_passport_brazil"] = _seed
_spec.loader.exec_module(_seed)
TEMPLATE_NAME = _seed.TEMPLATE_NAME
REQUIRED_INPUTS = _seed.REQUIRED_INPUTS


async def backfill() -> int:
    """Set required_inputs on the Dimples Passport row. Returns exit code."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(VideoTemplate).where(VideoTemplate.name == TEMPLATE_NAME)
        )
        row = result.scalars().first()
        if row is None:
            print(
                f"ERROR: template '{TEMPLATE_NAME}' not found in target DB.",
                file=sys.stderr,
            )
            return 1

        if row.required_inputs:
            print(
                f"No-op: '{TEMPLATE_NAME}' already has required_inputs "
                f"({len(row.required_inputs)} keys). Nothing to do."
            )
            return 0

        row.required_inputs = REQUIRED_INPUTS
        await db.commit()
        keys = ", ".join(spec["key"] for spec in REQUIRED_INPUTS)
        print(f"Updated '{TEMPLATE_NAME}' (id={row.id}) required_inputs: [{keys}]")
        return 0


def _confirm_target_db() -> None:
    db_url = os.environ.get("DATABASE_URL", "")
    parsed = urlparse(db_url)
    display = f"{parsed.hostname or '?'}:{parsed.port or '?'}{parsed.path or ''}"
    print(f"Target DB: {display}")
    if "--yes" in sys.argv:
        return
    if not sys.stdin.isatty():
        print("ERROR: non-interactive run requires --yes flag.", file=sys.stderr)
        sys.exit(2)
    answer = input("Proceed? [y/N]: ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)


if __name__ == "__main__":
    _confirm_target_db()
    sys.exit(asyncio.run(backfill()))
