"""Real planner/parser/runtime journey; model replies and paid ledger are offline replays."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.agents._runtime import ModelInvocation, RunContext
from app.agents.edit_copilot import EditCopilotAgent
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.drafts import undo_draft
from app.models import CreationThread, CreationThreadEvent, CreatorEditDraft, Job
from app.services import bounded_review
from app.tasks import kria_runtime
from tests.kria.test_editor_state_turns_postgres import _drafts, _submit
from tests.kria.test_sequence_natural_intent_postgres import (
    brief_project,  # noqa: F401
    sequence_project,  # noqa: F401
)


@pytest.mark.asyncio
async def test_real_planner_copy_repair_stages_exact_words_and_undo(
    brief_project,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    project, models, _ = brief_project
    user_id, thread_id, session_id, job_id, rows, _, enqueue = project
    original_rows = copy.deepcopy(rows)
    with sync_session() as db:
        job = db.get(Job, job_id)
        assembly = copy.deepcopy(job.assembly_plan)
        for row in assembly["variants"][0]["text_elements"]:
            row.update(start_s=0, end_s=10.8)
        assembly["variants"][0]["guided_edit_revision"]["text_elements"] = copy.deepcopy(
            assembly["variants"][0]["text_elements"]
        )
        job.assembly_plan = assembly
        db.commit()
        original_assembly = copy.deepcopy(assembly)
    before = _drafts(thread_id)

    def malformed_copy_reply(agent, input, **kwargs):  # noqa: A002
        models.calls.append(("copilot", input.utterance))
        ids = [
            row["id"]
            for row in input.variant_snapshot["text_bars"]
            if row.get("sequence_source_id") == "opening" and not row.get("removed")
        ]
        operations = [
            {"op": "set_total_duration", "target_s": 12},
            {
                "op": "retime_text_sequence",
                "selector": {"ids": ids},
                "start_s": 0,
                "end_s": 12,
                "fit_to_video": True,
                "gap_policy": "scale",
            },
            {
                "op": "rewrite_text",
                "selector": {"ids": [ids[0]]},
                "text": "Unrequested rewritten word",
            },
        ]
        parsed = agent.parse(
            json.dumps(
                {
                    "intent": "edit",
                    "confidence": 0.99,
                    "needs_clarification": False,
                    "reply": "Prepared timing edit.",
                    "ops": operations,
                    "claimed_ops": [op["op"] for op in operations],
                }
            ),
            input,
        )
        assert len(parsed.ops) == 3, parsed.rejection_reasons
        return parsed

    monkeypatch.setattr(EditCopilotAgent, "run", malformed_copy_reply)
    for name in (
        "creator_repair_reviewer_enabled",
        "credit_quality_review_enabled",
        "credit_quality_review_billing_verified",
        "ai_cost_control_enabled",
    ):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    ledger = {}
    for name in (
        "reserve_paid_call",
        "mark_paid_call_started",
        "settle_paid_call",
        "mark_paid_call_unknown",
        "release_paid_call",
    ):
        ledger[name] = Mock(return_value=0.0 if name == "settle_paid_call" else object())
        monkeypatch.setattr(bounded_review.costs, name, ledger[name])

    def select_candidate(**kwargs):
        with sync_session() as db:
            assert db.scalar(select(Job).where(Job.id == job_id).with_for_update(nowait=True))
            assert db.scalar(
                select(CreationThread)
                .where(CreationThread.id == thread_id)
                .with_for_update(nowait=True)
            )
            assert db.scalar(
                select(CreatorEditDraft)
                .where(CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True))
                .with_for_update(nowait=True)
            )
        context = json.loads(kwargs["prompt"].split("SNAPSHOT_DATA:\n", 1)[1])
        assert context["original_request"] == message
        assert context["editor_timing_only"] is True
        assert "Unrequested rewritten word" in context["planned_operations_json"]
        assert "Unrequested rewritten word" not in context["candidate_operations_json"]
        assert any(e["source"] == "compiler" for e in context["evidence"])
        return ModelInvocation(
            raw_text=json.dumps(
                {
                    "status": "accept_candidate",
                    "candidate_id": context["candidate_id"],
                    "evidence_ids": [context["evidence"][0]["evidence_id"]],
                    "reason": "Keep the requested timing and restore the exact existing words.",
                }
            ),
            tokens_in=0,
            tokens_out=0,
            model_used="gpt-6.1-sol",
            provider_request_id="offline-runtime-repair",
        )

    client = Mock()
    client.invoke.side_effect = select_candidate
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: client)
    plan_with_live_agent = kria_runtime._plan_with_live_agent

    async def authorized_offline_plan(*args, **kwargs):
        planned = await plan_with_live_agent(*args, **kwargs)
        return replace(
            planned,
            editor_repair_run_context=RunContext(
                job_id=str(job_id),
                creator_agent_session_id=str(session_id),
                request_id=str(kwargs["turn_id"]),
                test_run_id="offline-runtime-repair",
                usage_purpose="live_eval",
                estimated_max_cost_usd=1,
                reservation_approved=True,
            ),
        )

    monkeypatch.setattr(kria_runtime, "_plan_with_live_agent", authorized_offline_plan)
    message = "Make the video 12 seconds and fill the text across the whole video"
    accepted = await _submit(user_id, thread_id, message)
    result = await asyncio.to_thread(kria_runtime.run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "completed"
    models.assert_parsed(message)
    client.invoke.assert_called_once()
    ledger["reserve_paid_call"].assert_called_once()
    drafts = _drafts(thread_id)
    assert len(drafts) == len(before) + 1
    head = next(draft for draft in drafts if draft["head"])
    assert head["parent"] == next(draft["id"] for draft in before if draft["head"])
    retimed = head["snap"]["editor_payload"]["text_elements"]
    assert [row["text"] for row in retimed] == [row["text"] for row in original_rows]
    assert retimed[0]["start_s"] == 0 and retimed[-1]["end_s"] == 12
    assert all(a["end_s"] <= b["start_s"] for a, b in zip(retimed, retimed[1:]))
    for old, new in zip(original_rows, retimed, strict=True):
        assert {k: v for k, v in old.items() if k not in {"start_s", "end_s", "source_params"}} == {
            k: v for k, v in new.items() if k not in {"start_s", "end_s", "source_params"}
        }
    with sync_session() as db:
        assert db.get(Job, job_id).assembly_plan == original_assembly
        reply = db.scalars(
            select(CreationThreadEvent)
            .where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.event_type == "draft_applied",
            )
            .order_by(CreationThreadEvent.sequence.desc())
        ).first()
        assert "wording" in reply.content.lower()
        receipt = reply.payload["editor_repair"]
        assert receipt["status"] == "draft_applied"
        assert receipt["basis_digest"] and receipt["authority_digest"]
        assert receipt["evidence_ids"] == ["timing-only-compiler"]
        assert receipt["provenance"]["provider_request_id"] == "offline-runtime-repair"
        assert receipt["provenance"]["context_digest"]
        assert receipt["provenance"]["cost_usd"] == 0
        assert reply.payload["can_undo"] is True
        (tmp_path / "runtime-repaired-draft.json").write_text(json.dumps(head["snap"], indent=2))
        (tmp_path / "runtime-before-variant.json").write_text(
            json.dumps(original_assembly["variants"][0], indent=2)
        )
        (tmp_path / "runtime-repair-receipt.json").write_text(json.dumps(receipt, indent=2))
    enqueue.assert_not_called()
    assert (await asyncio.to_thread(kria_runtime.run_kria_turn.run, accepted.turn_id))[
        "status"
    ] == "ignored"
    client.invoke.assert_called_once()
    assert len(_drafts(thread_id)) == len(drafts)
    async with AsyncSessionLocal() as db:
        await undo_draft(db, thread_id=thread_id, creator_id=user_id, expected_revision=head["rev"])
    restored = next(draft for draft in _drafts(thread_id) if draft["head"])
    assert restored["snap"] == next(draft["snap"] for draft in before if draft["head"])
