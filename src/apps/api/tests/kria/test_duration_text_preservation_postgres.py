"""Database-backed editor turns preserve authored text or ask before staging."""

from __future__ import annotations

import asyncio
import copy
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.drafts import read_or_bootstrap_draft, undo_draft
from app.models import CreationThreadEvent, CreatorAgentExecution, Job
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_editor_state_turns_postgres import (
    _drafts,
    _plan_with,
    _seed_job,
    _submit,
    _turn,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project
from tests.services._guided_timeline_fixtures import guided_revision


def _text_rows():
    return [
        {
            "id": f"opening::sequence-{i + 1}",
            "text": f"word{i + 1}",
            "start_s": round(i * 12.876 / 27, 6),
            "end_s": round((i + 1) * 12.876 / 27, 6),
            "role": "generative_intro",
            "position": "middle",
            "font_family": "Playfair Display",
            "size_px": 111 if i == 2 else 76,
            "max_width_frac": 0.68,
            "source_params": {"sequence_source_id": "opening"},
        }
        for i in range(27)
    ]


@pytest.fixture
async def project(monkeypatch):
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "kria_editor_state_turns_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "guided_story_editor_v2_enabled", True)
    enqueue = Mock(side_effect=AssertionError("A staged edit must not enqueue rendering"))
    monkeypatch.setattr("app.tasks.kria_runtime.enqueue_editor_commit_render", enqueue)
    user_id, thread_id, session_id = _seed_runtime_project()
    job_id = _seed_job(user_id, session_id)
    rows = _text_rows()
    revision = guided_revision(str(job_id), count=28, duration_s=2, text_elements=rows)
    revision["base_generation"] = "generation-1"
    revision["state_hash"] = ""
    with sync_session() as db:
        job = db.get(Job, job_id)
        variant = copy.deepcopy(job.assembly_plan["variants"][0])
        variant.update(
            resolved_archetype="guided_story",
            text_elements=rows,
            guided_edit_revision=revision,
        )
        job.assembly_plan = {
            "variants": [variant],
            "guided_edit": {"proposal_version": 1, "media_digest": "a" * 64},
            "guided_story_execution_plan": {},
        }
        db.commit()
        original = copy.deepcopy(job.assembly_plan)
    async with AsyncSessionLocal() as db:
        await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
    try:
        yield user_id, thread_id, session_id, job_id, rows, original, enqueue
    finally:
        await async_engine.dispose()


def _plan_duration(monkeypatch, rows, target):
    _plan_with(
        monkeypatch,
        [
            {
                "op": "remove_texts",
                "selector": {"group": "all"},
                "target_ids": [r["id"] for r in rows],
                "expected_count": 27,
            },
            {"op": "add_text", "text": "Unrequested replacement", "start_s": 0, "end_s": 1},
            {"op": "set_total_duration", "target_s": target, "strategy": "proportional"},
        ],
    )


def _assert_job_unmodified(project):
    user_id, _, _, job_id, _, original, enqueue = project
    with sync_session() as db:
        job = db.get(Job, job_id)
        assert job.assembly_plan == original
        assert job.status == "variants_ready"
        assert list(db.scalars(select(Job.id).where(Job.user_id == user_id))) == [job_id]
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_nonfitting_preserved_text_asks_without_mutating_head_or_execution(
    project, monkeypatch
):
    user_id, thread_id, session_id, _, rows, _, _ = project
    before = _drafts(thread_id)
    _plan_duration(monkeypatch, rows, 12)
    accepted = await _submit(
        user_id,
        thread_id,
        "Don’t touch the text, shorten all clips to make the video 12 seconds",
    )
    assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))["status"] == "completed"
    assert _turn(accepted.turn_id) == (None, "completed")
    assert _drafts(thread_id) == before
    with sync_session() as db:
        replies = db.scalars(
            select(CreationThreadEvent).where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.event_type == "assistant_response",
            )
        ).all()
        assert len(replies) == 1
        assert replies[0].payload["turn_value"] == "question"
        assert "12.9" in replies[0].content
        assert "text timing" in replies[0].content
        assert (
            db.scalars(
                select(CreatorAgentExecution).where(
                    CreatorAgentExecution.session_id == session_id,
                )
            ).all()
            == []
        )
    _assert_job_unmodified(project)


