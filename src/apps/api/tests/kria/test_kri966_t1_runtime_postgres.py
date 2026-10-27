"""KRI-966 (runtime half): Don Julio walk-in T1 through the real runtime, prompt to approval.

Real runtime turns (``submit_turn`` + ``run_kria_turn`` over scratch Postgres). Only the Main
Creator's model call is replaced (``planner._call_main_creator``) by each turn's output: the
prompt's brief (the approval's v4 rows as turn 1 filed them, see the fixture's ``_source``),
prod's turn-2 rewrite of r1's start line to the tapped "what do you think?", and on the length
answer the model's echo of the tapped label as a new timing row (prod's r7). The draft strategy
is RECONSTRUCTED (prod recorded only its hash). Everything around it is the server's own: the
planner's handling of the model output, the question gate on the stored brief and the thread's
events, the creator's taps, the brief versions, the draft receipts, "Make this simpler version",
the approval card and the BriefBinding the approval pins.

Prod (``a2426885d``) asked "the available spoken take is 10.2 seconds", stored the echo as r7
(``duration_s: 10.2``), listed "Use the available 10.2 seconds" on the approval card and graded it
after the render as "you asked for 10.2s". Expected now: the card names the lines and the ~5 s
they keep, the echo never reaches the brief, and the approval pins the answered length on r1.
"""

from __future__ import annotations

import asyncio
import copy
import re
import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.agents._schemas.creator_agent import (
    CreativeStrategy,
    CreatorRenderIntentEvidence,
    ProposeStrategy,
)
from app.agents.main_creator import MainCreatorOutput
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import planner
from app.kria.api_schemas import ApprovalDecisionBody, ChoiceSelectionIn, SubmitTurnBody
from app.kria.brief import BriefUpdate, load_latest_brief_sync, parse_brief_updates
from app.kria.runtime import approval_fingerprint, decide_approval, submit_turn
from app.models import CreationThread, CreationThreadEvent, CreatorAgentApproval, PlanItem
from app.tasks.kria_runtime import _claim_approval_dispatch, run_kria_turn
from tests.kria.test_kri787_merope_t1_replay import prod_flags
from tests.kria.test_kri966_take_window_length import (
    EVENTS,
    FIXTURE,
    MEDIA_ID,
    ROW,
    brief,
    strategy,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("test Postgres not reachable", allow_module_level=True)

PROMPT = FIXTURE["prompt"]


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    # Prod offered no "Clean up speech" on this take (no draft line mentions it).
    monkeypatch.setattr("app.tasks.kria_runtime._speech_cleanup_offered", lambda *_a: False)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


def _seed_t1() -> tuple[uuid.UUID, uuid.UUID]:
    """A runtime project holding prod's take on Talking (subtitled), as the phone made it."""
    user_id, thread_id, _session_id = _seed_runtime_project()
    row = copy.deepcopy(ROW)
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "subtitled"
        item.idea = "Untitled video"
        item.clip_gcs_paths = [row["gcs_path"]]
        item.clip_assignments = [row]
        thread.state = {
            "media": [{"media_id": MEDIA_ID, "filename": "T1_01_footage"}],
            "media_count": 1,
            "edit_format": "subtitled",
        }
        db.commit()
    return user_id, thread_id


def turn1_updates() -> list[BriefUpdate]:
    """Turn 1's brief: the prompt's six rows (r1 quoting the whole opening line)."""
    return parse_brief_updates(
        [
            {
                "kind": req.kind,
                "scope": req.scope,
                "literal": req.literal,
                "description": req.description,
                "facts": req.facts,
            }
            for req in brief(1).requirements
        ]
    )


def turn2_rewrite() -> BriefUpdate:
    """Prod's turn 2: the model rewrote r1's start line to the line the creator tapped."""
    r1 = next(req for req in brief(2).requirements if req.id == "r1")
    return BriefUpdate(
        operation="change",
        target_requirement_id="r1",
        kind=r1.kind,
        scope=r1.scope,
        description=r1.description,
        facts=r1.facts,
    )


def echo_of(label: str) -> BriefUpdate:
    """Prod's turn 3: the tapped label filed as a new timing row (r7), with its seconds."""
    seconds = float(re.search(r"(\d+(?:\.\d+)?) seconds", label).group(1))  # test-only parse
    return BriefUpdate(
        operation="add",
        kind="timing",
        scope="global",
        description=label,
        facts={"duration_s": seconds},
    )


def _model_output(*, updates: tuple = ()) -> MainCreatorOutput:
    return MainCreatorOutput(
        action=ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy.model_validate(strategy(opening_title="Before the meat")),
            summary="",
            render_intent_evidence=CreatorRenderIntentEvidence(
                opening_title='Title: "Before the meat"'
            ),
        ),
        brief_updates=list(updates),
    )


def _revision(thread_id: uuid.UUID) -> int:
    with sync_session() as db:
        return int(db.get(CreationThread, thread_id).revision)


def _last_reply(thread_id: uuid.UUID) -> CreationThreadEvent:
    with sync_session() as db:
        return (
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type.in_(["draft_applied", "assistant_response"]),
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )


def _pending_approval(thread_id: uuid.UUID) -> CreatorAgentApproval | None:
    with sync_session() as db:
        return db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.thread_id == thread_id,
                CreatorAgentApproval.status == "pending",
            )
        ).scalar_one_or_none()


