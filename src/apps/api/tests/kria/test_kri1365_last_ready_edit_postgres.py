"""KRI-1365: a follow-up after approved wording edits the ready video, never the approved words.

Real submit -> worker -> planner -> draft commit on Postgres. Only the two model calls are
replaced: the brief extractor reads the follow-up as an edit, and the copilot's answer is parsed
by the real ``EditCopilotAgent`` against the snapshot the planner built.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents._model_client import ModelClient
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.config import settings
from app.database import sync_session
from app.kria import planner
from app.kria.api_schemas import SubmitTurnBody
from app.kria.brief import BriefUpdate
from app.kria.brief_binding import snapshot_media
from app.kria.runtime import submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentSession,
    CreatorEditDraft,
    Job,
    PlanItem,
)
from app.services.creative_copy_decisions import media_digest, wording_question
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_creative_brief import _MANIFEST
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures/kria_replay/kri1365_edit_route/fixture.json").read_text()
)
session_factory = async_sessionmaker(
    create_async_engine(settings.asyncpg_database_url, poolclass=NullPool), expire_on_commit=False
)


def _job(user_id, item_id, *, status, variants):  # noqa: ANN001, ANN202
    job_id = uuid.uuid4()
    variant_rows = copy.deepcopy(variants)
    for variant in variant_rows:
        for slot in variant["ai_timeline"]["slots"]:
            slot["source_gcs_path"] = slot["source_gcs_path"].replace(
                FIXTURE["ids"]["ready_job_id"], str(job_id)
            )
    return Job(
        id=job_id,
        user_id=user_id,
        status=status,
        mode="generative",
        raw_storage_path="",
        selected_platforms=["tiktok"],
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        all_candidates={"clip_paths": [clip["gcs_path"] for clip in FIXTURE["clips"]]},
        assembly_plan={"variants": variant_rows},
    )


def _seed(*, failed_current: bool):  # noqa: ANN202
    user_id, thread_id, session_id = _seed_runtime_project()
    variant = FIXTURE["variant"]
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        session = db.get(CreatorAgentSession, session_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        ready = _job(user_id, item.id, status="variants_ready", variants=[variant])
        db.add(ready)
        db.flush()
        current = ready
        if failed_current:
            current = _job(user_id, item.id, status="processing_failed", variants=[])
            current.failure_reason = "creation_text_failed"
            db.add(current)
            db.flush()
        item.current_job_id = current.id
        session.target_job_id = current.id
        session.target_variant_id = variant["variant_id"]
        session.target_generation_id = variant["render_generation_id"]
        session.manifest_hash = "a" * 64
        session.last_good = {
            "job_id": str(ready.id),
            "variant_id": variant["variant_id"],
            "render_generation_id": variant["render_generation_id"],
            "draft_id": None,
        }
        question = wording_question(
            target="opening_title",
            candidate=FIXTURE["approved_copy"]["text"],
            dependency_digest=media_digest(snapshot_media(item)),
        )
        db.add_all(
            [
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=2,
                    revision=3,
                    role="assistant",
                    event_type="choice_question",
                    content="Use this wording?",
                    payload={"choice_question": question},
                ),
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=3,
                    revision=4,
                    role="user",
                    event_type="user_message",
                    content="Approve",
                    payload={
                        "choice_selection": {
                            "question_id": question["question_id"],
                            "option_key": "approve",
                        }
                    },
                ),
            ]
        )
        thread.revision = 4
        thread.next_sequence = 4
        db.commit()
        return SimpleNamespace(
            user_id=user_id,
            thread_id=thread_id,
            session_id=session_id,
            item_id=item.id,
            ready_id=ready.id,
            current_id=current.id,
        )


def _agents(monkeypatch, case, raw_ops):  # noqa: ANN001, ANN202
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime.nudge_kria_observer", lambda *args: None)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    monkeypatch.setattr(planner, "default_client", lambda: object())
    monkeypatch.setattr(
        planner,
        "resolve_item_creator_context",
        AsyncMock(return_value=(_MANIFEST.model_copy(update={"item_id": str(case.item_id)}), [])),
    )
    monkeypatch.setattr(planner, "creator_context", lambda *a: ("creator", "item"))
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=None))
    monkeypatch.setattr(planner, "_copilot_clip_context", AsyncMock(return_value=None))
    creator = Mock(side_effect=AssertionError("A follow-up edit must not re-plan"))
    monkeypatch.setattr(planner, "MainCreatorAgent", creator)

    async def extractor(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        update = (
            BriefUpdate(
                kind="timing",
                scope="global",
                description="shorter",
                facts={"editor_timing_only": True},
            )
            if raw_ops is _shorter
            else BriefUpdate(kind="text", scope="title", description="change the title")
        )
        return SimpleNamespace(brief_updates=[update], request_scope="edit", clarification=None)

    monkeypatch.setattr(planner, "_call_brief_extractor", extractor)
    seen: list[str] = []

    async def copilot(body, *, job_id, **_kwargs):  # noqa: ANN001, ANN202
        seen.append(str(job_id))
        parsed = EditCopilotAgent(ModelClient()).parse(
            json.dumps(
                {
                    "intent": "edit",
                    "ops": raw_ops(body.snapshot),
                    "confidence": 0.9,
                    "reply": "Done.",
                    "suggestions": [],
                    "needs_clarification": False,
                }
            ),
            EditCopilotInput(
                utterance=body.message, prior_turns=[], variant_snapshot=body.snapshot
            ),
        )
        return SimpleNamespace(
            ops=parsed.ops,
            outcome="proposed",
            reply="Done.",
            intent="edit",
            rejection_reasons=[],
            unmet_requests=[],
            reply_notes=None,
        )

    monkeypatch.setattr(planner, "run_copilot_turn", copilot)

    async def planned(_snapshot, message, **_kwargs):  # noqa: ANN001, ANN202
        async with session_factory() as db:
            return await planner.plan_live_turn(
                db,
                thread_id=case.thread_id,
                item_id=case.item_id,
                creator_id=case.user_id,
                user_message=message,
            )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", planned)
    return seen, creator


def _shorter(snapshot):  # noqa: ANN001, ANN202
    target = round(float(snapshot["total_duration_s"]) * 0.7)
    return [
        {"op": "set_total_duration", "target_s": target, "strategy": "proportional"},
        *[
            {
                "op": "retime_text_sequence",
                "selector": {
                    "ids": [r["id"] for r in snapshot["text_bars"] if r["id"].startswith(prefix)]
                },
                "start_s": 0,
                "end_s": target,
                "gap_policy": "scale",
            }
            for prefix in ("title-tr", "translation-en")
        ],
    ]


def _rewrite(_snapshot):  # noqa: ANN001, ANN202
    return [
        {"op": "rewrite_text", "selector": {"ids": ["title-tr::sequence-3"]}, "text": "dünyada"}
    ]


async def _run(case, message):  # noqa: ANN001, ANN202
    async with session_factory() as db:
        expected = (await db.get(CreationThread, case.thread_id)).revision
        accepted, _ = await submit_turn(
            db,
            thread_id=case.thread_id,
            creator_id=case.user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=str(uuid.uuid4()),
                expected_thread_revision=expected,
            ),
        )
    return accepted, await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)


def _jobs(user_id) -> int:  # noqa: ANN001
    with sync_session() as db:
        return db.scalar(select(func.count()).select_from(Job).where(Job.user_id == user_id))


def _head(thread_id):  # noqa: ANN001, ANN202
    with sync_session() as db:
        return db.scalar(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True)
            )
        )


def _no_approval(turn_id) -> bool:  # noqa: ANN001
    with sync_session() as db:
        return (
            db.scalar(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.turn_id == uuid.UUID(turn_id)
                )
            )
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_current", "message"),
    [
        (False, "Make the video shorter, showing each text shorter"),
        (True, "Revert to the old version. Make the video shorter, showing each text shorter"),
    ],
)
async def test_follow_up_stages_a_shorter_edit_on_the_ready_video(
    monkeypatch, failed_current, message
):
    case = _seed(failed_current=failed_current)
    seen, creator = _agents(monkeypatch, case, _shorter)
    jobs = _jobs(case.user_id)

    accepted, result = await _run(case, message)

    assert result["status"] == "completed", result
    creator.assert_not_called()
    assert seen == [str(case.ready_id)]
    assert _no_approval(accepted.turn_id)
    assert _jobs(case.user_id) == jobs
    head = _head(case.thread_id)
    assert head is not None and head.base_job_id == case.ready_id
    payload = head.snapshot_json["editor_payload"]
    total = sum(slot["duration_s"] for slot in payload["timeline_slots"] if not slot["removed"])
    assert total < FIXTURE["variant"]["duration_s"] * 0.8
    words = {}
    for row in sorted(payload["text_elements"], key=lambda row: row["start_s"]):
        words.setdefault(row["id"].split("::")[0], []).append(row)
    assert " ".join(r["text"] for r in words["title-tr"]) == FIXTURE["approved_copy"]["text"]
    assert " ".join(r["text"] for r in words["translation-en"]) == FIXTURE["translation"]
    assert max(r["end_s"] for r in words["title-tr"]) == max(
        r["end_s"] for r in words["translation-en"]
    )
    assert max(r["end_s"] for r in payload["text_elements"]) <= total + 1e-6
    with sync_session() as db:
        assert db.get(PlanItem, case.item_id).current_job_id == case.ready_id
        session = db.get(CreatorAgentSession, case.session_id)
        assert session.target_job_id == case.ready_id
        assert session.target_variant_id == FIXTURE["variant"]["variant_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_current", [False, True])
async def test_an_edit_that_rewrites_approved_words_is_refused_and_changes_nothing(
    monkeypatch, failed_current
):
    case = _seed(failed_current=failed_current)
    _seen, creator = _agents(monkeypatch, case, _rewrite)

    accepted, result = await _run(case, "Change the third word of the title to dünyada")

    assert result["status"] == "completed", result
    creator.assert_not_called()
    assert _head(case.thread_id) is None
    assert _no_approval(accepted.turn_id)
    with sync_session() as db:
        assert db.get(PlanItem, case.item_id).current_job_id == case.current_id
        assert db.get(CreatorAgentSession, case.session_id).target_job_id == case.current_id
        reply = db.scalar(
            select(CreationThreadEvent.content)
            .where(
                CreationThreadEvent.thread_id == case.thread_id,
                CreationThreadEvent.role == "assistant",
            )
            .order_by(CreationThreadEvent.sequence.desc())
            .limit(1)
        )
    assert "wording you approved" in reply
