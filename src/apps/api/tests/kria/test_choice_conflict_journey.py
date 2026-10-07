"""KRI-476 (PR-C) journey on real Postgres: question -> answer -> approved binding -> contract.

Only the Main Creator model call is replaced (``planner._plan_live_turn``); the media
snapshot fence, the clarification gate, the free-text ingestion in ``submit_turn``, the
turn completion backstop, the approval and the pinned ``BriefBinding`` are all real, and the
answer is read back from the stored thread events exactly as a retried turn would.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import planner
from app.kria.api_schemas import ApprovalDecisionBody, SubmitTurnBody
from app.kria.brief import BriefUpdate
from app.kria.brief_binding import BriefBinding
from app.kria.planner import PlannedKriaTurn
from app.kria.runtime import approval_fingerprint, decide_approval, submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentSession,
    CreatorEditDraft,
    PlanItem,
)
from app.services.creator_render_contract import build_render_contract
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

CLIPS = 30
PROMPT = "Keep 15 seconds and use all of my clips"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


def _project(monkeypatch: pytest.MonkeyPatch) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id, thread_id, session_id = _seed_runtime_project()
    with sync_session() as db:
        session = db.get(CreatorAgentSession, session_id)
        item = db.get(PlanItem, session.plan_item_id)
        item.clip_gcs_paths = [f"clips/{i:02d}.mp4" for i in range(CLIPS)]
        item.clip_assignments = [
            {
                "media_id": f"clip-{i:02d}",
                "gcs_path": f"clips/{i:02d}.mp4",
                "storage_generation": "g1",
                "duration_s": 3.0,
            }
            for i in range(CLIPS)
        ]
        db.commit()
    calls = {"n": 0}

    async def _model(*_args, **_kwargs) -> PlannedKriaTurn:  # noqa: ANN002, ANN003
        # What the Main Creator returns for this ask on EVERY turn (a re-sent prompt
        # re-derives the same strategy); the brief gets its requirement only once.
        calls["n"] += 1
        plan = planner.adapt_creator_action(
            ProposeStrategy(
                kind="propose_strategy",
                strategy=CreativeStrategy(
                    direction="guided_story",
                    edit_format="montage",
                    audio_strategy="licensed_music",
                    pacing="fast",
                    render_program="guided",
                    target_duration_s=15,
                    target_duration_requested=True,
                    rationale="Every clip, fast.",
                ),
                summary="A fast montage of all your clips.",
            )
        )
        return PlannedKriaTurn(
            plan=plan,
            manifest_hash="a" * 64,
            context_hash="b" * 64,
            brief_updates=(
                (
                    BriefUpdate(
                        kind="timing",
                        scope="global",
                        description="15 seconds",
                        facts={"duration_s": 15},
                    ),
                )  # fmt: skip
                if calls["n"] == 1
                else ()
            ),
            brief_route="replan",
            brief_clip_ids=tuple(f"clip-{i:02d}" for i in range(CLIPS)),
        )

    monkeypatch.setattr(planner, "_plan_live_turn", _model)
    return user_id, thread_id, session_id


async def _say(user_id, thread_id, message, *, selection=None) -> dict:  # noqa: ANN001
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"j-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
                **({"choice_selection": selection} if selection else {}),
            ),
        )
    return await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)


async def _decide(user_id, thread_id, decision: str):  # noqa: ANN001, ANN202
    with sync_session() as db:
        approval = (
            db.execute(
                select(CreatorAgentApproval)
                .where(CreatorAgentApproval.thread_id == thread_id)
                .order_by(CreatorAgentApproval.created_at.desc())
            )
            .scalars()
            .first()
        )
        thread = db.get(CreationThread, thread_id)
        token = approval_fingerprint(approval)
        approval_id, revision, draft_revision = (
            approval.id,
            thread.revision,
            approval.draft_revision,
        )
    async with AsyncSessionLocal() as db:
        result, _ = await decide_approval(
            db,
            thread_id=thread_id,
            approval_id=approval_id,
            creator_id=user_id,
            decision=decision,
            body=ApprovalDecisionBody(
                expected_thread_revision=revision,
                expected_draft_revision=draft_revision,
                expected_approval_fingerprint=token,
            ),
        )
    return result


def _events(thread_id) -> list[CreationThreadEvent]:  # noqa: ANN001
    with sync_session() as db:
        rows = (
            db.execute(
                select(CreationThreadEvent)
                .where(CreationThreadEvent.thread_id == thread_id)
                .order_by(CreationThreadEvent.sequence)
            )
            .scalars()
            .all()
        )
        for row in rows:
            db.expunge(row)
    return rows


def _questions(thread_id) -> list[dict]:  # noqa: ANN001
    return [
        e.payload["choice_question"]
        for e in _events(thread_id)
        if e.role == "assistant" and e.payload and e.payload.get("choice_question")
    ]


def _head_draft(thread_id) -> CreatorEditDraft | None:  # noqa: ANN001
    with sync_session() as db:
        draft = db.execute(
            select(CreatorEditDraft)
            .where(CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True))
            .order_by(CreatorEditDraft.draft_revision.desc())
        ).scalars().first()  # fmt: skip
        if draft is not None:
            db.expunge(draft)
        return draft


def _draft_strategy(draft: CreatorEditDraft) -> dict:
    return (draft.snapshot_json or {}).get("strategy") or {}


@pytest.mark.asyncio
async def test_question_then_free_text_answer_then_approved_binding_carries_the_choice(
    monkeypatch,
) -> None:
    user_id, thread_id, _ = _project(monkeypatch)

    # 1. The ask: ONE question, nothing drafted, nothing can render.
    await _say(user_id, thread_id, PROMPT)
    (question,) = _questions(thread_id)
    assert question["kind"] == "duration_vs_count"
    assert [o["key"] for o in question["options"]] == ["extend", "fewer"]
    assert _head_draft(thread_id) is None
    with sync_session() as db:
        assert (
            db.execute(
                select(CreatorAgentApproval).where(CreatorAgentApproval.thread_id == thread_id)
            )
            .scalars()
            .first()
            is None
        )

    # 2. A typed answer in the creator's words (no structured selection at all).
    result = await _say(user_id, thread_id, "Extend it!")
    assert result["status"] == "awaiting_approval"
    stored = [
        e.payload["choice_selection"]
        for e in _events(thread_id)
        if e.role == "user" and e.payload and e.payload.get("choice_selection")
    ]
    assert stored == [{"question_id": question["question_id"], "option_key": "extend"}]
    assert len(_questions(thread_id)) == 1  # no re-ask

    draft = _head_draft(thread_id)
    strategy = _draft_strategy(draft)
    assert strategy["target_duration_s"] == 24
    (answer,) = strategy["choice_answers"]
    assert answer["option"] == "extend" and answer["requirement_ids"] == ["r1"]
    binding = BriefBinding.model_validate(draft.snapshot_json["brief_binding"])
    assert binding.choice_answers == [answer]  # approval freezes it (part of the digest)
    assert binding.brief.requirements[0].facts["duration_s"] == 24  # the pinned requirement

    # 3. Approve exactly that draft; the contract rebuilt from the binding carries the answer.
    decision = await _decide(user_id, thread_id, "approve")
    assert decision.status == "approved"
    contract = build_render_contract(
        strategy,
        generation_id="journey",
        brief=binding.resolve(),
        media_snapshot=binding.media_snapshot,
    )
    assert contract.duration_s == 24 and not contract.unresolved


@pytest.mark.asyncio
async def test_a_resent_prompt_after_the_answer_is_not_asked_again(monkeypatch) -> None:
    user_id, thread_id, _ = _project(monkeypatch)
    await _say(user_id, thread_id, PROMPT)
    (question,) = _questions(thread_id)
    selection = {"question_id": question["question_id"], "option_key": "fewer"}
    first = await _say(user_id, thread_id, "Keep 15 seconds with 18 clips", selection=selection)
    assert first["status"] == "awaiting_approval"
    await _decide(user_id, thread_id, "deny")  # the creator does not approve it yet
    again = await _say(user_id, thread_id, PROMPT)  # the same prompt, a new turn
    assert again["status"] == "awaiting_approval"
    assert len(_questions(thread_id)) == 1
    strategy = _draft_strategy(_head_draft(thread_id))
    assert strategy["target_duration_s"] == 15 and len(strategy["selected_media_ids"]) == 18


@pytest.mark.asyncio
async def test_unanswerable_reply_gets_one_re_ask_then_a_disclosed_default(monkeypatch) -> None:
    user_id, thread_id, _ = _project(monkeypatch)
    await _say(user_id, thread_id, PROMPT)
    await _say(user_id, thread_id, "make it more fun")  # not an answer: re-ask ONCE
    assert len(_questions(thread_id)) == 2
    assert _head_draft(thread_id) is None
    done = await _say(user_id, thread_id, "I don't know, surprise me")
    assert done["status"] == "awaiting_approval"
    assert len(_questions(thread_id)) == 2  # never a third
    (answer,) = _draft_strategy(_head_draft(thread_id))["choice_answers"]
    assert answer["source"] == "default" and answer["option"] == "extend"


@pytest.mark.asyncio
async def test_completion_backstop_never_mints_a_draft_the_gate_missed(monkeypatch) -> None:
    user_id, thread_id, _ = _project(monkeypatch)

    async def _bypass(_db, planned, **_kwargs) -> PlannedKriaTurn:  # noqa: ANN001, ANN003
        return planned  # as if the planner gate had been skipped

    monkeypatch.setattr(planner, "_gate_unresolved_choices", _bypass)
    await _say(user_id, thread_id, PROMPT)
    (question,) = _questions(thread_id)
    assert question["kind"] == "duration_vs_count"  # the SAME question, from the backstop
    assert _head_draft(thread_id) is None
    with sync_session() as db:
        assert (
            db.execute(
                select(CreatorAgentApproval).where(CreatorAgentApproval.thread_id == thread_id)
            )
            .scalars()
            .first()
            is None
        )


@pytest.mark.asyncio
async def test_flag_off_is_byte_identical_to_before(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", False)
    user_id, thread_id, _ = _project(monkeypatch)
    result = await _say(user_id, thread_id, PROMPT)
    assert result["status"] == "awaiting_approval"  # drafted exactly as before the gate
    assert _questions(thread_id) == []
    draft = _head_draft(thread_id)
    strategy = _draft_strategy(draft)
    assert "choice_answers" not in strategy and strategy["target_duration_s"] == 15
    assert "choice_answers" not in draft.snapshot_json["brief_binding"]
