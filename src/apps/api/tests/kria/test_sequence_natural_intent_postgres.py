"""Natural sequence edits through the brief-enabled runtime and real database."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.brief import BriefRequirement, load_latest_brief_sync
from app.kria.drafts import undo_draft
from app.models import (
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentExecution,
    CreatorAgentSession,
    PlanItem,
)
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_duration_text_preservation_postgres import (
    _assert_job_unmodified,
)
from tests.kria.test_duration_text_preservation_postgres import (
    project as sequence_project,  # noqa: F401
)
from tests.kria.test_editor_state_turns_postgres import _drafts, _submit, _turn
from tests.kria.test_kri506_postgres import _ask


@pytest.fixture
async def brief_project(sequence_project, monkeypatch):  # noqa: F811
    project = sequence_project
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    requirement = BriefRequirement(
        id="r1", kind="audio", scope="global", description="Keep the original audio"
    )
    with sync_session() as db:
        item = db.get(PlanItem, db.get(CreatorAgentSession, project[2]).plan_item_id)
        item.clip_gcs_paths = ["users/test/matcha.mp4"]
        item.clip_assignments = [
            {
                "media_id": "clip-1",
                "gcs_path": "users/test/matcha.mp4",
                "kind": "video",
                "duration_s": 56,
                "analysis": {"description": "Whisking matcha", "has_speech": False},
            }
        ]
        db.add(
            CreativeBriefVersion(
                thread_id=project[1], version=1, requirements=[requirement.model_dump()]
            )
        )
        db.commit()
    monkeypatch.setattr(
        "app.kria.planner._kick_clip_understanding",
        AsyncMock(side_effect=AssertionError("Source analysis must already be available")),
    )
    providers = {}
    for name in ("_call_brief_extractor", "_call_main_creator", "run_copilot_turn"):
        providers[name] = AsyncMock(side_effect=AssertionError(f"Provider forbidden: {name}"))
        monkeypatch.setattr(f"app.kria.planner.{name}", providers[name])
    return project, providers, requirement


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "duration"),
    [
        ("Fill the text the whole video", 56),
        ("Fit text to video length", 56),
        ("Yazıları videonun tamamına yay", 56),
        ("Make the video 12 seconds and fill the text across the whole video", 12),
    ],
)
async def test_natural_sequence_submit_stages_complete_text_and_brief(
    brief_project, message, duration
):
    project, providers, unrelated = brief_project
    user_id, thread_id, session_id, _, rows, _, _ = project
    before = _drafts(thread_id)
    accepted = await _submit(user_id, thread_id, message)
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "completed"
    assert _turn(accepted.turn_id) == (None, "completed")
    for provider in providers.values():
        provider.assert_not_called()
    drafts = _drafts(thread_id)
    assert len(drafts) == len(before) + 1
    head = next(draft for draft in drafts if draft["head"])
    assert head["parent"] == next(draft["id"] for draft in before if draft["head"])
    payload = head["snap"]["editor_payload"]
    retimed = payload["text_elements"]
    assert len(retimed) == len(rows) == 27
    for old, new in zip(rows, retimed, strict=True):
        for key, value in old.items():
            if key not in {"start_s", "end_s", "source_params"}:
                assert new[key] == value
        assert old["source_params"].items() <= new["source_params"].items()
        timing = new["source_params"]["sequence_timing"]
        assert timing["duration_s"] > 0
        assert timing["gap_after_s"] >= 0
        assert new["end_s"] - new["start_s"] >= 0.25
    assert retimed[0]["start_s"] == 0
    assert retimed[-1]["end_s"] == duration
    assert all(a["end_s"] == b["start_s"] for a, b in zip(retimed, retimed[1:]))
    slots = payload.get("timeline_slots")
    if slots is None:
        slots = project[5]["variants"][0]["guided_edit_revision"]["segments"]
    assert sum(Decimal(str(slot["duration_s"])) for slot in slots) == duration
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
        assert "27" in receipt.content and str(duration) in receipt.content
        assert "rendered" not in receipt.content.lower()
        assert "saved" not in receipt.content.lower()
        brief = load_latest_brief_sync(db, thread_id)
        assert unrelated in brief.live()
        new_requirements = [r for r in brief.live() if r.source_turn_id == str(accepted.turn_id)]
        assert new_requirements, "The deterministic edit must enter the durable brief ledger"
        assert any(r.kind in {"timing", "text_sequence"} for r in new_requirements)
        assert {r.id for r in new_requirements} <= set(
            head["snap"]["brief_coverage"]["applicable_ids"]
        )
    _assert_job_unmodified(project)


@pytest.mark.asyncio
async def test_preservation_conflict_asks_without_retiming(brief_project):
    project, providers, _ = brief_project
    user_id, thread_id, session_id, *_ = project
    before = _drafts(thread_id)
    accepted = await _submit(user_id, thread_id, "Don't touch text, make video 12")
    assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))["status"] == "completed"
    assert _drafts(thread_id) == before
    for provider in providers.values():
        provider.assert_not_called()
    with sync_session() as db:
        reply = db.scalars(
            select(CreationThreadEvent).where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.event_type == "assistant_response",
            )
        ).one()
        assert reply.payload["turn_value"] == "question"
        assert "12.9" in reply.content
        assert not db.scalars(
            select(CreatorAgentExecution).where(CreatorAgentExecution.session_id == session_id)
        ).all()
    _assert_job_unmodified(project)


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["stale_editor", "pending_copy", "style_compound"])
async def test_sequence_shortcut_never_discards_guard_or_compound_request(brief_project, guard):
    project, providers, _ = brief_project
    user_id, thread_id, session_id, _, rows, *_ = project
    state = None
    message = "Fill the text the whole video"
    if guard == "pending_copy":
        _ask(thread_id, session_id)
    elif guard == "stale_editor":
        state = {
            "base_generation": "obsolete-generation",
            "client_state_id": "stale-sequence-state",
            "lanes": {"text_elements": rows},
        }
    else:
        message += " and make all text red"
    before = _drafts(thread_id)
    accepted = await _submit(user_id, thread_id, message, state)
    if guard in {"pending_copy", "style_compound"}:
        with pytest.raises(AssertionError, match="Provider forbidden"):
            await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    else:
        assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))[
            "status"
        ] == "completed"
        for provider in providers.values():
            provider.assert_not_called()
    assert _drafts(thread_id) == before, "A shortcut must not stage only the timing clause"
    _assert_job_unmodified(project)
    if guard == "pending_copy":
        providers["_call_main_creator"].assert_called()


@pytest.mark.asyncio
async def test_sequence_multiturn_undo_reconciles_duration_ledger(brief_project):
    project, providers, unrelated = brief_project
    user_id, thread_id, session_id, _, original_rows, _, _ = project
    stable_ids = None
    twelve_second_rows = None

    def assert_current(video_duration, text_duration, policy_mode, execution_count):
        head = next(draft for draft in _drafts(thread_id) if draft["head"])
        payload = head["snap"]["editor_payload"]
        rows = payload["text_elements"]
        assert (
            sum(Decimal(str(slot["duration_s"])) for slot in payload["timeline_slots"])
            == video_duration
        )
        assert len(rows) == len(original_rows) == 27
        assert rows[0]["start_s"] == 0
        assert rows[-1]["end_s"] == text_duration
        assert all(a["end_s"] == b["start_s"] for a, b in zip(rows, rows[1:]))
        for original, row in zip(original_rows, rows, strict=True):
            assert row["end_s"] - row["start_s"] >= 0.25
            for key, value in original.items():
                if key not in {"start_s", "end_s", "source_params"}:
                    assert row[key] == value
            assert original["source_params"].items() <= row["source_params"].items()
        with sync_session() as db:
            brief = load_latest_brief_sync(db, thread_id)
            assert unrelated in brief.live()
            durations = [
                r
                for r in brief.live()
                if r.kind == "timing" and r.scope == "global" and "duration_s" in r.facts
            ]
            policies = [r for r in brief.live() if "sequence_timing" in r.facts]
            assert len(durations) == len(policies) == 1
            assert durations[0].facts["duration_s"] == video_duration
            assert policies[0].facts["sequence_timing"]["mode"] == policy_mode
            assert policies[0].facts["sequence_timing"]["source_id"] == "opening"
            assert policies[0].facts["sequence_timing"]["target_ids"] == [
                r["id"] for r in original_rows
            ]
            ids = (durations[0].id, policies[0].id)
            if stable_ids is not None:
                assert ids == stable_ids
            executions = db.scalars(
                select(CreatorAgentExecution).where(CreatorAgentExecution.session_id == session_id)
            ).all()
            assert len(executions) == execution_count
            assert all(
                e.status == "completed" and e.tool_name == "draft.apply_editor_ops"
                for e in executions
            )
        for provider in providers.values():
            provider.assert_not_called()
        _assert_job_unmodified(project)
        return head, ids

    for index, (message, video_duration, text_duration, mode) in enumerate(
        [
            ("Make the video 12 seconds and fill the text across the whole video", 12, 12, "fill"),
            ("Make the video 20 seconds", 20, 12, "absolute"),
            ("Make the video 30 seconds and fill the text across the whole video", 30, 30, "fill"),
        ],
        start=1,
    ):
        before = _drafts(thread_id)
        accepted = await _submit(user_id, thread_id, message)
        assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))[
            "status"
        ] == "completed"
        assert len(_drafts(thread_id)) == len(before) + 1
        head, ids = assert_current(video_duration, text_duration, mode, index)
        stable_ids = ids
        if index == 1:
            twelve_second_rows = head["snap"]["editor_payload"]["text_elements"]
        elif index == 2:
            assert head["snap"]["editor_payload"]["text_elements"] == twelve_second_rows
        after = _drafts(thread_id)
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert _drafts(thread_id) == after
        assert_current(video_duration, text_duration, mode, index)

    async with AsyncSessionLocal() as db:
        await undo_draft(db, thread_id=thread_id, creator_id=user_id, expected_revision=head["rev"])
    restored = next(draft for draft in _drafts(thread_id) if draft["head"])
    restored_payload = restored["snap"]["editor_payload"]
    assert restored_payload["text_elements"] == twelve_second_rows
    assert (
        sum(Decimal(str(slot["duration_s"])) for slot in restored_payload["timeline_slots"]) == 20
    )
    accepted = await _submit(user_id, thread_id, "Fill the text the whole video")
    assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))["status"] == "completed"
    assert_current(20, 20, "fill", 4)


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", [0, 1])
async def test_preservation_question_choices_execute_without_another_provider(
    brief_project, choice
):
    project, providers, _ = brief_project
    user_id, thread_id, _, _, rows, *_ = project
    before = _drafts(thread_id)
    asked = await _submit(user_id, thread_id, "Don't touch text, make video 12")
    assert (await asyncio.to_thread(run_kria_turn.run, asked.turn_id))["status"] == "completed"
    assert _drafts(thread_id) == before
    with sync_session() as db:
        question = db.scalars(
            select(CreationThreadEvent).where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.event_type == "assistant_response",
            )
        ).one()
        option = question.payload["options"][choice]
    selected = await _submit(user_id, thread_id, option)
    assert (await asyncio.to_thread(run_kria_turn.run, selected.turn_id))["status"] == "completed"
    head = next(d for d in _drafts(thread_id) if d["head"])
    after = head["snap"]["editor_payload"]["text_elements"]
    if choice == 0:
        assert after == rows
    else:
        assert after[-1]["end_s"] == 12
        assert [r["text"] for r in after] == [r["text"] for r in rows]
        assert all(a["end_s"] <= b["start_s"] for a, b in zip(after, after[1:]))
    assert len(_drafts(thread_id)) == len(before) + 1
    for provider in providers.values():
        provider.assert_not_called()
    _assert_job_unmodified(project)
