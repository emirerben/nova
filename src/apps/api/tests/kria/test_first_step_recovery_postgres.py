"""KRI-651: naming during the first model response cannot repeat provider work."""

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from app.agents._runtime import ModelInvocation, ProviderOutcomeUnknownError
from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.api_schemas import SubmitTurnBody
from app.kria.contracts import KriaObservedTurnResponse, KriaToolReceipt, KriaTurnPlan
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.kria.runtime import read_delta, submit_turn
from app.models import CreationThread, CreationThreadEvent, CreatorAgentTurn, PlanItem
from app.services import creation_thread_titles
from app.tasks.kria_runtime import (
    _append_sync_event,
    _claim,
    _complete_draft_turn,
    _complete_read_turn,
    _complete_response_turn,
    run_kria_turn,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project, _seed_user_turn


class TitleTransport:
    @property
    def aio(self):
        return self

    @property
    def models(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def generate_content(self, **_kwargs):
        return SimpleNamespace(text='{"title": "Marathon Finish"}')


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("language", ["en", "tr"])
async def test_first_hook_question_or_provider_failure_is_published_once_after_naming(
    monkeypatch, unknown, language
):
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    # The runtime's shared deadline must reach the actual provider transport.
    monkeypatch.setattr(run_kria_turn, "soft_time_limit", 45)
    monkeypatch.setattr("google.genai.Client", lambda **_kwargs: TitleTransport())
    user_id, thread_id, session_id = _seed_runtime_project()
    request = (
        "Add a hook about my marathon finish" if language == "en" else "Maraton için bir giriş yaz"
    )
    candidate = "Finish line feelings 😤" if language == "en" else "Bitiş çizgisinde 😤"
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.title = "Untitled video"
        thread.state = {**thread.state, "edit_format": "montage", "reply_language": language}
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.content_mode = "existing_footage"
        item.clip_gcs_paths = ["users/test/clip.mp4"]
        item.clip_assignments = [
            {
                "media_id": "clip-1",
                "gcs_path": "users/test/clip.mp4",
                "kind": "video",
                "duration_s": 4.1,
            }
        ]
        db.commit()
    async with AsyncSessionLocal() as db:
        accepted, publish = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=request, client_event_id=uuid.uuid4().hex, expected_thread_revision=2
            ),
        )
    assert publish
    loop = asyncio.get_running_loop()
    calls = []
    published = []

    class MainTransport:
        def invoke(self, **kwargs):
            calls.append(kwargs)
            # Force the real title event to commit while the model owns the turn.
            asyncio.run_coroutine_threadsafe(
                creation_thread_titles.generate_thread_title(thread_id), loop
            ).result(timeout=5)
            if unknown:
                raise ProviderOutcomeUnknownError("PRIVATE provider result unknown")
            return ModelInvocation(
                raw_text=json.dumps(
                    {
                        "action": {
                            "kind": "ask_user",
                            "question": "Use this hook?",
                            "reason_code": "hook_candidate",
                            "options": [],
                        },
                        "creative_decision": {
                            "target": "opening_title",
                            "status": "candidate",
                            "proposed_text": candidate,
                            "language": language,
                        },
                    }
                )
            )

    monkeypatch.setattr("app.kria.planner.default_client", lambda: MainTransport())
    monkeypatch.setattr(
        run_kria_turn, "apply_async", lambda **kwargs: published.append(kwargs["args"])
    )
    try:
        result = await asyncio.to_thread(run_kria_turn.run, str(accepted.turn_id))
    except ProviderOutcomeUnknownError:
        assert unknown
        result = {"status": "failed"}
    if result["status"] == "requeued":
        result = await asyncio.to_thread(run_kria_turn.run, str(accepted.turn_id))
    assert len(calls) == 1, "automatic naming repeated a provider request"
    assert 0 < calls[0]["timeout_s"] <= 25
    assert published == []
    async with AsyncSessionLocal() as db:
        delta = await read_delta(
            db, thread_id=thread_id, creator_id=user_id, after_sequence=1, limit=20
        )
    replies = [
        event
        for event in delta.events
        if event.role == "assistant" and event.event_type != "thread_title_generated"
    ]
    assert len(replies) == 1
    assert "PRIVATE" not in replies[0].content
    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, accepted.turn_id)
        assert turn.lease_epoch == 1
        assert db.get(CreationThread, thread_id).title == "Marathon Finish"
        if unknown:
            assert turn.status == "failed"
            assert turn.error["code"] == "runtime_provider_outcome_unknown"
            assert replies[0].payload["code"] == turn.error["code"]
            assert ("AI service" if language == "en" else "Yapay zekâ") in replies[0].content
        else:
            assert turn.status == "completed"
            assert replies[0].payload["choice_question"]["options"][0]["label"]
        source = db.get(CreationThreadEvent, turn.source_event_id)
        assert source.content == request


@pytest.mark.parametrize("completion_kind", ["response", "draft", "read"])
@pytest.mark.parametrize("change", ["title", "user", "incorrect_marker"])
def test_worker_completion_ignores_only_a_proven_title_change(monkeypatch, completion_kind, change):
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    _user_id, thread_id, session_id = _seed_runtime_project()
    turn_id = _seed_user_turn(thread_id, session_id, content="Plan my footage", status="pending")
    _snapshot, _message, epoch, revision, *_rest = _claim(turn_id, "owner")
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        _append_sync_event(
            db,
            thread,
            role="system",
            event_type="thread_title_generated",
            content=None,
            payload={"title": "New title"},
        )
        thread.state = {**thread.state, "title_generated_revision": thread.revision}
        if change == "user":
            _append_sync_event(
                db,
                thread,
                role="user",
                event_type="user_message",
                content="Use different footage",
                payload={},
            )
        elif change == "incorrect_marker":
            thread.state = {**thread.state, "title_generated_revision": thread.revision - 1}
        db.commit()
    common = {"lease_owner": "owner", "lease_epoch": epoch, "claimed_thread_revision": revision}
    if completion_kind == "response":
        completed = _complete_response_turn(
            turn_id,
            plan=KriaTurnPlan(mode="respond", turn_value="question", response="Choose the opening"),
            **common,
        )
    elif completion_kind == "draft":
        plan = adapt_creator_action(
            ProposeStrategy(
                kind="propose_strategy",
                strategy=CreativeStrategy(
                    direction="guided_story",
                    edit_format="day_vlog",
                    audio_strategy="licensed_music",
                    pacing="fast",
                    render_program="guided",
                    selected_media_ids=[],
                    rationale="Keep the footage",
                ),
                summary="A footage draft",
            )
        )
        completed = _complete_draft_turn(
            turn_id,
            planned=PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64),
            **common,
        )
    else:
        completed = _complete_read_turn(
            turn_id,
            plan=KriaTurnPlan(
                mode="act",
                turn_value="decision",
                intents=[
                    {"intent_id": "inspect", "tool_name": "project.inspect", "tool_version": 1}
                ],
            ),
            receipt=KriaToolReceipt(
                intent_id="inspect",
                tool_name="project.inspect",
                tool_version=1,
                status="completed",
                result={"editorial_decision": "Footage is ready"},
            ),
            response=KriaObservedTurnResponse(turn_value="decision", message="Footage is ready"),
            **common,
        )
    assert completed.committed is (change == "title")
    assert (completed.requeue_turn_id is not None) is (change != "title")
