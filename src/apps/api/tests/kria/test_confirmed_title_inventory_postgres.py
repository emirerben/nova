"""KRI-651: confirmed title placement survives the clip-operation inventory.

Failure modes recorded before implementation:
* an approved opening/closing title is questioned again as a chapter caption;
* a one-clip shortcut silently loses a separately requested caption;
* mixed requests miss the saved title decision or the actual available clip count;
* a new placement request or changed footage inherits an older title approval.

The durable submit/worker/wording-selection/draft/delta boundaries, Main Creator
and inventory parsers, and filename caption membership are real. Only model
transport is substituted. All footage and words below are synthetic.
"""

import asyncio
import json
import re
import uuid

import pytest

from app.agents._runtime import ModelInvocation
from app.agents._skills import load_skill
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.api_schemas import SubmitTurnBody
from app.kria.runtime import read_delta, submit_turn
from app.models import CreationThread, CreatorAgentTurn, CreatorEditDraft, PlanItem
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

AMBIGUOUS_PLACEMENT = (
    "Is the hook a title for the video, or a caption on a chapter or set of clips?"
)


def _confirmed_context(prompt):
    section = prompt.partition("Server-confirmed title placement")[2]
    matched = re.search(r"\n(\[[^\n]*\])\n\navailable_clip_count: (\d+)", section)
    if not matched:
        return [], None
    return json.loads(matched[1]), int(matched[2])


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "guided_edit_capability_enabled", True)


def _project(language, count):
    user_id, thread_id, session_id = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**thread.state, "edit_format": "montage", "reply_language": language}
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.content_mode = "existing_footage"
        item.clip_gcs_paths = [f"users/test/finish-{index}.mp4" for index in range(count)]
        item.clip_assignments = [
            {
                "media_id": f"clip-{index + 1}",
                "gcs_path": path,
                "source_filename": f"finish-{index + 1}.mov",
                "kind": "video",
                "duration_s": 4.1,
                "has_audio": True,
                "analysis": {"summary": "A runner at the finish line"},
            }
            for index, path in enumerate(item.clip_gcs_paths)
        ]
        db.commit()
    return user_id, thread_id, session_id


async def _submit_and_run(user_id, thread_id, message, *, choice_selection=None):
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        accepted, publish = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=uuid.uuid4().hex,
                expected_thread_revision=revision,
                **({"choice_selection": choice_selection} if choice_selection else {}),
            ),
        )
    assert publish
    result = await asyncio.to_thread(run_kria_turn.run, str(accepted.turn_id))
    async with AsyncSessionLocal() as db:
        delta = await read_delta(
            db, thread_id=thread_id, creator_id=user_id, after_sequence=1, limit=100
        )
    events = [
        event
        for event in delta.events
        if event.payload and event.payload.get("turn_id") == str(accepted.turn_id)
    ]
    return accepted, result, events


class CopyTransport:
    def __init__(
        self,
        *,
        target,
        candidate,
        request,
        language,
        count,
        caption=False,
        changed=False,
        omit_caption_brief=False,
        reuse_title_caption=False,
        copy_choice="approve",
    ):
        self.target = target
        self.candidate = candidate
        self.request = request
        self.language = language
        self.count = count
        self.caption = caption
        self.changed = changed
        self.omit_caption_brief = omit_caption_brief
        self.caption_text = candidate if reuse_title_caption else "Made it 😤"
        self.copy_choice = copy_choice
        self.main_calls = 0
        self.inventory_prompts = []

    def invoke(self, *, model, prompt, **_kwargs):
        if model == "gemini-3.1-pro-preview":
            self.main_calls += 1
            if self.main_calls == 1:
                updates = [{"kind": "text", "scope": "title", "description": "Add a hook title"}]
                if self.caption and not self.omit_caption_brief:
                    updates.append(
                        {
                            "kind": "text",
                            "scope": "global",
                            "description": "Put the requested words on finish-1.mov",
                        }
                    )
                answer = {
                    "action": {
                        "kind": "ask_user",
                        "question": "Use this hook?",
                        "reason_code": "hook_candidate",
                        "options": [],
                    },
                    "creative_decision": {
                        "target": self.target,
                        "status": "candidate",
                        "proposed_text": self.candidate,
                        "language": self.language,
                    },
                    "brief_updates": updates,
                }
            else:
                answer = {
                    "action": {
                        "kind": "propose_strategy",
                        "strategy": {
                            "direction": "guided_story",
                            "edit_format": "montage",
                            "render_program": "guided",
                            "audio_strategy": "original_audio",
                            "media_scope": "all",
                            "selected_media_ids": [],
                            "target_duration_s": 4.1 * self.count,
                            "rationale": "Keep the available footage with the approved words.",
                            self.target: self.candidate if self.copy_choice == "approve" else None,
                        },
                        "summary": "Use your chosen title.",
                    },
                    "brief_updates": (
                        [
                            {
                                "operation": "change",
                                "target_requirement_id": "r1",
                                "expected_version": 1,
                                "kind": "text",
                                "scope": "title",
                                "description": "Put the hook on a selected chapter instead",
                            }
                        ]
                        if self.changed
                        else [
                            {
                                "operation": "change",
                                "target_requirement_id": "r1",
                                "expected_version": 1,
                                "kind": "text",
                                "scope": "title",
                                "literal": self.candidate,
                                "description": "Closing title at the end",
                            }
                        ]
                        if self.target == "closing_title"
                        else []
                    ),
                }
        elif prompt.startswith("You extract the COMPLETE inventory"):
            self.inventory_prompts.append(prompt)
            if self.caption:
                answer = {
                    "intents": [
                        {
                            "intent_id": "finish-caption",
                            "op": "caption",
                            "attribute": "finish-1.mov",
                            "creator_text": self.caption_text,
                            "source_quote": f'Put "{self.caption_text}" on finish-1.mov',
                        }
                    ],
                    "question": None,
                }
            elif self.changed or not (
                _confirmed_context(prompt)
                == ([{"target": self.target, "text": self.candidate}], self.count)
            ):
                answer = {
                    "intents": [],
                    "question": None if self.copy_choice != "approve" else AMBIGUOUS_PLACEMENT,
                }
            else:
                answer = {"intents": [], "question": None}
        elif prompt.startswith(
            load_skill("meaningful-moment") + "\n\nYou resolve a creator's open-vocabulary request"
        ):
            answer = {
                "intents": [
                    {
                        "intent_id": "finish-caption",
                        "assignments": [
                            {
                                "media": "m001",
                                "evidence": "A runner at the finish line",
                                "confidence": 1.0,
                            }
                        ],
                        "needs_vision": [],
                        "question": None,
                        "caption": None,
                    }
                ]
            }
        else:
            raise AssertionError("Unexpected provider transport call")
        return ModelInvocation(raw_text=json.dumps(answer, ensure_ascii=False))


