"""Q3's original request through real runtime, stored brief and approval.

Provider answers are authored from the recorded parsed brief: production provider
I/O was redacted. No planner, checker, readiness, approval or compiler is mocked.
No live model, native export or production claim.
"""

from __future__ import annotations

import asyncio
import copy

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.api_schemas import ApprovalDecisionBody
from app.kria.brief_binding import BriefBinding
from app.kria.runtime import approval_fingerprint, decide_approval
from app.models import CreationThread, CreatorAgentApproval, PlanItem
from app.services.phone_montage_preflight import preflight_montage_plan
from app.tasks.kria_runtime import _claim_approval_dispatch
from tests.kria.test_kri945_global_text_journey import Chat, _row, _wire
from tests.kria.test_kri1295_whole_montage import BRIEF, MEDIA, REQUEST, STRATEGY
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project


@pytest.mark.asyncio
async def test_original_q3_request_drafts_and_approves_without_waiving_the_whole_clip(
    prod_profile, monkeypatch
):
    for name in (
        "kria_brief_binding_enabled",
        "kria_approval_readiness_enabled",
        "kria_choice_questions_enabled",
        "kria_phone_montage_preflight_enabled",
        "keep_whole_take_plan_enabled",
    ):
        monkeypatch.setattr(settings, name, True)
    user_id, thread_id, _ = _seed_runtime_project()
    row = _row(
        {
            "uuid": "q3-whole",
            "duration_s": 11.0,
            "subject": "Laptop",
            "summary": "A silent shot of a woman opening her laptop",
            "setting": "room",
            "activity": "working",
        },
        0,
        speaking=False,
    )
    row["analysis"]["best_moments"] = copy.deepcopy(
        MEDIA["clip_assignments"][0]["analysis"]["best_moments"]
    )
    row["analysis"]["understanding"]["speech"] = {"has_speech": False}
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.clip_gcs_paths = [row["gcs_path"]]
        item.clip_assignments = [row]
        thread.state = {
            "edit_format": "montage",
            "reply_language": "en",
            "media_count": 1,
            "media": [{"media_id": row["media_id"]}],
        }
        db.commit()
    answers = {
        "main_creator": {
            "action": {
                "kind": "propose_strategy",
                "strategy": STRATEGY,
                "summary": "The requested middle text over your whole silent clip.",
            },
            "brief_updates": [
                req.model_dump(include={"kind", "scope", "description", "literal", "facts"})
                for req in BRIEF.requirements
            ],
            "delegates_all_choices": False,
            "starts_fresh": False,
        },
        "clip_intent_planner": [],
    }
    chat = Chat(user_id, thread_id, _wire(monkeypatch))
    draft = await chat.send(REQUEST, answers)
    assert draft.event_type == "draft_applied", draft.content
    assert not (draft.payload or {}).get("choice_question")
    assert "Talking edit" not in draft.content
    assert chat.readiness()["ready"] is True
    with sync_session() as db:
        approval = db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.thread_id == thread_id,
                CreatorAgentApproval.status == "pending",
            )
        ).scalar_one()
        approval_id = approval.id
        body = ApprovalDecisionBody(
            expected_thread_revision=db.get(CreationThread, thread_id).revision,
            expected_draft_revision=approval.draft_revision,
            expected_approval_fingerprint=approval_fingerprint(approval),
        )
    async with AsyncSessionLocal() as db:
        await decide_approval(
            db,
            thread_id=thread_id,
            approval_id=approval_id,
            creator_id=user_id,
            decision="approve",
            body=body,
        )
    claim = await asyncio.to_thread(_claim_approval_dispatch, approval_id)
    assert claim is not None
    binding = BriefBinding.model_validate(claim.brief_binding)
    keep = next(req for req in binding.brief.live() if req.facts.get("keep_whole_take") is True)
    assert keep.kind == "style" and keep.status == "open"
    assert "Keep the whole clip" in claim.creator_request
    planned = preflight_montage_plan(
        creator_id=user_id,
        edit_format="montage",
        brief=binding.brief,
        strategy=claim.strategy,
        media_snapshot=binding.media_snapshot,
        visuals=[],
        client_protocol=44,
    )
    assert isinstance(planned, tuple), planned
    assert [(c.source_start_s, c.source_end_s) for c in planned[0].snapshot.fast_cuts] == [
        (0.0, 11.0)
    ]
