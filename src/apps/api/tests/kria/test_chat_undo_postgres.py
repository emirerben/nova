"""KRI-1384: chat "undo that" on the phone path, end to end on real PostgreSQL.

Failure modes, written before the code. Each is a test here, in the parity test
(`test_editor_capability_parity`) or in the iOS staging tests (`ChatDraftStagingTests`):

F1  A hand edit after the chat turn, in a lane or row the turn changed, would be overwritten by
    the undo. Refused: the editor state must still hold what the chat wrote there.
F2  A hand edit elsewhere (another row, another lane) must survive the undo.
F3  A Save after the chat turn makes its draft history; the head is the Save's revision, so
    nothing is offered.
F4  A second "undo" walks nowhere unexpected: an undo turn stores no restore record, so it is
    refused instead of chaining or toggling.
F5  "Undo" with no earlier chat turn offers nothing.
F6  A render in flight or finished since the chat turn moved the generation: the head is no
    longer current, nothing is offered.
F7  A re-plan put the item on another job: the head belongs to the old job, nothing is offered.
F8  repeat_last_edit is never offered (no `last_turn_summary` in the server's history state).
F9  A hand edit, Save or turn that lands between planning and commit: the commit re-derives the
    undo under the head lock and refuses; nothing is written.
F10 A lane the restore record cannot put back exactly (a level with no saved value, a key the
    commit would drop): the turn is not undoable, never a partial undo.
F11 An app build below protocol 50 keeps a staged chat lane that a newer head returns to the
    saved value: nothing is offered to it (iOS fix and its tests in ChatDraftStagingTests).
F12 The phone drops only a staged lane the creator has not touched since (ChatDraftStagingTests).
F13 The phone re-encodes a staged lane; an untouched one must compare equal (ChatDraftStagingTests).
F14 The receipt overclaims: it names what the undo put back, from the restored lanes.

The only fakes are the copilot model (its JSON answers go through the real parser on the real
snapshot) and the render queue; planner, compiler, drafts and locks run for real.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.agents.edit_copilot import EditCopilotAgent
from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.kria.planner import PlannedKriaTurn, adapt_editor_action
from app.models import CreationThreadEvent, CreatorAgentExecution, Job
from app.services.client_protocol import bind_sending_build, unbind_sending_build
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_editor_state_turns_postgres import HOOK, _drafts, _seed_job, _submit
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

PHONE = 50
RED = {"op": "patch_text_style", "bar_index": 0, "patch": {"color": "#FF0000"}}
UNDO = {"op": "undo_last_edit"}
UNDONE = "I can't undo that from chat"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "kria_editor_state_turns_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(
        "app.tasks.kria_runtime.enqueue_editor_commit_render", lambda *_a, **_k: None
    )


class Copilot:
    """The model's answer per message; the real parser judges it on the real snapshot."""

    def __init__(self) -> None:
        self.answers: dict[str, list[dict[str, Any]]] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}

    def run(self, agent: EditCopilotAgent, input: Any, **_: Any) -> Any:  # noqa: ANN401
        self.snapshots[input.utterance] = input.variant_snapshot
        ops = self.answers[input.utterance]
        return agent.parse(
            json.dumps(
                {
                    "intent": "edit",
                    "confidence": 0.95,
                    "needs_clarification": False,
                    "reply": "Done.",
                    "ops": ops,
                }
            ),
            input,
        )


@pytest.fixture
def copilot(monkeypatch: pytest.MonkeyPatch) -> Copilot:
    model = Copilot()
    monkeypatch.setattr("app.kria.planner.default_client", lambda: object())
    monkeypatch.setattr("app.routes._copilot.default_client", lambda: object())
    monkeypatch.setattr(
        "app.kria.planner._call_main_creator",
        AsyncMock(side_effect=AssertionError("an edit turn never re-plans")),
    )
    monkeypatch.setattr(EditCopilotAgent, "run", lambda agent, input, **kw: model.run(agent, input))
    return model


