"""KRI-724 (runtime half): the Merope T1 chat asks which "Merope", and approval freezes the pick.

Real runtime turns (``submit_turn`` + ``run_kria_turn`` over scratch Postgres), the KRI-787 T1
harness: only the Main Creator's model call is replaced by the turn's recorded output. With
``SPEECH_OCCURRENCE_QUESTION_ENABLED`` on, a turn emits a ``choice_question`` of kind
``speech_occurrence`` (the card iOS and web already render); the creator taps Caro's "Merope"
(the second); approval binds a ``BriefBinding`` whose ``choice_answers`` hold that pick; and the
dispatched job carries it into the worker. Flag off: the question never appears.

Failure modes pinned here:
1. The question is never asked on a real turn (capability not wired into the planner gate).
2. The tapped answer is not what approval freezes (the binding loses it, or the value).
3. The pick is lost between approval and the worker (prepared snapshot / job binding).
4. Flag off still asks.
"""

from __future__ import annotations

import pytest

from app.config import settings
from tests.kria.test_kri787_merope_t1_replay import dispatch
from tests.kria.test_kri787_merope_t1_runtime_postgres import (  # noqa: F401 - fixtures
    ANSWERS,
    MESSAGES,
    RENDERS,
    Chat,
    _approve,
    _dispose_async_engine,
    _flags,
    _model_output,
    _pending_approval,
    _seed_t1,
    _turn,
    turn1_brief_updates,
)

KIND = "speech_occurrence"


async def _chat(monkeypatch: pytest.MonkeyPatch, *, pick: str = "occ_2_") -> tuple[Chat, list]:
    """Prod's prompt, then every question answered (Caro's "Merope" for ours), to approval."""
    user_id, thread_id = _seed_t1(cleanup_chosen=False)
    chat = Chat(user_id, thread_id, [])
    asked: list[dict] = []
    await _turn(monkeypatch, chat, MESSAGES[0], _model_output(updates=tuple(turn1_brief_updates())))
    for _ in range(6):
        question = (chat.replies[-1].payload or {}).get("choice_question")
        if _pending_approval(thread_id) is not None or question is None:
            break
        asked.append(question)
        if question.get("kind") == KIND:
            option = next(o["key"] for o in question["options"] if o["key"].startswith(pick))
            await _turn(
                monkeypatch,
                chat,
                "The second one",
                _model_output(),
                choice=(question["question_id"], option),
            )
            continue
        option, message = ANSWERS[question.get("kind") or question["conflict"]]
        summary = RENDERS[0]["draft_summary"] if option == "simpler_version" else ""
        await _turn(
            monkeypatch,
            chat,
            message,
            _model_output(summary=summary),
            choice=(question["question_id"], option),
        )
    return chat, asked


@pytest.mark.asyncio
async def test_a_real_turn_asks_which_merope_and_approval_freezes_carols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "speech_occurrence_question_enabled", True)
    chat, asked = await _chat(monkeypatch)

    # 1: the existing choice card, of the new kind, about r7.
    [ours] = [q for q in asked if q.get("kind") == KIND]
    assert ours["conflict"] == f"{KIND}:r7"
    keys = [o["key"] for o in ours["options"]]
    assert [k.split("_t")[0] for k in keys[:4]] == [f"occ_{i}_of_4" for i in range(1, 5)]
    assert keys[-1] == "waive"

    # 2: approval freezes the creator's pick.
    claim = await _approve(chat)
    answers = [a for a in claim.brief_binding.get("choice_answers") or [] if a["kind"] == KIND]
    assert len(answers) == 1
    assert answers[0]["option"].startswith("occ_2_of_4_")
    assert answers[0]["value"] == "Merope" and answers[0]["requirement_ids"] == ["r7"]

    # 3: the dispatched job's binding is that frozen one.
    job = dispatch(monkeypatch, RENDERS[0], strategy=claim.strategy, binding=claim.brief_binding)
    bound = job.assembly_plan["creator_brief_binding"]
    assert [a for a in bound["choice_answers"] if a["kind"] == KIND] == answers


@pytest.mark.asyncio
async def test_flag_off_never_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "speech_occurrence_question_enabled", False)
    _chat_result, asked = await _chat(monkeypatch)
    assert all(q.get("kind") != KIND for q in asked)
