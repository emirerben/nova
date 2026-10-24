"""KRI-901: "No text on her face or on the plate" asks for the plate's mark BEFORE approval.

The Merope T1 chat through the real runtime (``submit_turn`` + ``run_kria_turn`` over scratch
Postgres), with the Main Creator's model call replaced by the turn's recorded output, as in
``test_kri787_merope_t1_runtime_postgres``. Turn 1's brief carries the KRI-827 fact the
current extractor writes for that sentence (``keep_text_off`` = her face + "plate") on the
title and MEROPE asks.

Failure modes (written before the code), each pinned below:

1. The approval card goes out with the plate unmarked, so the render can only answer
   ``no_selection`` after the fact (the ticket).
2. An old app build (or the web) is asked a question it has no card for and is stuck.
3. The mark answer is read as a new instruction: the brief changes across the mark turn.
4. The mark answer is an editor follow-up or a duplicate send: no re-plan, approval never
   offered after the mark.
5. The approval binding pins no mark, or a client-stamped generation.
6. The creator says the text can go anywhere (the fact is dropped): the gate keeps asking.
7. A re-uploaded clip keeps an old mark that points at other bytes.
8. A face-only ask asks for a mark (faces are sampled automatically).
9. Approval readiness on: a marked object stays ``evidence_missing`` and is never ready.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import planner
from app.kria.api_schemas import ChoiceSelectionIn, SubmitTurnBody
from app.kria.brief import BriefUpdate, load_latest_brief_sync
from app.kria.runtime import submit_turn
from app.models import CreationThread, CreatorEditDraft, PlanItem
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_kri787_merope_t1_replay import FIXTURE, RENDERS, turn1_brief_updates
from tests.kria.test_kri787_merope_t1_runtime_postgres import (
    ANSWERS,
    Chat,
    _approve,
    _flags,  # noqa: F401 - the Merope prod flags, autouse
    _last_reply,
    _model_output,
    _pending_approval,
    _revision,
    _seed_t1,
)

TAKE = FIXTURE["take_row"]["media_id"]
KEEP_OFF = {"keep_text_off": [{"kind": "face"}, {"kind": "object", "label": "plate"}]}
# The app's mark: a box the creator drew around the plate, converted from the editor's
# (x, y, w, h) to the server's [left, top, right, bottom] the way the app converts it.
APP_RECT = (0.3, 0.55, 0.4, 0.2)
APP_BOX = [0.3, 0.55, 0.7, 0.75]


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    clear_request_context()
    await async_engine.dispose()


def _with_keep_off(updates: list[BriefUpdate], facts: dict = KEEP_OFF) -> list[BriefUpdate]:
    """Turn 1's recorded updates with the extractor's ``keep_text_off`` on the title and
    MEROPE asks ("placed so there is no text on her face or on the plate")."""
    out = []
    for update in updates:
        if "no text on her face or on the plate" in str(update.description):
            update = update.model_copy(update={"facts": {**update.facts, **facts}})
        out.append(update)
    return out


async def _send(
    monkeypatch: pytest.MonkeyPatch,
    chat: Chat,
    message: str,
    *,
    protocol: int | None,
    updates: tuple = (),
    choice: tuple[str, str] | None = None,
    mark: dict | None = None,
    before_run=None,  # noqa: ANN001
) -> dict:
    """One creator turn from an app build at ``protocol``; returns the reply's payload."""
    summary = RENDERS[0]["draft_summary"] if choice and choice[1] == "simpler_version" else ""
    output = _model_output(summary=summary, updates=updates)

    async def _model(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return output

    monkeypatch.setattr(planner, "_call_main_creator", _model)
    if protocol is not None:
        set_client_protocol(protocol)
    try:
        async with AsyncSessionLocal() as db:
            accepted, _ = await submit_turn(
                db,
                thread_id=chat.thread_id,
                creator_id=chat.user_id,
                body=SubmitTurnBody(
                    message=message,
                    client_event_id=f"kri901-{uuid.uuid4().hex}",
                    expected_thread_revision=_revision(chat.thread_id),
                    **(
                        {
                            "choice_selection": ChoiceSelectionIn(
                                question_id=choice[0], option_key=choice[1]
                            )
                        }
                        if choice
                        else {}
                    ),
                    **({"protected_region": mark} if mark else {}),
                ),
            )
    finally:
        clear_request_context()
    if before_run is not None:
        before_run(chat.thread_id)
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] in {"completed", "awaiting_approval"}, result
    reply = _last_reply(chat.thread_id)
    chat.replies.append(reply)
    return dict(reply.payload or {})


