"""KRI-723: the creator's moment answer over the real submit path (Postgres).

The planner asks "put your moments in order and show me where each one is" (persisted on the
assistant event as ``moment_order_question``). The app answers with ``moment_order`` on the
next turn. Ways this could fail, each pinned below:

* An answer to an old / unknown question is accepted (409 ``moment_order_stale``).
* An answer pointing at a clip that is not this thread's take, or at a take that was
  re-uploaded since the question (a new storage generation), pins the wrong bytes.
* An incomplete / out-of-bounds / overlapping answer is stored and later folded.
* The stored answer is not what the planner's gate folds (the event shape drifts).
* An old app is offered the card: only a build at ``moment_order_min_client_protocol`` gets
  ``answers_moment_questions`` stamped on its turn.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import SubmitTurnBody
from app.kria.planner import _load_thread_events
from app.kria.runtime import RuntimeFailure, submit_turn
from app.models import CreationThread, CreationThreadEvent, PlanItem
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.named_moments import decide, named_moments
from app.tasks.kria_runtime import _append_sync_event
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

TAKE = "analysis-proxy-ios-take.mp4"
GENERATION = "1792587128136153"
WORDS = ["Caro arriving with the black tray", "her hello wave", "Atakan's first bite"]


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    yield
    clear_request_context()


def _take_row(generation: str = GENERATION) -> dict:
    return {
        "media_id": TAKE,
        "kind": "video",
        "storage_generation": generation,
        "duration_s": 68.37,
        "analysis": {
            "best_moments": [
                {
                    "start_s": 57.04,
                    "end_s": 59.84,
                    "energy": 8.0,
                    "description": "man takes first bite of dessert",
                },
            ]
        },
    }


def _question() -> dict:
    strategy = {
        "edit_format": "montage",
        "resolved_clip_intents": [
            {
                "op": "order",
                "status": "resolved",
                "attribute": ", ".join(WORDS),
                "assignments": [{"media_id": TAKE}],
            }
        ],
    }
    moment_set = named_moments(strategy, {"clip_assignments": [_take_row()]})
    decision = decide(moment_set, [], can_ask=True, default_window_s=2.0)
    assert decision.question is not None
    return decision.question


def _seed(question: dict, *, generation: str = GENERATION):  # noqa: ANN202
    user_id, thread_id, _ = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.clip_assignments = [_take_row(generation)]
        _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_response",
            content="I found 1 of your 3 moments.",
            payload={"turn_value": "question", "moment_order_question": question},
        )
        db.commit()
    return user_id, thread_id


def _answer(question: dict, windows=((2.0, 4.0), (33.0, 35.0), (57.5, 59.5))) -> dict:  # noqa: ANN001
    return {
        "question_id": question["question_id"],
        "answers": [
            {
                "moment_key": row["moment_key"],
                "action": "point",
                "media_id": TAKE,
                "source_start_s": start,
                "source_end_s": end,
            }
            for row, (start, end) in zip(question["moments"], windows, strict=True)
        ],
    }


async def _submit(user_id, thread_id, answer: dict):  # noqa: ANN001, ANN202
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message="Here are my moments",
                client_event_id=f"mo-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
                moment_order=answer,
            ),
        )
    return accepted


def _user_payloads(thread_id) -> list[dict]:  # noqa: ANN001
    with sync_session() as db:
        rows = db.execute(
            select(CreationThreadEvent.payload).where(
                CreationThreadEvent.thread_id == thread_id, CreationThreadEvent.role == "user"
            )
        ).scalars()
        return [dict(p or {}) for p in rows]


@pytest.mark.asyncio
async def test_a_valid_answer_is_stored_and_the_gate_folds_it_into_pins() -> None:
    question = _question()
    user_id, thread_id = _seed(question)
    try:
        set_client_protocol(settings.moment_order_min_client_protocol)
        await _submit(user_id, thread_id, _answer(question))
        payload = _user_payloads(thread_id)[-1]
        assert payload["moment_order"]["question_id"] == question["question_id"]
        assert payload["answers_moment_questions"] is True
        async with AsyncSessionLocal() as db:
            events = await _load_thread_events(db, thread_id)
        strategy = {
            "edit_format": "montage",
            "resolved_clip_intents": [
                {
                    "op": "order",
                    "status": "resolved",
                    "attribute": ", ".join(WORDS),
                    "assignments": [{"media_id": TAKE}],
                }
            ],
        }
        moment_set = named_moments(strategy, {"clip_assignments": [_take_row()]})
        pins = decide(moment_set, events, can_ask=True, default_window_s=2.0).pins
        assert [(p.words, p.origin, p.start_s, p.end_s) for p in pins] == [
            (WORDS[0], "creator_answer", 2.0, 4.0),
            (WORDS[1], "creator_answer", 33.0, 35.0),
            (WORDS[2], "creator_answer", 57.5, 59.5),
        ]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_an_old_app_is_never_stamped_as_able_to_answer() -> None:
    question = _question()
    user_id, thread_id = _seed(question)
    try:
        set_client_protocol(settings.moment_order_min_client_protocol - 1)
        await _submit(user_id, thread_id, _answer(question))
        assert "answers_moment_questions" not in _user_payloads(thread_id)[-1]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "status", "code"),
    [
        ("stale", 409, "moment_order_stale"),
        ("reuploaded", 409, "moment_order_stale"),
        ("foreign_take", 404, "moment_order_media_not_found"),
        ("out_of_bounds", 422, "moment_order_invalid"),
        ("incomplete", 422, "moment_order_invalid"),
        ("overlap", 422, "moment_order_overlap"),
    ],
)
async def test_bad_answers_are_refused_and_nothing_is_stored(
    case: str, status: int, code: str
) -> None:
    question = _question()
    user_id, thread_id = _seed(question, generation="999" if case == "reuploaded" else GENERATION)
    answer = _answer(question)
    if case == "stale":
        answer["question_id"] = "moments-old"
    elif case == "foreign_take":
        for row in answer["answers"]:
            row["media_id"] = "someone-elses-take.mp4"
    elif case == "out_of_bounds":
        answer["answers"][0]["source_end_s"] = 80.0
    elif case == "incomplete":
        answer["answers"].pop()
    elif case == "overlap":
        answer["answers"][1].update(source_start_s=3.0, source_end_s=5.0)
    try:
        with pytest.raises(RuntimeFailure) as err:
            await _submit(user_id, thread_id, answer)
        assert (err.value.status_code, err.value.code) == (status, code)
        assert all("moment_order" not in p for p in _user_payloads(thread_id))
    finally:
        await async_engine.dispose()
