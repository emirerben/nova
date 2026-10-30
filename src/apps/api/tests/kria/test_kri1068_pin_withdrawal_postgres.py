"""KRI-1068 through the REAL runtime turn: `run_kria_turn` -> `_complete_draft_turn` on Postgres.

`test_kri1068_pin_withdrawal.py` drives the planner step, the gate and the backstop module by
module. This one runs the Leverkusen turns the way the worker does (database, brief versions,
choice gate, the stored reply and its payload), so the wiring inside `_complete_draft_turn` is
covered too:

1. turn 1 is stuck on "the final whistle" and the stored reply records that clip;
2. a turn that does not drop it gets the ways forward, never the same reply twice in a row;
3. "Okay skip that part" (read by the real planner step from the stored events) persists a
   brief version without the clip, and the turn reaches a draft that says it was dropped;
4. the incident's own turn 2, "Okay make it simplier", does the same one turn earlier.

The Main Creator is replaced by the turn's recorded-shape draft (`kri1068_leverkusen_m1.json`);
no model is called. Needs the test database like the other `*_postgres` runtime tests.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreatorMediaRef,
    ResolvedCreatorManifest,
)
from app.config import settings
from app.database import sync_session
from app.kria import planner
from app.kria.brief import BriefUpdate, load_latest_brief_sync
from app.kria.brief_binding import snapshot_media
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentTurn,
    PlanItem,
)
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_kri1068_pin_withdrawal import CASE, DROPPED, PROMPT, STUCK, WHISTLE, _draft
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project, _submit

TURN1 = tuple(
    BriefUpdate(
        kind=req["kind"],
        scope=req["scope"],
        description=req["description"],
        facts=req["facts"],
    )
    for req in CASE["brief_v1"]["requirements"]
)


def _clip_rows(user_id: uuid.UUID, thread_id: uuid.UUID) -> list[dict]:
    return [
        {
            "media_id": m["media_id"],
            "kind": m["kind"],
            "duration_s": m["duration_s"],
            "capture": {"capture_time": m["capture_time"]},
            "has_audio": m["kind"] == "video",
            "gcs_path": f"users/{user_id}/creation-threads/{thread_id}/{m['media_id']}",
            "manifest_identity": m["media_id"],
            "storage_generation": "1",
        }
        for m in CASE["media"]
    ]


def _project() -> tuple[uuid.UUID, uuid.UUID]:
    user_id, thread_id, _session_id = _seed_runtime_project()
    rows = _clip_rows(user_id, thread_id)
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        db.execute(
            update(PlanItem)
            .where(PlanItem.id == thread.active_plan_item_id)
            .values(
                clip_gcs_paths=[row["gcs_path"] for row in rows],
                clip_assignments=copy.deepcopy(rows),
                edit_format="montage",
            )
        )
        db.commit()
    return user_id, thread_id


MANIFEST = ResolvedCreatorManifest(
    item_id="item-1",
    edit_format="montage",
    render_program="guided",
    media=[
        CreatorMediaRef(
            media_id=m["media_id"],
            kind=m["kind"],
            duration_s=m["duration_s"],
        )
        for m in CASE["media"]
    ],
    capabilities={"edit_format:montage": CapabilityAvailability(available=True)},
    context_hash="a" * 64,
    manifest_hash="b" * 64,
)


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "kria_runtime_v2_enabled": True,
        "main_creator_agent_enabled": True,
        "kria_creative_brief_enabled": True,
        "kria_brief_binding_enabled": True,
        "kria_choice_questions_enabled": True,
        "clip_intents_enabled": True,
        "kria_clip_selection_questions_enabled": True,
    }.items():
        monkeypatch.setattr(settings, name, value)


def _wire(monkeypatch: pytest.MonkeyPatch, user_id: uuid.UUID, thread_id: uuid.UUID) -> None:
    """The live planner for every turn: turn 1 states the brief; later turns state nothing
    new (the model restated no requirement) and run the real stuck-clip step and gate."""

    async def _planned(snapshot, message, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        draft = _draft()
        if message == PROMPT:
            # Turn 1 deliberately exercises the completion backstop, as it ran in prod.
            return replace(
                draft,
                brief_updates=TURN1,
                brief_route="replan",
                brief_manifest=MANIFEST,
            )
        engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                prior = await planner.load_latest_brief(db, thread_id)
                updates, notices = await planner._withdraw_stuck_pins(
                    db, thread_id=thread_id, prior=prior, updates=(), user_message=message
                )
                item = await db.get(PlanItem, uuid.UUID(str(snapshot["item_id"])))
                return await planner._gate_unresolved_choices(
                    db,
                    replace(
                        draft,
                        brief_updates=updates,
                        pin_notices=notices,
                        brief_route="replan",
                        brief_manifest=MANIFEST,
                        brief_expected_version=prior.version if prior else 0,
                        media_snapshot=snapshot_media(item),
                    ),
                    thread_id=thread_id,
                    creator_id=user_id,
                    item_id=item.id,
                    creator_text=message,
                )
        finally:
            await engine.dispose()

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)


async def _turn(user_id: uuid.UUID, thread_id: uuid.UUID, message: str) -> CreationThreadEvent:
    """Send ``message`` as the creator and run the worker's turn; the reply it stored."""
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    accepted = await _submit(user_id, thread_id, message, revision)
    await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, uuid.UUID(str(accepted.turn_id)))
        assert turn.status in {"completed", "awaiting_approval"}, turn.error
        return db.get(CreationThreadEvent, turn.observed_event_id)


