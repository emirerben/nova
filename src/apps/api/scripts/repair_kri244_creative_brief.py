#!/usr/bin/env python3
"""Append-only repair for the one confirmed KRI-244 Creative Brief.

Dry-run is the default. The mutating mode locks the exact creation thread,
requires the completed source turn to remain the thread's only turn, requires
the single known version-1 snapshot byte-for-byte, and appends version 2 with
its false order requirement marked superseded. It never updates or deletes
version 1 and aborts if any production state has drifted.

Run inside the deployed API image after the KRI-244 prompt fix is healthy:

    python scripts/repair_kri244_creative_brief.py
    python scripts/repair_kri244_creative_brief.py --apply \
      --confirm-thread 3cc46656-d596-495b-aff9-ebf59061d07d
"""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from sqlalchemy import select

from app.database import sync_session
from app.models import CreationThread, CreativeBriefVersion, CreatorAgentTurn

THREAD_ID = uuid.UUID("3cc46656-d596-495b-aff9-ebf59061d07d")
SOURCE_TURN_ID = uuid.UUID("cbbb4c9a-6842-410e-be45-d546ff1e921e")
EXPECTED_TURN_STATUS = "completed"
EXPECTED_V1_REQUIREMENTS: list[dict[str, Any]] = [
    {
        "id": "r1",
        "kind": "order",
        "facts": {
            "end": "night cycle",
            "key": "capture_time",
            "start": "sunset walk",
        },
        "scope": "global",
        "status": "open",
        "literal": None,
        "description": "chronological order from sunset walk to night cycle",
        "source_turn_id": str(SOURCE_TURN_ID),
    }
]


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


EXPECTED_V1_SHA256 = hashlib.sha256(_canonical(EXPECTED_V1_REQUIREMENTS).encode()).hexdigest()


def _rows(db, *, lock: bool = False) -> list[CreativeBriefVersion]:  # noqa: ANN001
    statement = (
        select(CreativeBriefVersion)
        .where(CreativeBriefVersion.thread_id == THREAD_ID)
        .order_by(CreativeBriefVersion.version)
    )
    if lock:
        statement = statement.with_for_update()
    return list(db.execute(statement).scalars().all())


def _turns(db) -> list[CreatorAgentTurn]:  # noqa: ANN001
    # Do not take turn locks while holding the thread lock: runtime completion
    # uses turn -> thread lock order. New-turn admission already serializes on
    # the thread row, and this exact known turn is terminal.
    return list(
        db.execute(
            select(CreatorAgentTurn)
            .where(CreatorAgentTurn.thread_id == THREAD_ID)
            .order_by(CreatorAgentTurn.created_at, CreatorAgentTurn.id)
        )
        .scalars()
        .all()
    )


def _assert_turn_state(turns: Sequence[CreatorAgentTurn]) -> CreatorAgentTurn:
    if len(turns) != 1:
        raise RuntimeError(f"expected exactly one creator turn, found {len(turns)}")
    turn = turns[0]
    if turn.id != SOURCE_TURN_ID or turn.thread_id != THREAD_ID:
        raise RuntimeError("creator-turn identity changed")
    if turn.status != EXPECTED_TURN_STATUS:
        raise RuntimeError(
            f"expected source turn status {EXPECTED_TURN_STATUS}, found {turn.status}"
        )
    return turn


def _assert_initial_state(rows: Sequence[CreativeBriefVersion]) -> CreativeBriefVersion:
    if len(rows) != 1:
        raise RuntimeError(f"expected exactly one brief version, found {len(rows)}")
    row = rows[0]
    if row.version != 1:
        raise RuntimeError(f"expected latest brief version 1, found {row.version}")
    if row.thread_id != THREAD_ID or row.source_turn_id != SOURCE_TURN_ID:
        raise RuntimeError("version-1 thread or source-turn identity changed")
    if row.requirements != EXPECTED_V1_REQUIREMENTS:
        actual_hash = hashlib.sha256(_canonical(row.requirements).encode()).hexdigest()
        raise RuntimeError(
            f"version-1 requirements changed (expected {EXPECTED_V1_SHA256}, found {actual_hash})"
        )
    return row


def _version_two_requirements() -> list[dict[str, Any]]:
    requirements = deepcopy(EXPECTED_V1_REQUIREMENTS)
    requirements[0]["status"] = "superseded"
    return requirements


def _assert_repaired_state(rows: Sequence[CreativeBriefVersion]) -> CreativeBriefVersion:
    if len(rows) != 2 or [row.version for row in rows] != [1, 2]:
        raise RuntimeError("repair verification did not find exactly versions 1 and 2")
    _assert_initial_state(rows[:1])
    repaired = rows[1]
    if repaired.thread_id != THREAD_ID or repaired.source_turn_id is not None:
        raise RuntimeError("repair version has an unexpected thread or creator turn")
    if repaired.requirements != _version_two_requirements():
        raise RuntimeError("repair version does not exactly supersede the known requirement")
    if any(req.get("status") != "superseded" for req in repaired.requirements):
        raise RuntimeError("repair verification still contains a live requirement")
    return repaired


def _summary(rows: Sequence[CreativeBriefVersion]) -> dict[str, Any]:
    latest = rows[-1]
    return {
        "thread_id": str(THREAD_ID),
        "versions": [row.version for row in rows],
        "latest_live_requirements": sum(
            1 for req in latest.requirements if req.get("status") != "superseded"
        ),
        "version_1_sha256": EXPECTED_V1_SHA256,
    }


def inspect() -> dict[str, Any]:
    with sync_session() as db:
        _assert_turn_state(_turns(db))
        rows = _rows(db)
        _assert_initial_state(rows)
        return _summary(rows)


def apply() -> dict[str, Any]:
    with sync_session() as db, db.begin():
        thread = db.get(CreationThread, THREAD_ID, with_for_update=True)
        if thread is None:
            raise RuntimeError("target creation thread does not exist")
        _assert_turn_state(_turns(db))
        rows = _rows(db, lock=True)
        _assert_initial_state(rows)
        db.add(
            CreativeBriefVersion(
                thread_id=THREAD_ID,
                version=2,
                requirements=_version_two_requirements(),
                source_turn_id=None,
            )
        )
        db.flush()
        rows = _rows(db, lock=True)
        _assert_repaired_state(rows)
        return _summary(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="append repair version 2")
    parser.add_argument(
        "--confirm-thread",
        help="must equal the exact target thread UUID when --apply is used",
    )
    args = parser.parse_args()
    if args.apply:
        if args.confirm_thread != str(THREAD_ID):
            raise RuntimeError("--apply requires the exact --confirm-thread value")
        result = apply()
    else:
        result = inspect()
        result["dry_run"] = True
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