def _wire(monkeypatch, transport):
    monkeypatch.setattr("app.kria.planner.default_client", lambda: transport)
    monkeypatch.setattr("app.services.clip_intent_planning.default_client", lambda: transport)
    monkeypatch.setattr("app.services.clip_intent_resolution.default_client", lambda: transport)


async def _suggest(user_id, thread_id, transport):
    accepted, result, events = await _submit_and_run(user_id, thread_id, transport.request)
    assert result["status"] == "completed"
    reply = next(event for event in events if event.event_type == "assistant_response")
    question = reply.payload["choice_question"]
    assert question["creative_target"] == transport.target
    assert question["candidate"] == transport.candidate
    assert not transport.inventory_prompts
    return question


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "language,target,count",
    [
        ("en", "opening_title", 1),
        ("tr", "opening_title", 1),
        ("en", "closing_title", 2),
        ("tr", "closing_title", 1),
    ],
)
async def test_approved_title_reaches_render_approval_without_reopening_placement(
    monkeypatch, language, target, count
):
    user_id, thread_id, _session_id = _project(language, count)
    candidate = "Finish line açaí 😤" if language == "en" else "Bitiş çizgisinde açaí 😤"
    transport = CopyTransport(
        target=target,
        candidate=candidate,
        request="Suggest a hook about the finish line",
        language=language,
        count=count,
    )
    _wire(monkeypatch, transport)
    await _suggest(user_id, thread_id, transport)
    accepted, result, events = await _submit_and_run(
        user_id, thread_id, "Use this wording" if language == "en" else "Bu ifadeyi kullan"
    )
    assert result["status"] == "awaiting_approval", "approved title placement was reopened"
    assert len(transport.inventory_prompts) == 1
    prompt = transport.inventory_prompts[0]
    assert _confirmed_context(prompt) == ([{"target": target, "text": candidate}], count)
    assert any(event.event_type == "approval_requested" for event in events)
    assert all(AMBIGUOUS_PLACEMENT != event.content for event in events)
    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, accepted.turn_id)
        assert turn.status == "awaiting_approval"
        draft_id = next(
            event.payload["draft_id"] for event in events if event.event_type == "draft_applied"
        )
        draft = db.get(CreatorEditDraft, uuid.UUID(draft_id))
        assert draft.snapshot_json["strategy"][target] == candidate


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "language,omit_caption_brief,reuse_title_caption",
    [
        ("en", False, False),
        ("tr", False, False),
        ("en", True, True),
    ],
)
async def test_one_clip_title_with_requested_caption_still_inventories_exact_caption(
    monkeypatch, language, omit_caption_brief, reuse_title_caption
):
    user_id, thread_id, _session_id = _project(language, 1)
    candidate = "Finish line açaí 😤"
    caption_text = candidate if reuse_title_caption else "Made it 😤"
    transport = CopyTransport(
        target="opening_title",
        candidate=candidate,
        request=f'Suggest a hook. Put "{caption_text}" on finish-1.mov',
        language=language,
        count=1,
        caption=True,
        omit_caption_brief=omit_caption_brief,
        reuse_title_caption=reuse_title_caption,
    )
    _wire(monkeypatch, transport)
    await _suggest(user_id, thread_id, transport)
    accepted, result, events = await _submit_and_run(user_id, thread_id, "Use this wording")
    assert result["status"] == "awaiting_approval"
    assert len(transport.inventory_prompts) == 1
    prompt = transport.inventory_prompts[0]
    assert _confirmed_context(prompt) == ([{"target": "opening_title", "text": candidate}], 1)
    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, accepted.turn_id)
        assert turn.status == "awaiting_approval"
        draft_id = next(
            event.payload["draft_id"] for event in events if event.event_type == "draft_applied"
        )
        draft = db.get(CreatorEditDraft, uuid.UUID(draft_id))
        strategy = draft.snapshot_json["strategy"]
        assert strategy["opening_title"] == candidate
        captions = [
            intent for intent in strategy["resolved_clip_intents"] if intent["op"] == "caption"
        ]
        assert len(captions) == 1
        assert captions[0]["creator_text"] == caption_text
        assert captions[0]["assignments"][0]["media_id"] == "clip-1"