def _r2(thread_id: uuid.UUID):  # noqa: ANN202
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)
    return brief.version, next(req for req in brief.live() if req.kind == "order")


def _approvals(thread_id: uuid.UUID) -> int:
    with sync_session() as db:
        return len(
            db.execute(
                select(CreatorAgentApproval).where(CreatorAgentApproval.thread_id == thread_id)
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_stuck_twice_then_skip_that_part_drops_the_ending_and_drafts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id = _project()
    _wire(monkeypatch, user_id, thread_id)

    first = await _turn(user_id, thread_id, PROMPT)
    assert first.content.startswith(STUCK)
    assert first.payload["brief_coverage"]["blocked_pins"] == [
        {"requirement_id": "r2", "position": "last", "words": WHISTLE}
    ]
    version, r2 = _r2(thread_id)
    assert r2.facts["last_clip"] == WHISTLE

    # A message that does not drop the clip: still stuck, but never the same reply again.
    second = await _turn(user_id, thread_id, "Use my stadium videos")
    assert second.content != first.content
    assert second.content.startswith(STUCK) and 'reply "skip it"' in second.content
    assert second.payload["brief_coverage"]["blocked_pins"][0]["words"] == WHISTLE
    assert _r2(thread_id)[1].facts["last_clip"] == WHISTLE  # KRI-522: kept

    third = await _turn(user_id, thread_id, "Okay skip that part")
    new_version, r2 = _r2(thread_id)
    assert new_version > version
    assert r2.facts == {"key": "capture_time", "first_clip": "the lineup sheet"}
    assert WHISTLE not in r2.text()
    assert third.event_type == "draft_applied"  # a draft, not another stuck reply
    assert DROPPED in third.content
    assert _approvals(thread_id) == 1


@pytest.mark.asyncio
async def test_the_incidents_make_it_simplier_drafts_on_turn_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id = _project()
    _wire(monkeypatch, user_id, thread_id)

    first = await _turn(user_id, thread_id, PROMPT)
    assert first.content.startswith(STUCK)
    second = await _turn(user_id, thread_id, "Okay make it simplier")
    assert second.event_type == "draft_applied"
    assert DROPPED in second.content
    assert "last_clip" not in _r2(thread_id)[1].facts
    assert _approvals(thread_id) == 1