def _brief_state(thread_id: uuid.UUID) -> tuple[int, list[tuple[str, dict]]]:
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)
    return brief.version, [(req.id, dict(req.facts)) for req in brief.live()]


def _draft_count(thread_id: uuid.UUID) -> int:
    with sync_session() as db:
        item_id = db.get(CreationThread, thread_id).active_plan_item_id
        return len(
            db.execute(select(CreatorEditDraft.id).where(CreatorEditDraft.item_id == item_id)).all()
        )


async def _chat(
    monkeypatch: pytest.MonkeyPatch,
    *,
    protocol: int | None,
    facts: dict = KEEP_OFF,
    on_mark_question=None,  # noqa: ANN001
    max_turns: int = 6,
    extra_updates: tuple = (),
) -> tuple[Chat, list[dict]]:
    """Merope T1 until approval: each question answered the way the creator would. Every
    mark question is handed to ``on_mark_question(question, thread_id)`` (which returns the turn
    kwargs) or, by
    default, answered with the app's mark on the question's first clip."""
    user_id, thread_id = _seed_t1(cleanup_chosen=False)
    chat = Chat(user_id, thread_id, [])
    asked: list[dict] = []
    payload = await _send(
        monkeypatch,
        chat,
        FIXTURE["creator_messages"][0],
        protocol=protocol,
        updates=(*_with_keep_off(turn1_brief_updates(), facts), *extra_updates),
    )
    for _ in range(max_turns):
        if _pending_approval(thread_id) is not None:
            break
        question = payload.get("protected_region_question")
        if question is not None:
            asked.append(question)
            kwargs = (on_mark_question or (lambda q, _t: _app_mark(q)))(question, thread_id)
            payload = await _send(monkeypatch, chat, protocol=protocol, **kwargs)
            continue
        choice = payload.get("choice_question")
        assert choice is not None, chat.replies[-1].content
        option, message = ANSWERS[choice.get("kind") or choice["conflict"]]
        payload = await _send(
            monkeypatch, chat, message, protocol=protocol, choice=(choice["question_id"], option)
        )
    return chat, asked


def _app_mark(question: dict) -> dict:
    return {
        "message": f"Marked the {question['label']}",
        "mark": {
            "label": question["label"],
            "media_id": question["media_ids"][0],
            "source_t_s": 20.0,
            "box": APP_BOX,
        },
    }


# == 1, 3, 4, 5: asked before approval, approved with the creator's mark =====================


@pytest.mark.asyncio
async def test_the_plate_is_asked_for_before_approval_and_pinned_at_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []

    def answer(question: dict, thread_id: uuid.UUID) -> dict:
        seen.append(
            {
                "approval": _pending_approval(thread_id),
                "brief": _brief_state(thread_id),
                "drafts": _draft_count(thread_id),
            }
        )
        return _app_mark(question)

    chat, asked = await _chat(monkeypatch, protocol=17, on_mark_question=answer)

    # Asked once, before any approval card, about the plate, on the take.
    assert [q["label"] for q in asked] == ["plate"]
    assert asked[0]["media_ids"] == [TAKE]
    assert set(asked[0]["requirement_ids"]) == {"r6", "r7"}
    assert seen[0]["approval"] is None, "approval offered with the plate unmarked"
    # Asking kept no speculative draft as the head.
    assert seen[0]["drafts"] == 0
    ask_reply = next(r for r in chat.replies if (r.payload or {}).get("protected_region_question"))
    assert "Mark the plate on one frame" in str(ask_reply.content)

    # The mark turn changed no requirement (the extractor read a neutral stand-in).
    assert _brief_state(chat.thread_id) == seen[0]["brief"]

    # After the mark the approval card goes out, and approving pins the server-stamped mark.
    claim = await _approve(chat)
    assert claim.brief_binding["protected_regions"] == [
        {
            "label": "plate",
            "media_id": TAKE,
            "source_t_s": 20.0,
            "box": APP_BOX,
            "generation": FIXTURE["take_row"]["storage_generation"],
        }
    ]


# == 2: an old build / the web is never asked ===============================================


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [16, None], ids=["protocol-16-app", "web"])
async def test_a_client_without_the_card_is_never_asked(
    monkeypatch: pytest.MonkeyPatch, protocol: int | None
) -> None:
    chat, asked = await _chat(monkeypatch, protocol=protocol)
    assert asked == []
    assert chat.approval is not None
    assert all("protected_region_question" not in (r.payload or {}) for r in chat.replies)