@pytest.mark.asyncio
async def test_retime_submit_through_real_planner_persists_words_and_receipt(project, monkeypatch):
    user_id, thread_id, session_id, _, rows, _, _ = project
    before = _drafts(thread_id)
    providers = []
    for name in ("run_copilot_turn", "_call_main_creator", "_call_brief_extractor"):
        provider = AsyncMock(side_effect=AssertionError("Retime must not call a provider"))
        monkeypatch.setattr(f"app.kria.planner.{name}", provider)
        providers.append(provider)

    accepted = await _submit(user_id, thread_id, "Retime text to 12s")
    assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))["status"] == "completed"
    assert _turn(accepted.turn_id) == (None, "completed")
    drafts = _drafts(thread_id)
    assert len(drafts) == len(before) + 1
    head = next(d for d in drafts if d["head"])
    assert head["parent"] == before[-1]["id"]
    payload = head["snap"]["editor_payload"]
    retimed = payload["text_elements"]
    assert len(retimed) == 27
    for old, new in zip(rows, retimed, strict=True):
        assert {k: v for k, v in new.items() if k not in {"start_s", "end_s"}} == {
            k: v for k, v in old.items() if k not in {"start_s", "end_s"}
        }
        assert new["end_s"] - new["start_s"] >= 0.25
    assert retimed[0]["start_s"] == 0
    assert retimed[-1]["end_s"] == 12
    assert all(a["end_s"] == b["start_s"] for a, b in zip(retimed, retimed[1:]))
    assert sum(Decimal(str(slot["duration_s"])) for slot in payload["timeline_slots"]) == 12
    with sync_session() as db:
        execution = db.scalars(
            select(CreatorAgentExecution).where(CreatorAgentExecution.session_id == session_id)
        ).one()
        assert execution.tool_name == "draft.apply_editor_ops"
        assert execution.status == "completed"
        assert execution.target_draft_id == head["id"]
        assert execution.target_draft_revision == head["rev"]
        receipt = db.scalars(
            select(CreationThreadEvent).where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.event_type == "draft_applied",
            )
        ).one()
        assert receipt.payload["draft_id"] == str(head["id"])
        assert receipt.payload["receipt_ids"] == [str(execution.id)]
        assert receipt.payload["render_requested"] is False
        assert "all 27 words in sequence over 12s" in receipt.content
        assert "keeping their wording and appearance" in receipt.content
        assert "rendered" not in receipt.content.lower()
        assert "saved" not in receipt.content.lower()
    for provider in providers:
        provider.assert_not_called()
    _assert_job_unmodified(project)


@pytest.mark.asyncio
async def test_fitting_duration_preserves_text_after_undo_and_resubmission(project, monkeypatch):
    user_id, thread_id, _, _, rows, _, _ = project
    original_head = _drafts(thread_id)[-1]
    _plan_duration(monkeypatch, rows, 20)
    accepted = await _submit(
        user_id,
        thread_id,
        "Don’t touch the text, shorten all clips to make the video 20 seconds",
    )
    assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))["status"] == "completed"
    head = _drafts(thread_id)[-1]
    payload = head["snap"]["editor_payload"]
    assert payload["text_elements"] == rows
    assert sum(slot["duration_s"] for slot in payload["timeline_slots"]) == pytest.approx(
        20, abs=0.05
    )
    _assert_job_unmodified(project)
    async with AsyncSessionLocal() as db:
        await undo_draft(db, thread_id=thread_id, creator_id=user_id, expected_revision=head["rev"])
    restored = next(d for d in _drafts(thread_id) if d["head"])
    assert restored["snap"] == original_head["snap"]
    _plan_duration(monkeypatch, rows, 12)
    before_retry = _drafts(thread_id)
    retry = await _submit(
        user_id,
        thread_id,
        "Don’t touch the text, shorten all clips to make the video 12 seconds",
        {
            "base_generation": "generation-1",
            "client_state_id": "restored-state",
            "lanes": {"text_elements": rows},
        },
    )
    assert (await asyncio.to_thread(run_kria_turn.run, retry.turn_id))["status"] == "completed"
    assert _drafts(thread_id) == before_retry
    assert _turn(retry.turn_id) == (None, "completed")
    with sync_session() as db:
        reply = db.scalars(
            select(CreationThreadEvent)
            .where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.event_type == "assistant_response",
            )
            .order_by(CreationThreadEvent.sequence.desc())
        ).first()
        assert reply.payload["turn_value"] == "question"
        assert "12.9" in reply.content
    _assert_job_unmodified(project)