@dataclass
class Chat:
    user_id: uuid.UUID
    thread_id: uuid.UUID
    replies: list[CreationThreadEvent]

    @property
    def question(self) -> dict:
        return (self.replies[-1].payload or {}).get("choice_question") or {}


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    chat: Chat,
    message: str,
    model_output: MainCreatorOutput,
    *,
    choice: tuple[str, str] | None = None,
) -> None:
    """One creator turn: submit (a typed message or a tapped option) and run it."""

    async def _model(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return model_output

    monkeypatch.setattr(planner, "_call_main_creator", _model)
    selection = (
        {"choice_selection": ChoiceSelectionIn(question_id=choice[0], option_key=choice[1])}
        if choice
        else {}
    )
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=chat.thread_id,
            creator_id=chat.user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"kri966-{uuid.uuid4().hex}",
                expected_thread_revision=_revision(chat.thread_id),
                **selection,
            ),
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] in {"completed", "awaiting_approval"}, result
    chat.replies.append(_last_reply(chat.thread_id))


async def _tap(monkeypatch: pytest.MonkeyPatch, chat: Chat, key: str, output: MainCreatorOutput):
    question = chat.question
    option = next(o for o in question["options"] if o["key"] == key)
    await _turn(monkeypatch, chat, option["label"], output, choice=(question["question_id"], key))
    return option["label"]


async def _chat_to_the_length_answer(monkeypatch: pytest.MonkeyPatch) -> tuple[Chat, str]:
    """Prod's turns 1-3: the prompt, the start-line tap, the length tap (with the echo)."""
    user_id, thread_id = _seed_t1()
    chat = Chat(user_id, thread_id, [])
    await _turn(monkeypatch, chat, PROMPT, _model_output(updates=tuple(turn1_updates())))
    assert chat.question.get("kind") == "take_line_start", chat.replies[-1].content
    assert (
        chat.question["input_digest"] == EVENTS["6"]["payload"]["choice_question"]["input_digest"]
    )
    await _tap(monkeypatch, chat, "line_1", _model_output(updates=(turn2_rewrite(),)))
    assert chat.question.get("kind") == "length_vs_natural", chat.replies[-1].content
    card = chat.replies[-1].content or ""
    label = chat.question["options"][0]["label"]
    await _tap(monkeypatch, chat, "accept_natural", _model_output(updates=(echo_of(label),)))
    return chat, card


@pytest.mark.asyncio
async def test_the_length_card_names_the_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    chat, card = await _chat_to_the_length_answer(monkeypatch)
    assert "10.2" not in card and "spoken take" not in card, card
    assert "“what do you think?”" in card and "“Nice!”" in card and "about 5 seconds" in card


@pytest.mark.asyncio
async def test_the_tapped_label_never_becomes_the_creators_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat, _card = await _chat_to_the_length_answer(monkeypatch)
    with sync_session() as db:
        stored = load_latest_brief_sync(db, chat.thread_id)
    assert [req.id for req in stored.live()] == ["r1", "r2", "r3", "r4", "r5", "r6"]
    r1 = next(req for req in stored.live() if req.id == "r1")
    assert r1.facts["duration_s"] == 7  # the creator's own words stay on the stored brief
    receipts = (chat.replies[-1].payload or {}).get("requirement_receipts") or []
    assert "r7" not in {row["requirement_id"] for row in receipts}
    assert "Keep those 5 seconds" not in str(chat.replies[-1].content)

    # The held first draft (ATAKAN) -> "Make this simpler version" -> the approval card.
    if _pending_approval(chat.thread_id) is None:
        assert chat.question.get("conflict") == "simplification_requires_choice", chat.replies[
            -1
        ].content
        await _tap(monkeypatch, chat, "simpler_version", _model_output())
    approval = _pending_approval(chat.thread_id)
    assert approval is not None, chat.replies[-1].content
    shown = str(approval.consequence_summary)
    for echo in ("Keep those", "Use the available", "10.2", "you asked for"):
        assert echo not in shown, shown
    # r1 keeps the creator's words; its line receipt names the part the lines keep.
    assert f"{brief(2).requirements[0].description} (I'll keep your take from" in shown, shown
    assert "(0:15)" in shown and "(0:20)" in shown, shown

    with sync_session() as db:
        body = ApprovalDecisionBody(
            expected_thread_revision=db.get(CreationThread, chat.thread_id).revision,
            expected_draft_revision=approval.draft_revision,
            expected_approval_fingerprint=approval_fingerprint(approval),
        )
    async with AsyncSessionLocal() as db:
        await decide_approval(
            db,
            thread_id=chat.thread_id,
            approval_id=approval.id,
            creator_id=chat.user_id,
            decision="approve",
            body=body,
        )
    claim = await asyncio.to_thread(_claim_approval_dispatch, approval.id)
    assert claim is not None
    pinned = claim.brief_binding["brief"]["requirements"]
    live = {row["id"]: row for row in pinned if row["status"] == "open"}
    assert "r7" not in live
    assert live["r1"]["facts"]["duration_s"] == 5  # the answer, applied to the creator's own ask
    assert live["r1"]["description"] == brief(2).requirements[0].description
    answers = {a["kind"]: a["option"] for a in claim.strategy.get("choice_answers") or []}
    assert answers.get("length_vs_natural") == "accept_natural"