@contextlib.contextmanager
def _build(protocol: int | None) -> Iterator[None]:
    token = bind_sending_build(protocol)
    try:
        yield
    finally:
        unbind_sending_build(token)


def _staged(head: dict[str, Any], *, sid: str, edit: dict[str, Any] | None = None) -> dict:
    """The state an iPhone editor sends after staging ``head`` (plus the creator's ``edit``)."""
    lanes = {"text_elements": head["snap"]["editor_payload"]["text_elements"]}
    if edit:
        lanes = {**lanes, **edit}
    return {
        "base_generation": "generation-1",
        "client_state_id": sid,
        "draft_revision": head["rev"],
        "lanes": lanes,
    }


async def _say(user_id, thread_id, message, state=None, *, protocol: int | None = PHONE):  # noqa: ANN001, ANN202
    with _build(protocol):
        accepted = await _submit(user_id, thread_id, message, state)
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "completed", result
    return accepted.turn_id


def _reply(thread_id) -> CreationThreadEvent:  # noqa: ANN001
    with sync_session() as db:
        return (
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.role == "assistant",
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )


def _head(thread_id) -> dict[str, Any]:  # noqa: ANN001
    return next(d for d in _drafts(thread_id) if d["head"])


def _record(draft_id) -> dict[str, Any] | None:  # noqa: ANN001
    from app.models import CreatorEditDraft

    with sync_session() as db:
        row = db.get(CreatorEditDraft, draft_id)
        execution = db.get(CreatorAgentExecution, row.source_execution_id)
        return (execution.result or {}).get("plan_review_before")


def _force(monkeypatch, ops) -> None:  # noqa: ANN001
    """Skip the planner: the commit must not trust what planning saw (F9)."""

    async def _planned(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=adapt_editor_action(reply="Done.", ops=ops),
            manifest_hash="a" * 64,
            context_hash="b" * 64,
        )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)


def _hook(draft: dict[str, Any]) -> dict[str, Any]:
    return next(r for r in draft["snap"]["editor_payload"]["text_elements"] if r["id"] == "hook")


