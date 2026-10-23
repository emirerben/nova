"""KRI-849 through the REAL runtime turn: `run_kria_turn` -> `_complete_draft_turn` on Postgres.

`tests/kria/test_kri849_dj_t1_unclear_word_replay.py` drives the gate, the writer and the receipts
module by module. This one runs the whole draft turn the way the worker does (database, brief
persistence, choice gate, receipts, approval card), so the wiring inside `_complete_draft_turn`
itself is covered: the review found that deleting the `PlanFacts.unclear_words` line there left
every other test green.

Cases:

1. the ask is recorded and nobody answered: the turn is a tappable `unclear_word` question and
   there is no draft to approve (the approval backstop inside the turn);
2. the creator's own words ("leave that word out") are recorded as `unclear_answer`: the draft goes
   ahead and the captions row's receipt, the reply and the approval card say what the render
   will do, never "I can't verify this one automatically yet." and never the heard words.

Needs the test database like the other `*_postgres` runtime tests.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreativeStrategy,
    CreatorMediaRef,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.kria.brief import BriefUpdate
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.models import CreationThread, CreationThreadEvent, CreatorAgentApproval, PlanItem
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project, _submit

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri849_dj_t1" / "dj_t1.json").read_text(
        encoding="utf-8"
    )
)
OLD_RECEIPT = "I can't verify this one automatically yet."
GUESSES = [g.casefold() for g in FIXTURE["guess_tokens"]]


def _plan_and_asks(*, unclear_answer: str | None):
    rows = copy.deepcopy(FIXTURE["brief"]["requirements"])
    for row in rows:
        if row["id"] == "r3" and unclear_answer:
            row["facts"]["unclear_answer"] = unclear_answer
    plan = adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                direction="native",
                edit_format="subtitled",
                audio_strategy="original_audio",
                pacing="balanced",
                render_program="guided",
                selected_media_ids=[],
                rationale="Lucas names the sauces.",
                opening_title="Sauce check at Don Julio",
                caption_style="clean",
            ),
            summary="A Talking edit of Lucas naming the sauces.",
        ),
    )
    asks = tuple(
        BriefUpdate(
            kind=row["kind"],
            scope=row["scope"],
            literal=row["literal"],
            description=row["description"],
            facts=row["facts"],
        )
        for row in rows
    )
    return plan, asks, rows


async def _run_turn(monkeypatch: pytest.MonkeyPatch, *, unclear_answer: str | None):
    plan, asks, rows = _plan_and_asks(unclear_answer=unclear_answer)
    user_id, thread_id, _session_id = _seed_runtime_project()
    take = FIXTURE["take_row"]
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        db.execute(
            update(PlanItem)
            .where(PlanItem.id == thread.active_plan_item_id)
            .values(
                clip_gcs_paths=[take["gcs_path"]],
                clip_assignments=[copy.deepcopy(take)],
                edit_format="subtitled",
            )
        )
        db.commit()
    manifest = ResolvedCreatorManifest(
        item_id="item-1",
        edit_format="subtitled",
        render_program="guided",
        media=[CreatorMediaRef(media_id="clip-1", kind="video", duration_s=25.7)],
        capabilities={"edit_format:subtitled": CapabilityAvailability(available=True)},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=plan,
            manifest_hash="a" * 64,
            context_hash="b" * 64,
            brief_updates=asks,
            brief_route="replan",
            brief_clip_ids=("clip-1",),
            brief_manifest=manifest,
        )

    for name, value in {
        "kria_runtime_v2_enabled": True,
        "main_creator_agent_enabled": True,
        "kria_creative_brief_enabled": True,
        "kria_brief_binding_enabled": True,
        "kria_choice_questions_enabled": True,
        "phone_rendering_enabled": True,
        "phone_render_user_ids": [],
        "phone_subtitled_rendering_enabled": True,
        "subtitled_archetype_enabled": True,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    accepted = await _submit(user_id, thread_id, FIXTURE["creator_request"], 2)
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    return accepted, result, thread_id, rows


@pytest.mark.asyncio
async def test_an_unanswered_ask_becomes_a_question_not_an_approvable_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        accepted, result, thread_id, _rows = await _run_turn(monkeypatch, unclear_answer=None)
        assert result["status"] != "awaiting_approval", result
        with sync_session() as db:
            payloads = [
                dict(event.payload or {})
                for event in db.execute(
                    select(CreationThreadEvent).where(
                        CreationThreadEvent.thread_id == thread_id,
                        CreationThreadEvent.role == "assistant",
                    )
                ).scalars()
            ]
            approvals = db.execute(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
                )
            ).all()
        question = next(
            p["choice_question"] for p in payloads if isinstance(p.get("choice_question"), dict)
        )
        assert question["kind"] == "unclear_word"
        assert {o["key"] for o in question["options"]} >= {"leave_out", "use_1", "use_2"}
        assert not approvals, "nothing to approve while the question is open"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_the_draft_turn_says_what_the_render_will_do_about_the_unclear_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        accepted, result, thread_id, rows = await _run_turn(monkeypatch, unclear_answer="omit")
        assert result["status"] == "awaiting_approval", result
        with sync_session() as db:
            event = db.execute(
                select(CreationThreadEvent).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "draft_applied",
                )
            ).scalar_one()
            payload, content = dict(event.payload), event.content
            card = db.execute(
                select(CreatorAgentApproval.consequence_summary).where(
                    CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
                )
            ).scalar_one()
        receipts = {r["requirement_id"]: r for r in payload["requirement_receipts"]}
        r3 = receipts["r3"]
        assert OLD_RECEIPT not in r3["reason"]
        assert (
            "I'll leave out the word I couldn't make out around 0:09 in your clip" in r3["reason"]
        )
        # The same line reaches the reply the creator reads and the approval card.
        assert "around 0:09 in your clip" in content and "around 0:09 in your clip" in card
        for text in (r3["reason"], content, card):
            assert not [g for g in GUESSES if g in text.casefold()], text
    finally:
        await async_engine.dispose()