@pytest.mark.asyncio
async def test_fresh_description_only_placement_change_does_not_use_title_only_shortcut(
    monkeypatch,
):
    user_id, thread_id, _session_id = _project("en", 1)
    transport = CopyTransport(
        target="opening_title",
        candidate="Finish line açaí 😤",
        request="Suggest a hook about the finish line",
        language="en",
        count=1,
        changed=True,
    )
    _wire(monkeypatch, transport)
    question = await _suggest(user_id, thread_id, transport)
    _accepted, result, events = await _submit_and_run(
        user_id,
        thread_id,
        "Use this wording, but put the hook on a selected chapter instead",
        choice_selection={"question_id": question["question_id"], "option_key": "approve"},
    )
    assert result["status"] == "completed"
    assert len(transport.inventory_prompts) == 1
    assert not any(event.event_type == "approval_requested" for event in events)


@pytest.mark.asyncio
async def test_changed_footage_requires_a_new_wording_approval(monkeypatch):
    user_id, thread_id, _session_id = _project("en", 1)
    transport = CopyTransport(
        target="opening_title",
        candidate="Finish line açaí 😤",
        request="Suggest a hook about the finish line",
        language="en",
        count=1,
    )
    _wire(monkeypatch, transport)
    await _suggest(user_id, thread_id, transport)
    # Accept against the original footage, then change the media identity before
    # the worker plans. The durable fold must refuse the now-stale approval.
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        accepted, publish = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message="Use this wording",
                client_event_id=uuid.uuid4().hex,
                expected_thread_revision=revision,
            ),
        )
    assert publish
    with sync_session() as db:
        item = db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id)
        item.clip_assignments = [{**item.clip_assignments[0], "media_id": "replacement-clip"}]
        db.commit()
    result = await asyncio.to_thread(run_kria_turn.run, str(accepted.turn_id))
    assert result["status"] == "completed"
    async with AsyncSessionLocal() as db:
        delta = await read_delta(
            db, thread_id=thread_id, creator_id=user_id, after_sequence=1, limit=100
        )
    newest = [
        event
        for event in delta.events
        if event.payload and event.payload.get("turn_id") == str(accepted.turn_id)
    ]
    assert not any(event.event_type == "approval_requested" for event in newest)
    question = next(
        event.payload["choice_question"] for event in newest if event.payload.get("choice_question")
    )
    assert question["creative_target"] == "opening_title"
    assert transport.inventory_prompts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["cancel", "revise"])
async def test_declined_or_revised_candidate_is_never_sent_as_confirmed_copy(monkeypatch, choice):
    user_id, thread_id, _session_id = _project("en", 1)
    transport = CopyTransport(
        target="opening_title",
        candidate="Finish line açaí 😤",
        request="Suggest a hook about the finish line",
        language="en",
        count=1,
        copy_choice=choice,
    )
    _wire(monkeypatch, transport)
    question = await _suggest(user_id, thread_id, transport)
    _accepted, result, events = await _submit_and_run(
        user_id,
        thread_id,
        "Skip it" if choice == "cancel" else "Revise it",
        choice_selection={"question_id": question["question_id"], "option_key": choice},
    )
    assert len(transport.inventory_prompts) == 1
    assert _confirmed_context(transport.inventory_prompts[0]) == ([], 1)
    if choice == "cancel":
        assert result["status"] == "awaiting_approval"
        draft_id = next(
            event.payload["draft_id"] for event in events if event.event_type == "draft_applied"
        )
        with sync_session() as db:
            strategy = db.get(CreatorEditDraft, uuid.UUID(draft_id)).snapshot_json["strategy"]
            assert not strategy.get("opening_title")
            assert strategy["omitted_copy_targets"] == ["opening_title"]
    else:
        assert result["status"] == "completed"
        assert not any(event.event_type == "approval_requested" for event in events)
        assert any(event.payload.get("choice_question") for event in events)
