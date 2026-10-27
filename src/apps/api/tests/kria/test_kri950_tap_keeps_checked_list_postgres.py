"""KRI-950: the checked list a picker turn keeps, against Postgres.

The submit -> worker -> persist path for a clip-picker reply and the tap that answers it. The
failure-mode list (10 here: the list stays on the server, and the tap finds it) is in
``test_kri950_tap_keeps_checked_list.py``; it was written before the code.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import planner
from app.kria.api_schemas import SubmitTurnBody
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import PlannedKriaTurn
from app.kria.runtime import submit_turn
from app.models import CreationThread, CreationThreadEvent, CreatorAgentTurn
from app.services.client_protocol import clear_request_context
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

CAPTION = "Ojo de bife 🥩"
VIDEO = "analysis-proxy-ios-155481DC-92B9-431A-BD9B-6D69972E2894.mp4"
QUEUE_KEY = "include:the-queue-under-the-striped-awning"
INVENTORY = {
    "version": 1,
    "key": "k" * 64,
    "output": {
        "intents": [
            {
                "intent_id": "caption_ribeye_photo",
                "op": "caption",
                "attribute": "the ribeye photo",
                "creator_text": CAPTION,
                "source_quote": f'show the ribeye photo full screen with the text "{CAPTION}"',
            }
        ]
    },
}


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    yield
    clear_request_context()


def _picker() -> dict[str, Any]:
    return {
        "version": 1,
        "question_id": str(uuid.uuid4()),
        "allow_none": True,
        "categories": [
            {
                "op": "include",
                "key": QUEUE_KEY,
                "label": "the queue under the striped awning",
                "intent_id": "clip_moment_queue_include",
                "candidate_media_ids": [VIDEO],
                "suggested_media_ids": [],
            }
        ],
    }


async def _submit(user_id: uuid.UUID, thread_id: uuid.UUID, **body: Any):  # noqa: ANN202
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                client_event_id=f"kri950-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
                **body,
            ),
        )
    return accepted


@pytest.mark.asyncio
async def test_the_picker_turn_keeps_its_list_on_the_server_and_the_tap_finds_it(
    monkeypatch,
) -> None:
    question = _picker()

    async def _plan(_db, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        return PlannedKriaTurn(
            plan=KriaTurnPlan(
                mode="respond",
                turn_value="question",
                response="I couldn't find the queue in your video. Tap the video if it's in there.",
                clip_question=question,
                clip_intent_inventory=INVENTORY,
            ),
            manifest_hash="a" * 64,
            context_hash="b" * 64,
        )

    monkeypatch.setattr("app.tasks.kria_runtime.plan_live_turn", _plan)
    user_id, thread_id, _ = _seed_runtime_project()
    try:
        asked = await _submit(user_id, thread_id, message="Six moments from our Don Julio walk.")
        await asyncio.to_thread(run_kria_turn.run, asked.turn_id)

        with sync_session() as db:
            turn = db.get(CreatorAgentTurn, uuid.UUID(str(asked.turn_id)))
            replies = db.execute(
                select(CreationThreadEvent.payload).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.role == "assistant",
                )
            ).scalars()
            (reply,) = [p for p in replies if (p or {}).get("turn_id") == str(asked.turn_id)]
        # The turn record keeps the list ...
        assert turn.plan_json["clip_intent_inventory"] == INVENTORY
        # ... and the chat event the app downloads never carries it.
        assert reply["clip_question"]["question_id"] == question["question_id"]
        assert "clip_intent_inventory" not in reply
        assert CAPTION not in json.dumps(reply, ensure_ascii=False)

        await _submit(
            user_id,
            thread_id,
            message="The queue under the striped awning: clip 1",
            clip_selection={
                "question_id": question["question_id"],
                "answers": [{"key": QUEUE_KEY, "media_ids": [VIDEO]}],
            },
        )
        # The tap's turn finds the asking turn and reads its list back from Postgres.
        async with AsyncSessionLocal() as db:
            events = await planner._load_thread_events(db, thread_id)
            asking = planner._picker_answer_turn_id(events)
            assert asking == str(asked.turn_id)
            assert await planner._load_checked_inventory(db, asking) == INVENTORY
    finally:
        await async_engine.dispose()