@pytest.mark.asyncio
async def test_the_kill_switch_stops_the_question(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "protected_region_mark_min_client_protocol", 999)
    chat, asked = await _chat(monkeypatch, protocol=17)
    assert asked == [] and chat.approval is not None


# == 6: "the text can go anywhere" drops the ask ============================================


@pytest.mark.asyncio
async def test_saying_the_text_can_go_anywhere_stops_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def retract(question: dict, _thread_id: uuid.UUID) -> dict:
        return {
            "message": "The text can go on the plate, that's fine",
            "updates": tuple(
                BriefUpdate(
                    operation="change",
                    target_requirement_id=rid,
                    facts={"keep_text_off": [{"kind": "face"}]},
                )
                for rid in question["requirement_ids"]
            ),
        }

    chat, asked = await _chat(monkeypatch, protocol=17, on_mark_question=retract)
    assert len(asked) == 1
    claim = await _approve(chat)
    assert not claim.brief_binding.get("protected_regions")


# == 7: a re-uploaded clip asks again ========================================================


def _reupload(thread_id: uuid.UUID) -> None:
    """The creator replaces the take after marking it: the same clip id, new bytes."""
    with sync_session() as db:
        item = db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id)
        item.clip_assignments = [
            {**dict(row), "storage_generation": "2"} for row in item.clip_assignments
        ]
        db.commit()


@pytest.mark.asyncio
async def test_a_mark_on_a_replaced_clip_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(question: dict, _thread_id: uuid.UUID) -> dict:
        # The first mark is stamped with generation "1", then the take is replaced before the
        # turn runs; the second mark is on the new bytes.
        return {**_app_mark(question), **({"before_run": _reupload} if not answered else {})}

    answered: list[bool] = []

    def answer_once(question: dict, thread_id: uuid.UUID) -> dict:
        out = answer(question, thread_id)
        answered.append(True)
        return out

    chat, asked = await _chat(monkeypatch, protocol=17, on_mark_question=answer_once)
    assert [q["label"] for q in asked] == ["plate", "plate"]
    claim = await _approve(chat)
    assert [m["generation"] for m in claim.brief_binding["protected_regions"]] == ["2"]


# == 8: a face is never asked for ===========================================================


@pytest.mark.asyncio
async def test_a_face_only_ask_asks_for_no_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    chat, asked = await _chat(monkeypatch, protocol=17, facts={"keep_text_off": [{"kind": "face"}]})
    assert asked == [] and chat.approval is not None


# == 9: approval readiness on ==============================================================


def _readiness_rows(chat: Chat) -> list[dict]:
    """Every readiness assessment the chat's replies recorded, newest last."""
    out = []
    for reply in chat.replies:
        coverage = (reply.payload or {}).get("brief_coverage") or {}
        if coverage.get("approval_readiness"):
            out.append(coverage["approval_readiness"])
    with sync_session() as db:
        item_id = db.get(CreationThread, chat.thread_id).active_plan_item_id
        for (snapshot,) in db.execute(
            select(CreatorEditDraft.snapshot_json)
            .where(CreatorEditDraft.item_id == item_id)
            .order_by(CreatorEditDraft.created_at)
        ).all():
            report = ((snapshot or {}).get("brief_coverage") or {}).get("approval_readiness")
            if report:
                out.append(report)
    return out


@pytest.mark.asyncio
async def test_with_readiness_on_the_marked_plate_is_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness on, with the ask as its own requirement (no other checker): before KRI-901 it
    was ``evidence_missing`` forever. Asked for the mark first; once marked, the phone Talking
    render owns it and approval is offered."""
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    own = BriefUpdate(
        kind="text",
        scope="global",
        description="No text on her face or on the plate",
        facts=KEEP_OFF,
    )
    chat, asked = await _chat(monkeypatch, protocol=17, extra_updates=(own,))
    assert [q["label"] for q in asked] == ["plate"]
    assert "Mark the plate" in str(chat.replies[0].content)
    assert chat.approval is not None
    [plate_id] = [rid for rid in asked[0]["requirement_ids"] if rid not in {"r6", "r7"}]
    approved = _readiness_rows(chat)[-1]
    assert approved["ready"] is True
    row = next(r for r in approved["requirements"] if r["requirement_id"] == plate_id)
    assert (row["disposition"], row["evidence_owner"]) == ("executable", "protected_text_render")
    # No report after the mark still asks for it.
    for report in _readiness_rows(chat):
        assert all("Mark the plate" not in str(r["reason"]) for r in report["requirements"])
