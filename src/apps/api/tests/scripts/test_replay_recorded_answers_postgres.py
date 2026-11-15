"""A recorded tap reaches the replay's own question, through the real runtime (KRI-1375).

The server mints every question id, so production's ids never name a question of a replay, and
pool assets get a fresh id per replay. Shapes follow production threads: job 07efa44c answered a
`simplification_requires_choice` card with `simpler_version` on its target message, and job
98eac78f tapped `asset-<uuid>` pool candidates in a clip picker.

Ways this can go wrong, each checked below against `submit_turn` on Postgres:
* the recorded id is sent unchanged and the runtime refuses it as stale;
* the answer is aimed at a replay question of another kind, answering what the creator never saw;
* a pool asset id is sent as production's and the clip picker refuses it as not offered;
* a choice the server derived from "you choose" is sent as production's pick instead of being
  derived again against the replay's own question.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.models import CreationThread, CreationThreadEvent
from app.tasks.kria_runtime import _append_sync_event
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

SCRIPTS = Path(__file__).parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("replay_creator", SCRIPTS / "replay_creator.py")
assert spec and spec.loader
replay_creator = importlib.util.module_from_spec(spec)
sys.modules.setdefault("replay_creator", replay_creator)
spec.loader.exec_module(replay_creator)

PRODUCTION_QUESTION = "148afe3e-92db-4f7d-b457-e2a0be90898b"
SIMPLER = {
    "version": 1,
    "conflict": "simplification_requires_choice",
    "options": [
        {"key": "change_request", "label": "Change my request", "recommended": False},
        {"key": "simpler_version", "label": "Make it without these", "recommended": True},
    ],
    "allow_free_text": True,
}
PROD_ASSET = "asset-bc5abc8d-6c3c-472c-ae1a-c3f39e200ca8"
CLIP = "analysis-proxy-ios-67E18328-733F-46AB-99D0-4959F9ADBBBC.mp4"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr("app.routes.kria_runtime._publish_turn", lambda _turn_id: None)


def _ask(thread_id: uuid.UUID, key: str, question: dict) -> None:
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_response",
            content="Should I make it without these?",
            payload={"turn_value": "question", key: question},
        )
        db.commit()


def _stored(thread_id: uuid.UUID, field: str) -> list[dict]:
    with sync_session() as db:
        payloads = db.execute(
            select(CreationThreadEvent.payload).where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.event_type == "user_message",
            )
        ).scalars()
        return [p[field] for p in payloads if p and field in p]


def _turn(message: str, **answers: dict) -> replay_creator.RecordedTurn:
    turn_id = str(uuid.uuid4())
    return replay_creator.RecordedTurn(
        turn_id=turn_id,
        message=message,
        client_protocol=28,
        payload={"turn_id": turn_id, "client_protocol": 28, **answers},
        approved=False,
    )


def _ids(*questions: tuple[str, str, dict], media: dict | None = None):
    return replay_creator.ReplayIds(
        questions={qid: (key, {**q, "question_id": qid}) for qid, key, q in questions},
        media=media or {},
    )


async def _send(user_id, thread_id, turn, ids):  # noqa: ANN001, ANN202
    try:
        return await replay_creator.submit_recorded_turn(
            thread_id=thread_id, user_id=user_id, turn=turn, ids=ids
        )
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_tapped_choice_answers_the_replays_question_of_the_same_kind() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    replay_question = str(uuid.uuid4())
    _ask(thread_id, "choice_question", {**SIMPLER, "question_id": replay_question})
    recorded = {"option_key": "simpler_version", "question_id": PRODUCTION_QUESTION}

    _, sent = await _send(
        user_id,
        thread_id,
        _turn("Make it without these", choice_selection=recorded),
        _ids((PRODUCTION_QUESTION, "choice_question", SIMPLER)),
    )

    assert _stored(thread_id, "choice_selection") == [
        {"option_key": "simpler_version", "question_id": replay_question}
    ]
    assert sent["remapped"]["choice_selection"] == {
        "production_question_id": PRODUCTION_QUESTION,
        "replay_question_id": replay_question,
    }


@pytest.mark.asyncio
async def test_a_choice_for_another_kind_of_question_is_a_divergence_not_an_answer() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    other = {
        "version": 1,
        "question_id": str(uuid.uuid4()),
        "conflict": "order_vs_group",
        "options": [{"key": "group_first", "label": "Group", "recommended": True}],
    }
    _ask(thread_id, "choice_question", other)
    recorded = {"option_key": "simpler_version", "question_id": PRODUCTION_QUESTION}

    with pytest.raises(replay_creator.TurnDiverged) as diverged:
        await _send(
            user_id,
            thread_id,
            _turn("Make it without these", choice_selection=recorded),
            _ids((PRODUCTION_QUESTION, "choice_question", SIMPLER)),
        )

    detail = diverged.value.detail
    assert detail["production_question"]["conflict"] == "simplification_requires_choice"
    assert detail["replay_question"]["conflict"] == "order_vs_group"
    assert "previous reply already differed" in detail["reason"]
    assert _stored(thread_id, "choice_selection") == []


@pytest.mark.asyncio
async def test_a_tapped_pool_asset_is_sent_as_the_replays_asset() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    replay_asset = f"asset-{uuid.uuid4()}"
    production_question = "c0541fd9-0c76-4e75-8ca4-888983273647"
    picker = {
        "question_id": str(uuid.uuid4()),
        "categories": [{"key": "order:the-warm-up", "candidate_media_ids": [CLIP, replay_asset]}],
    }
    _ask(thread_id, "clip_question", picker)
    recorded = {
        "question_id": production_question,
        "answers": [{"key": "order:the-warm-up", "media_ids": [PROD_ASSET]}],
        "none_keys": [],
        "skipped": False,
    }

    await _send(
        user_id,
        thread_id,
        _turn("The warm-up: clip 9", clip_selection=recorded),
        _ids(
            (production_question, "clip_question", {"categories": picker["categories"]}),
            media={PROD_ASSET: replay_asset},
        ),
    )

    stored = _stored(thread_id, "clip_selection")
    assert stored[0]["question_id"] == picker["question_id"]
    assert stored[0]["answers"] == [{"key": "order:the-warm-up", "media_ids": [replay_asset]}]


@pytest.mark.asyncio
async def test_a_category_the_replay_resolved_itself_is_left_out_of_the_answer() -> None:
    # 40684a25: production asked `order:the-obelisco` and `exclude:anything-too-short-to-use`;
    # the replay's server measured the short clips itself and asked only the first.
    user_id, thread_id, _ = _seed_runtime_project()
    production_question = "5b0c5d3e-3a4c-4b8e-9f6e-2d1a7c9b8e40"
    asked = [
        {"key": "order:the-obelisco", "candidate_media_ids": [CLIP]},
        {"key": "exclude:anything-too-short-to-use", "candidate_media_ids": [CLIP]},
    ]
    picker = {"question_id": str(uuid.uuid4()), "categories": asked[:1]}
    _ask(thread_id, "clip_question", picker)
    recorded = {
        "question_id": production_question,
        "answers": [{"key": "exclude:anything-too-short-to-use", "media_ids": [CLIP]}],
        "none_keys": ["order:the-obelisco"],
        "skipped": False,
    }

    _, sent = await _send(
        user_id,
        thread_id,
        _turn("None for the Obelisco, leave out clips 2 and 4", clip_selection=recorded),
        _ids((production_question, "clip_question", {"categories": asked})),
    )

    stored = _stored(thread_id, "clip_selection")
    assert (stored[0]["answers"], stored[0]["none_keys"]) == ([], ["order:the-obelisco"])
    assert sent["remapped"]["clip_selection"]["not_asked_by_replay"] == [
        "exclude:anything-too-short-to-use"
    ]


@pytest.mark.asyncio
async def test_you_choose_is_derived_again_against_the_replays_question() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    replay_question = str(uuid.uuid4())
    # The replay's card recommends the other option, so a copied pick would be production's.
    replay_card = {
        **SIMPLER,
        "question_id": replay_question,
        "options": [
            {"key": "change_request", "label": "Change my request", "recommended": True},
            {"key": "simpler_version", "label": "Make it without these", "recommended": False},
        ],
    }
    _ask(thread_id, "choice_question", replay_card)
    recorded = {
        "option_key": "simpler_version",
        "question_id": PRODUCTION_QUESTION,
        "delegated": True,
    }

    _, sent = await _send(
        user_id,
        thread_id,
        _turn("you choose", choice_selection=recorded),
        _ids((PRODUCTION_QUESTION, "choice_question", SIMPLER)),
    )

    assert sent["rederived_from_text"] == ["choice_selection"]
    assert _stored(thread_id, "choice_selection") == [
        {"option_key": "change_request", "question_id": replay_question, "delegated": True}
    ]