@pytest.mark.asyncio
async def test_timing_only_request_rejects_replacement_without_persisting(project, monkeypatch):
    user_id, thread_id, session_id, _, rows, _, _ = project
    before = _drafts(thread_id)
    _plan_with(
        monkeypatch,
        [
            {
                "op": "remove_texts",
                "selector": {"group": "all"},
                "target_ids": [r["id"] for r in rows],
                "expected_count": 27,
            },
            {
                "op": "add_text",
                "text": " ".join(r["text"] for r in rows),
                "start_s": 0,
                "end_s": 56,
            },
        ],
    )
    accepted = await _submit(
        user_id,
        thread_id,
        "The text are overlapping. They shouldn’t overlap "
        "and the last one should end at the end of the video",
    )
    assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))["status"] == "completed"
    assert _turn(accepted.turn_id) == (None, "completed")
    assert _drafts(thread_id) == before
    with sync_session() as db:
        reply = db.scalars(
            select(CreationThreadEvent).where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.event_type == "assistant_response",
            )
        ).one()
        assert reply.payload["turn_value"] == "recovery"
        assert "keep every word and its appearance" in reply.content
        assert "Nothing was changed" in reply.content
        assert (
            db.scalars(
                select(CreatorAgentExecution).where(
                    CreatorAgentExecution.session_id == session_id,
                )
            ).all()
            == []
        )
    _assert_job_unmodified(project)


@pytest.mark.asyncio
async def test_explicit_retime_recovery_stages_gapless_existing_words(project, monkeypatch):
    from app.kria.editor_sequence_timing import sequence_timing_plan
    from app.services.kria_editor_ops import build_editor_snapshot

    user_id, thread_id, _, job_id, rows, _, _ = project
    before = _drafts(thread_id)
    with sync_session() as db:
        job = db.get(Job, job_id)
        snapshot = build_editor_snapshot(job, job.assembly_plan["variants"][0])
    plan = sequence_timing_plan("Retime text to 12s", snapshot)
    assert plan is not None and plan.operations
    _plan_with(monkeypatch, list(plan.operations))
    accepted = await _submit(user_id, thread_id, "Retime text to 12s")
    assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))["status"] == "completed"
    assert _turn(accepted.turn_id) == (None, "completed")
    drafts = _drafts(thread_id)
    assert len(drafts) == len(before) + 1
    head = next(d for d in drafts if d["head"])
    payload = head["snap"]["editor_payload"]
    retimed = payload["text_elements"]
    assert len(retimed) == 27
    for old, new in zip(rows, retimed, strict=True):
        assert {k: v for k, v in new.items() if k not in {"start_s", "end_s"}} == {
            k: v for k, v in old.items() if k not in {"start_s", "end_s"}
        }
        assert new["end_s"] - new["start_s"] >= 0.25
    assert retimed[0]["start_s"] == 0
    assert retimed[-1]["end_s"] == 12
    assert all(a["end_s"] == b["start_s"] for a, b in zip(retimed, retimed[1:]))
    assert sum(slot["duration_s"] for slot in payload["timeline_slots"]) == pytest.approx(
        12, abs=0.05
    )
    _assert_job_unmodified(project)
