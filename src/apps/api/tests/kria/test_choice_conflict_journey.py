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


def _project(
    monkeypatch: pytest.MonkeyPatch,
    *,
    emit_after: int = 15,
    order: bool = False,
    clips: int = CLIPS,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """``emit_after``: the length the (mock) Creator model emits on every turn after the
    first; a model that obeys an option's label emits 24, one that ignores it re-emits 15.
    ``order``: the creator asked for filming order over clips with no capture times."""
    user_id, thread_id, session_id = _seed_runtime_project()
    with sync_session() as db:
        session = db.get(CreatorAgentSession, session_id)
        item = db.get(PlanItem, session.plan_item_id)
        item.clip_gcs_paths = [f"clips/{i:02d}.mp4" for i in range(clips)]
        item.clip_assignments = [
            {
                "media_id": f"clip-{i:02d}",
                "gcs_path": f"clips/{i:02d}.mp4",
                "storage_generation": "g1",
                "duration_s": 3.0,
            }
            for i in range(clips)
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
                    **(
                        {}
                        if order
                        else {
                            "target_duration_s": 15 if calls["n"] == 1 else emit_after,
                            "target_duration_requested": True,
                        }
                    ),
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
                    (
                        BriefUpdate(
                            kind="order",
                            scope="global",
                            description="in the order I filmed them",
                            facts={"key": "capture_time"},
                        )
                        if order
                        else BriefUpdate(
                            kind="timing",
                            scope="global",
                            description="15 seconds",
                            facts={"duration_s": 15},
                        )
                    ),
                )
                if calls["n"] == 1
                else ()
            ),
            brief_route="replan",
            brief_clip_ids=tuple(f"clip-{i:02d}" for i in range(clips)),
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


def _receipts(thread_id) -> dict[str, dict]:  # noqa: ANN001
    """Requirement receipts of the newest assistant event that carries them."""
    for event in reversed(_events(thread_id)):
        rows = (event.payload or {}).get("requirement_receipts")
        if event.role == "assistant" and rows:
            return {row["requirement_id"]: row for row in rows}
    return {}