@pytest.mark.asyncio
async def test_undo_puts_back_the_chat_turn_and_says_what_it_undid(copilot) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    copilot.answers = {"Make the hook red": [RED], "Undo that": [UNDO], "Undo again": [UNDO]}
    try:
        await _say(
            user_id,
            thread_id,
            "Make the hook red",
            {"base_generation": "generation-1", "client_state_id": "s0", "lanes": {}},
        )
        assert "history_state" not in copilot.snapshots["Make the hook red"], "F5"
        red = _head(thread_id)
        assert _hook(red)["color"] == "#FF0000"
        assert _record(red["id"])["chat_undo"]["changes"]

        await _say(user_id, thread_id, "Undo that", _staged(red, sid="s1"))
        offered = copilot.snapshots["Undo that"]
        assert offered["history_state"] == {"can_undo_last_turn": True}, "F8: no repeat offer"
        assert "history" in offered["allowed_op_families"]
        undone = _head(thread_id)
        assert undone["rev"] > red["rev"] and undone["gen"] == red["gen"]
        assert _hook(undone)["color"] == HOOK["color"]
        assert {k: v for k, v in _hook(undone).items() if k in HOOK} == HOOK
        receipt = _reply(thread_id)
        assert receipt.event_type == "draft_applied"
        assert receipt.content.startswith("I undid my last change"), receipt.content
        assert "white (#FFFFFF) on the title" in receipt.content, "F14: names what it put back"
        assert _record(undone["id"]) is None, "F4: an undo turn stores no record"

        drafts = len(_drafts(thread_id))
        await _say(user_id, thread_id, "Undo again", _staged(undone, sid="s2"))
        assert "history_state" not in copilot.snapshots["Undo again"], "F4"
        assert len(_drafts(thread_id)) == drafts
        assert not _reply(thread_id).content.startswith("I undid")
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_hand_edit_on_what_the_chat_changed_is_never_undone(copilot, monkeypatch) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    copilot.answers = {"Make the hook red": [RED], "Undo that": [UNDO]}
    try:
        await _say(user_id, thread_id, "Make the hook red")
        red = _head(thread_id)
        moved = [{**_hook(red), "x_frac": 0.2}]
        state = _staged(red, sid="s1", edit={"text_elements": moved})

        await _say(user_id, thread_id, "Undo that", state)
        assert "history_state" not in copilot.snapshots["Undo that"], "F1"
        assert _head(thread_id)["id"] == red["id"]

        # F9: an undo the planner offered on stale knowledge is refused under the commit locks.
        _force(monkeypatch, [UNDO])
        await _say(
            user_id, thread_id, "Undo it now", _staged(red, sid="s2", edit={"text_elements": moved})
        )
        assert _head(thread_id)["id"] == red["id"], "nothing was written"
        reply = _reply(thread_id)
        assert reply.event_type == "assistant_response" and reply.content.startswith(UNDONE)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_hand_edit_elsewhere_survives_the_undo(copilot) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    job_id = _seed_job(user_id, session_id)
    caption = {**HOOK, "id": "pier", "text": "Pier", "start_s": 2.0, "end_s": 4.0}
    with sync_session() as db:
        job = db.get(Job, job_id, with_for_update=True)
        plan = json.loads(json.dumps(job.assembly_plan))
        plan["variants"][0]["text_elements"].append(caption)
        job.assembly_plan = plan
        db.commit()
    copilot.answers = {"Make the hook red": [RED], "Undo that": [UNDO]}
    try:
        await _say(user_id, thread_id, "Make the hook red")
        red = _head(thread_id)
        rows = red["snap"]["editor_payload"]["text_elements"]
        renamed = [r if r["id"] == "hook" else {**r, "text": "Pier at dusk"} for r in rows]
        await _say(
            user_id, thread_id, "Undo that", _staged(red, sid="s1", edit={"text_elements": renamed})
        )
        assert copilot.snapshots["Undo that"]["history_state"]["can_undo_last_turn"] is True
        undone = _head(thread_id)
        texts = {r["id"]: r for r in undone["snap"]["editor_payload"]["text_elements"]}
        assert texts["hook"]["color"] == HOOK["color"]
        assert texts["pier"]["text"] == "Pier at dusk", "F2: the creator's edit stays"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["old_build", "web", "rendered_since", "replanned"])
async def test_undo_is_not_offered_when_the_phone_or_the_video_moved(copilot, case) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    job_id = _seed_job(user_id, session_id)
    copilot.answers = {"Make the hook red": [RED], "Undo that": [UNDO]}
    try:
        await _say(user_id, thread_id, "Make the hook red")
        red = _head(thread_id)
        state = _staged(red, sid="s1")
        protocol: int | None = PHONE
        if case == "old_build":
            protocol = PHONE - 1  # F11
        elif case == "web":
            protocol = None
        elif case == "rendered_since":  # F3 / F6: a Save or render moved the generation
            with sync_session() as db:
                job = db.get(Job, job_id, with_for_update=True)
                plan = json.loads(json.dumps(job.assembly_plan))
                plan["variants"][0]["render_generation_id"] = "generation-2"
                job.assembly_plan = plan
                db.commit()
            state = {**state, "base_generation": "generation-2", "lanes": {}}
        else:  # F7: a re-plan points the item at a new job
            _seed_job(user_id, session_id)
            state = {**state, "lanes": {}}
        await _say(user_id, thread_id, "Undo that", state, protocol=protocol)
        assert "history_state" not in copilot.snapshots.get("Undo that", {}), case
        assert all(
            d["id"] == red["id"] or d["rev"] < red["rev"] for d in _drafts(thread_id) if d["head"]
        )
    finally:
        await async_engine.dispose()
