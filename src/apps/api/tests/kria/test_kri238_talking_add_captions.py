"""KRI-238: "Add captions" on a one-clip iPhone Talking project ended in
"I couldn't finish that step" (prod thread 467a02c4, 2026-10-01).

The Main Creator answered with `media_scope: "all"` for the single clip. "all"
needs guided proposals, which a phone Talking edit never has, so every attempt
was refused as a schema error and the turn failed with
`RuntimeError: Kria could not produce a reliable editorial plan`. Runs against
the test Postgres with production flags, the prod clip receipt, and the prod
thread events; only the model is stubbed, with the answer it gave.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents._runtime import ModelInvocation
from app.agents.main_creator import MAIN_CREATOR_CONVERSATION_MAX
from app.config import settings
from app.database import sync_session
from app.kria import planner
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreatorAgentSession,
    Persona,
    PlanItem,
    User,
)

MEDIA_ID = "analysis-proxy-ios-4DB8A9C3-80FE-4269-9D22-7C19280DC543.mp4"
# The receipt iOS uploaded with the clip (prod `media_added` payload).
UPLOAD_CONTRACT = {
    "proxy": {
        "width": 320,
        "height": 568,
        "original": {
            "kind": "video",
            "width": 1920,
            "height": 1080,
            "sha256": "727797c7fd0cf096191ff17f3dc9a500048083efaa7acd019c2fe35f9e354dbc",
            "has_audio": True,
            "byte_count": 39352254,
            "duration_s": 14.793333333333333,
            "orientation_degrees": 90,
        },
        "duration_s": 14.8,
        "frame_rate": 30.0,
        "timing_version": 1,
        "orientation_degrees": 0,
    },
    "purpose": "analysis_proxy",
}


def _seed(*, history: list[tuple[str, str]] = ()) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id, persona_id, plan_id, item_id, session_id, thread_id = (uuid.uuid4() for _ in range(6))
    path = f"users/{user_id}/creation-threads/{thread_id}/analysis-proxies/{MEDIA_ID}"
    with sync_session() as db:
        db.add(User(id=user_id, email=f"{user_id}@test.local"))
        db.flush()
        db.add(Persona(id=persona_id, user_id=user_id, persona_status="ready", persona={}))
        db.flush()
        db.add(ContentPlan(id=plan_id, user_id=user_id, persona_id=persona_id))
        db.flush()
        db.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan_id,
                position=1,
                idea="",
                item_status="awaiting_clips",
                edit_format="subtitled",
                content_mode="existing_footage",
                clip_gcs_paths=[path],
                clip_assignments=[
                    {
                        "media_id": MEDIA_ID,
                        "manifest_identity": MEDIA_ID,
                        "gcs_path": path,
                        "kind": "video",
                        "duration_s": 14.8,
                        "has_audio": True,
                        "storage_generation": "1790858000000000",
                        "upload_contract": UPLOAD_CONTRACT,
                    }
                ],
            )
        )
        db.flush()
        db.add(CreatorAgentSession(id=session_id, creator_id=user_id, plan_item_id=item_id))
        db.flush()
        db.add(
            CreationThread(
                id=thread_id,
                creator_id=user_id,
                runtime_version=2,
                content_plan_id=plan_id,
                active_plan_item_id=item_id,
                active_creator_agent_session_id=session_id,
                title="Add Captions",
                revision=5,
                state={"edit_format": "talking_to_camera", "media_count": 1},
            )
        )
        db.flush()
        events = [
            ("system", "thread_created", None),
            ("assistant", "format_prompt", "What are we making? Pick a format."),
            ("user", "action_select_format", None),
            ("user", "media_added", None),
            *((role, f"{role}_message", text) for role, text in history),
            ("user", "user_message", "Add captions"),
        ]
        for sequence, (role, kind, content) in enumerate(events):
            db.add(
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=sequence,
                    revision=sequence + 1,
                    role=role,
                    event_type=kind,
                    content=content,
                    payload=None,
                )
            )
        db.commit()
    return user_id, thread_id, item_id


# The answer the live Main Creator gave on a local replay of this turn.
_ADD_CAPTIONS_ANSWER = {
    "action": {
        "kind": "propose_strategy",
        "strategy": {
            "direction": "native",
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "media_scope": "all",
            "caption_style": "auto",
            "render_program": "native",
            "selected_media_ids": [MEDIA_ID],
            "target_duration_s": 14.8,
            "rationale": "Play the whole clip so every spoken line gets a caption.",
        },
        "summary": "I'll add captions to your clip and keep its original audio.",
    },
    "brief_updates": [
        {"kind": "style", "scope": "global", "literal": None, "description": "Add captions"}
    ],
}


class _SameAnswerClient:
    """Gives the same answer on every attempt, as the model did in prod."""

    def __init__(self, answer: dict[str, Any] = _ADD_CAPTIONS_ANSWER) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def invoke(self, **kwargs: Any) -> ModelInvocation:
        self.prompts.append(kwargs["prompt"])
        return ModelInvocation(raw_text=json.dumps(self.answer), tokens_in=10, tokens_out=20)


async def _plan(user_id: uuid.UUID, thread_id: uuid.UUID, item_id: uuid.UUID):  # noqa: ANN202
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            return await planner.plan_live_turn(
                db,
                thread_id=thread_id,
                item_id=item_id,
                creator_id=user_id,
                user_message="Add captions",
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_add_captions_on_one_clip_phone_talking_project_proposes_captions(
    monkeypatch: pytest.MonkeyPatch, prod_profile
) -> None:
    client = _SameAnswerClient()
    monkeypatch.setattr(planner, "default_client", lambda: client)
    user_id, thread_id, item_id = _seed()

    result = await _plan(user_id, thread_id, item_id)

    assert result.plan.mode == "act", result.plan.response
    assert len(client.prompts) == 1
    strategy = result.plan.intents[0].arguments["strategy"]
    assert strategy["edit_format"] == "subtitled"
    assert strategy["selected_media_ids"] == [MEDIA_ID]
    assert strategy["render_program"] == "native"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dropped",
    [("media_scope",), ("media_scope", "selected_media_ids")],
    ids=["no-scope", "no-scope-no-ids"],
)
async def test_flag_on_answer_without_media_scope_still_captions_the_clip(
    monkeypatch: pytest.MonkeyPatch, prod_profile, dropped: tuple[str, ...]
) -> None:
    # With CLIP_INTENTS_ENABLED on, the live Main Creator left `media_scope` out
    # of this answer on every flag-on eval run (2026-10-01 rollout). Only "all"
    # needs guided proposals, so an unset scope must still caption the one clip.
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    answer = copy.deepcopy(_ADD_CAPTIONS_ANSWER)
    for key in dropped:
        del answer["action"]["strategy"][key]
    client = _SameAnswerClient(answer)
    monkeypatch.setattr(planner, "default_client", lambda: client)
    user_id, thread_id, item_id = _seed()

    result = await _plan(user_id, thread_id, item_id)

    assert result.plan.mode == "act", result.plan.response
    assert len(client.prompts) == 1
    strategy = result.plan.intents[0].arguments["strategy"]
    assert strategy["edit_format"] == "subtitled"
    assert strategy["render_program"] == "native"
    assert strategy.get("media_scope") != "all"
    assert strategy["selected_media_ids"] == [MEDIA_ID]


@pytest.mark.asyncio
async def test_long_thread_sends_the_main_creator_its_bounded_latest_history(
    monkeypatch: pytest.MonkeyPatch, prod_profile
) -> None:
    # Prod thread e798bda2 failed every turn once it held more than 20 chat
    # messages: the planner loaded 24 and the Main Creator input allows 20.
    seen: list = []

    async def creator(inputs, **_kw):  # noqa: ANN001, ANN202
        seen.append(inputs.agent_input.conversation)
        return planner.MainCreatorOutput.model_validate(
            {"action": {"kind": "ask_user", "question": "Which part?", "reason_code": "x"}}
        )

    monkeypatch.setattr(planner, "_call_main_creator", creator)
    history = [
        ("user" if i % 2 else "assistant", f"message {i}")
        for i in range(MAIN_CREATOR_CONVERSATION_MAX + 10)
    ]
    user_id, thread_id, item_id = _seed(history=history)

    result = await _plan(user_id, thread_id, item_id)

    assert result.plan.mode == "respond"
    (conversation,) = seen
    assert len(conversation) == MAIN_CREATOR_CONVERSATION_MAX
    assert conversation[-1] == {"role": "user", "content": "Add captions"}