@pytest.mark.asyncio
@pytest.mark.parametrize("emit_after", [24, 18, 15])
async def test_the_answered_length_wins_whatever_the_model_emits_next(
    monkeypatch, emit_after: int
) -> None:
    """P1-B: iOS sends the label as the message, so an obedient model emits 24 while the
    brief still says 15; a stubborn one re-emits 15; another invents 18. All three mint
    the draft with the ANSWERED length and green receipts (no "Partly: 15 seconds")."""
    user_id, thread_id, _ = _project(monkeypatch, emit_after=emit_after)
    await _say(user_id, thread_id, PROMPT)
    (question,) = _questions(thread_id)
    selection = {"question_id": question["question_id"], "option_key": "extend"}
    result = await _say(user_id, thread_id, "Extend it to 24 seconds", selection=selection)
    assert result["status"] == "awaiting_approval"
    strategy = _draft_strategy(_head_draft(thread_id))
    assert strategy["target_duration_s"] == 24
    assert strategy["choice_answers"][0]["option"] == "extend"
    receipt = _receipts(thread_id)["r1"]
    assert receipt["status"] == "met", receipt
    assert len(_questions(thread_id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("emit_after", [24, 15])
async def test_the_answered_clip_subset_wins_whatever_the_model_emits_next(
    monkeypatch, emit_after: int
) -> None:
    user_id, thread_id, _ = _project(monkeypatch, emit_after=emit_after)
    await _say(user_id, thread_id, PROMPT)
    (question,) = _questions(thread_id)
    selection = {"question_id": question["question_id"], "option_key": "fewer"}
    result = await _say(user_id, thread_id, "Keep 15 seconds with 18 clips", selection=selection)
    assert result["status"] == "awaiting_approval"
    strategy = _draft_strategy(_head_draft(thread_id))
    assert strategy["target_duration_s"] == 15 and len(strategy["selected_media_ids"]) == 18
    assert _receipts(thread_id)["r1"]["status"] == "met"


@pytest.mark.asyncio
async def test_unanswerable_replies_get_one_re_ask_then_the_plan_goes_through_unchanged(
    monkeypatch,
) -> None:
    """P2-2: no default is applied and no requirement is rewritten; repeating the request
    is not an answer."""
    user_id, thread_id, _ = _project(monkeypatch)
    await _say(user_id, thread_id, PROMPT)
    await _say(user_id, thread_id, "make it more fun")  # not an answer: re-ask ONCE
    assert len(_questions(thread_id)) == 2
    assert _head_draft(thread_id) is None
    done = await _say(user_id, thread_id, PROMPT)  # the request again is NOT an answer
    assert done["status"] == "awaiting_approval"
    assert len(_questions(thread_id)) == 2  # never a third
    strategy = _draft_strategy(_head_draft(thread_id))
    assert "choice_answers" not in strategy and strategy["target_duration_s"] == 15
    draft = _head_draft(thread_id)
    assert "choice_answers" not in draft.snapshot_json["brief_binding"]
    assert draft.snapshot_json["brief_binding"]["brief"]["requirements"][0]["facts"] == {
        "duration_s": 15
    }


@pytest.mark.asyncio
async def test_surprise_me_delegates_to_the_recommended_option_and_is_disclosed(
    monkeypatch,
) -> None:
    user_id, thread_id, _ = _project(monkeypatch)
    await _say(user_id, thread_id, PROMPT)
    result = await _say(user_id, thread_id, "Surprise me")
    assert result["status"] == "awaiting_approval"
    (answer,) = _draft_strategy(_head_draft(thread_id))["choice_answers"]
    assert answer["source"] == "creator_delegated" and answer["option"] == "extend"
    assert len(_questions(thread_id)) == 1


@pytest.mark.asyncio
async def test_an_unanswered_order_question_ends_in_one_plain_message_a_typed_reply_answers(
    monkeypatch,
) -> None:
    """P2-2/P2-3: after the single re-ask the order is not guessed. ONE plain message
    names what cannot be checked and quotes the two ways forward; typing one of them
    still answers the (exhausted) question."""
    user_id, thread_id, _ = _project(monkeypatch, order=True)
    await _say(user_id, thread_id, "Put them in the order I filmed them")
    assert [q["kind"] for q in _questions(thread_id)] == ["order_basis"]
    await _say(user_id, thread_id, "hmm, what do you mean?")  # re-ask once
    assert len(_questions(thread_id)) == 2
    await _say(user_id, thread_id, "Put them in the order I filmed them")  # not an answer
    assert len(_questions(thread_id)) == 2  # no third question ...
    assert _head_draft(thread_id) is None  # ... and no draft
    last = [e for e in _events(thread_id) if e.role == "assistant"][-1]
    text = last.content or ""
    assert "Use the order you added the clips" in text
    assert "Continue without a fixed order" in text
    assert "draft is unchanged" not in text  # there is no draft to be unchanged
    result = await _say(user_id, thread_id, "Use the order you added the clips")
    assert result["status"] == "awaiting_approval"
    (answer,) = _draft_strategy(_head_draft(thread_id))["choice_answers"]
    assert answer["option"] == "attachment_order"


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


def _append_assistant(thread_id, event_type: str, **payload) -> None:  # noqa: ANN001, ANN003
    from app.tasks.kria_runtime import _append_sync_event

    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        _append_sync_event(
            db, thread, role="assistant", event_type=event_type, content="x", payload=payload
        )
        db.commit()


ASYNC_EVENTS = (
    ("generation_ready", {"job_id": "j"}),
    ("assistant_review", {}),
    ("memory_updated", {}),
    ("status_update", {}),
    ("format_prompt", {"kind": "select_format"}),
    ("draft_applied", {}),
    ("assistant_render_failed", {}),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["tap", "typed_number"])
async def test_async_events_after_the_question_do_not_break_the_tap_or_the_typed_answer(
    monkeypatch, how: str
) -> None:
    """P2-4: an earlier render finishing / a memory write / a status line after the card
    used to make the tap fail with 'no longer open' and a typed '2' stop converting."""
    user_id, thread_id, _ = _project(monkeypatch)
    await _say(user_id, thread_id, PROMPT)
    (question,) = _questions(thread_id)
    for event_type, payload in ASYNC_EVENTS:
        _append_assistant(thread_id, event_type, **payload)
    if how == "tap":
        selection = {"question_id": question["question_id"], "option_key": "fewer"}
        result = await _say(
            user_id, thread_id, "Keep 15 seconds with 18 clips", selection=selection
        )
    else:
        result = await _say(user_id, thread_id, "2")  # the second listed option: fewer
    assert result["status"] == "awaiting_approval"
    assert len(_questions(thread_id)) == 1  # the async events did not burn an ask
    (answer,) = _draft_strategy(_head_draft(thread_id))["choice_answers"]
    assert answer["option"] == "fewer"


@pytest.mark.asyncio
async def test_a_later_text_question_stops_a_typed_number_from_answering_the_card(
    monkeypatch,
) -> None:
    from app.tasks.kria_runtime import _append_sync_event

    user_id, thread_id, _ = _project(monkeypatch)
    await _say(user_id, thread_id, PROMPT)
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        _append_sync_event(
            db, thread, role="user", event_type="user_message", content="hmm", payload={}
        )
        _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_response",
            content="How many clips do you want?",
            payload={"turn_id": str(uuid.uuid4()), "turn_value": "question"},
        )
        db.commit()
    await _say(user_id, thread_id, "2")  # an answer to the text question, not to the card
    stored = [
        e.payload["choice_selection"]
        for e in _events(thread_id)
        if e.role == "user" and e.payload and e.payload.get("choice_selection")
    ]
    assert stored == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restatement", ["no I want exactly 15 seconds with all of them", "make it shorter"]
)
async def test_a_later_restatement_reopens_the_question_and_is_never_silently_24_8(
    monkeypatch, restatement: str
) -> None:
    user_id, thread_id, _ = _project(monkeypatch, clips=31)
    await _say(user_id, thread_id, "Keep 15 seconds and use all of my 31 clips")
    (question,) = _questions(thread_id)
    selection = {"question_id": question["question_id"], "option_key": "extend"}
    first = await _say(user_id, thread_id, "Extend it to 24.8 seconds", selection=selection)
    assert first["status"] == "awaiting_approval"
    assert _draft_strategy(_head_draft(thread_id))["target_duration_s"] == 24.8
    await _decide(user_id, thread_id, "deny")
    revision = _head_draft(thread_id).draft_revision
    await _say(user_id, thread_id, restatement)  # the model emits 15 again
    assert len(_questions(thread_id)) == 2  # it asks once more ...
    assert _head_draft(thread_id).draft_revision == revision  # ... and drafts nothing at 24.8
    done = await _say(user_id, thread_id, restatement + "!")  # the second ask is spent
    assert done["status"] == "awaiting_approval"
    strategy = _draft_strategy(_head_draft(thread_id))
    assert strategy["target_duration_s"] == 15 and "choice_answers" not in strategy
    assert len(_questions(thread_id)) == 2
