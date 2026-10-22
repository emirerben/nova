"""KRI-802: one reply asks the title wording and the clip picker; one turn answers both.

The submit -> store -> claim -> planner path for the batched reply, against Postgres. The
failure-mode list (1, 10, 11, 12 here) is in ``test_kri802_question_batch.py``; it was written
before the code.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import SubmitTurnBody
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import PlannedKriaTurn
from app.kria.runtime import request_digest, submit_turn
from app.models import CreationThread, CreationThreadEvent
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.clip_selection import fold_clip_selections
from app.services.creative_copy_decisions import fold_creative_copy, wording_question
from app.tasks.kria_runtime import _append_sync_event, run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

DIGEST = "d" * 24
TITLE = "Lunch at Don Julio \U0001f1e6\U0001f1f7"
RED_CAP = "analysis-proxy-ios-38A1FAF8-FA26-4257-B8CA-4D488D26A94C.mp4"
WAITER = "analysis-proxy-ios-D0BD8AF7-0904-4AE2-B585-5BE68623369E.mp4"
CLIP_KEY = "order:my-friend-in-the-red-cap-smiling-at-the-camera"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    yield
    clear_request_context()


def _ask_both(thread_id: uuid.UUID) -> tuple[dict[str, Any], dict[str, Any]]:
    """The batched reply exactly as the planner sends it: a wording card and a clip picker."""

    wording = wording_question(target="opening_title", candidate=TITLE, dependency_digest=DIGEST)
    clips = {
        "version": 1,
        "question_id": str(uuid.uuid4()),
        "allow_none": True,
        "categories": [
            {
                "op": "order",
                "key": CLIP_KEY,
                "label": "my friend in the red cap smiling at the camera",
                "intent_id": "order_friend_last",
                "candidate_media_ids": [WAITER, RED_CAP],
                "suggested_media_ids": [],
            }
        ],
    }
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_response",
            content="Does this wording work?\n\nWhich clips show the red cap?",
            payload={"turn_value": "question", "choice_question": wording, "clip_question": clips},
        )
        db.commit()
    return wording, clips


def _body(thread_id: uuid.UUID, wording: dict, clips: dict) -> SubmitTurnBody:
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    return SubmitTurnBody(
        message="Use this wording · My friend in the red cap smiling at the camera: clip 9",
        client_event_id=f"batch-{uuid.uuid4().hex}",
        expected_thread_revision=revision,
        choice_selection={"question_id": wording["question_id"], "option_key": "approve"},
        clip_selection={
            "question_id": clips["question_id"],
            "answers": [{"key": CLIP_KEY, "media_ids": [RED_CAP]}],
        },
    )


async def _submit(user_id: uuid.UUID, thread_id: uuid.UUID, body: SubmitTurnBody):  # noqa: ANN202
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(db, thread_id=thread_id, creator_id=user_id, body=body)
    return accepted


def _events(thread_id: uuid.UUID) -> list[tuple[str, dict]]:
    with sync_session() as db:
        rows = db.execute(
            select(CreationThreadEvent.role, CreationThreadEvent.payload)
            .where(CreationThreadEvent.thread_id == thread_id)
            .order_by(CreationThreadEvent.sequence)
        ).all()
    return [(role, payload) for role, payload in rows]


@pytest.mark.asyncio
async def test_one_turn_answers_both_cards_and_the_planner_is_told_to_batch(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def _plan(_db, **kwargs):  # noqa: ANN001, ANN003, ANN202
        seen.update(kwargs)
        return PlannedKriaTurn(
            plan=KriaTurnPlan(mode="respond", turn_value="question", response="Got both."),
            manifest_hash="a" * 64,
            context_hash="b" * 64,
        )

    monkeypatch.setattr("app.tasks.kria_runtime.plan_live_turn", _plan)
    user_id, thread_id, _ = _seed_runtime_project()
    try:
        wording, clips = _ask_both(thread_id)
        body = _body(thread_id, wording, clips)
        set_client_protocol(14)
        accepted = await _submit(user_id, thread_id, body)
        # 10. Neither selection is refused as stale: both questions are open in one reply ...
        (stored,) = [p for role, p in _events(thread_id) if role == "user" and p]
        assert stored["choice_selection"]["option_key"] == "approve"
        assert stored["clip_selection"]["answers"][0]["media_ids"] == [RED_CAP]
        # 1. ... the message says which app answered it ...
        assert stored["answers_batched_questions"] is True
        # 11. ... outside the request digest, so a resend from another build still replays.
        assert stored["request_digest"] == request_digest(body)
        # 10. Both answers fold: the title is approved and the red cap is on the tapped clip.
        events = _events(thread_id)
        copy = fold_creative_copy(events, dependency_digest=DIGEST)["opening_title"]
        assert (copy.status, copy.approved) == ("approved", TITLE)
        selections = fold_clip_selections(events)
        assert selections.by_key[CLIP_KEY].media_ids == (RED_CAP,)
        # The worker re-plans once, with both answers and the batching permission.
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert seen["batch_questions"] is True
        assert seen["answers_clip_question"] is True
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [None, 13])
async def test_older_apps_and_the_web_keep_one_question_per_turn(monkeypatch, protocol) -> None:
    seen: dict[str, Any] = {}

    async def _plan(_db, **kwargs):  # noqa: ANN001, ANN003, ANN202
        seen.update(kwargs)
        return PlannedKriaTurn(
            plan=KriaTurnPlan(mode="respond", turn_value="question", response="Which one?"),
            manifest_hash="a" * 64,
            context_hash="b" * 64,
        )

    monkeypatch.setattr("app.tasks.kria_runtime.plan_live_turn", _plan)
    user_id, thread_id, _ = _seed_runtime_project()
    try:
        with sync_session() as db:
            revision = db.get(CreationThread, thread_id).revision
        if protocol is not None:
            set_client_protocol(protocol)
        accepted = await _submit(
            user_id,
            thread_id,
            SubmitTurnBody(
                message="Montage of our lunch at Don Julio",
                client_event_id=f"old-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
            ),
        )
        (stored,) = [p for role, p in _events(thread_id) if role == "user" and p]
        assert "answers_batched_questions" not in stored  # byte-identical payload
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert "batch_questions" not in seen
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_kill_switch_stops_batching_without_a_deploy(monkeypatch) -> None:
    # 12. `KRIA_QUESTION_BATCH_MIN_CLIENT_PROTOCOL=999` (fly secret + restart): no app qualifies.
    monkeypatch.setattr(settings, "kria_question_batch_min_client_protocol", 999)
    user_id, thread_id, _ = _seed_runtime_project()
    try:
        with sync_session() as db:
            revision = db.get(CreationThread, thread_id).revision
        set_client_protocol(14)
        await _submit(
            user_id,
            thread_id,
            SubmitTurnBody(
                message="Montage of our lunch at Don Julio",
                client_event_id=f"off-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
            ),
        )
        (stored,) = [p for role, p in _events(thread_id) if role == "user" and p]
        assert "answers_batched_questions" not in stored
    finally:
        await async_engine.dispose()
