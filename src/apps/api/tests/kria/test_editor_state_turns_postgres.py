"""Real-Postgres coverage for chat turns built on the editor's unsaved state."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import SubmitTurnBody
from app.kria.planner import PlannedKriaTurn, adapt_editor_action
from app.kria.runtime import submit_turn
from app.models import (
    CreationThreadEvent,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    PlanItem,
)
from app.services.kria_editor_ops import resolve_editor_base
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

HOOK = {
    "id": "hook",
    "text": "Old matcha hook",
    "start_s": 0.0,
    "end_s": 2.0,
    "role": "generative_intro",
    "font_family": "Playfair Display",
    "size_px": 72,
    "color": "#FFFFFF",
    "effect": "static",
    "alignment": "center",
    "position": "middle",
}


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "kria_editor_state_turns_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(
        "app.tasks.kria_runtime.enqueue_editor_commit_render", lambda *_a, **_k: None
    )


def _seed_job(user_id, session_id) -> uuid.UUID:  # noqa: ANN001
    job_id = uuid.uuid4()
    with sync_session() as db:
        session = db.get(CreatorAgentSession, session_id, with_for_update=True)
        item = db.get(PlanItem, session.plan_item_id, with_for_update=True)
        db.add(
            Job(
                id=job_id,
                user_id=user_id,
                status="variants_ready",
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=item.id,
                content_plan_ownership_epoch=0,
                all_candidates={"clip_paths": ["users/test/matcha.mp4"]},
                assembly_plan={
                    "variants": [
                        {
                            "variant_id": "original_text",
                            "resolved_archetype": "montage",
                            "render_status": "ready",
                            "render_generation_id": "generation-1",
                            "render_finished_at": "2026-09-07T08:00:00Z",
                            "video_path": "generative-jobs/test/output.mp4",
                            "base_video_path": "generative-jobs/test/base.mp4",
                            "text_elements": [dict(HOOK)],
                        }
                    ]
                },
            )
        )
        db.flush()
        item.current_job_id = job_id
        session.target_job_id = job_id
        session.target_variant_id = "original_text"
        session.target_generation_id = "generation-1"
        session.manifest_hash = "a" * 64
        db.commit()
    return job_id


def _state(text: str = "Creator hook", base: str = "generation-1", sid: str = "state-1") -> dict:
    return {
        "base_generation": base,
        "client_state_id": sid,
        "lanes": {"text_elements": [{**HOOK, "text": text, "x_frac": 0.7, "y_frac": 0.2}]},
    }


async def _submit(user_id, thread_id, message, state=None, *, event_id=None, revision=None):  # noqa: ANN001, ANN202
    if revision is None:
        with sync_session() as db:
            from app.models import CreationThread

            revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=event_id or f"state-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
                **({"editor_state": state} if state is not None else {}),
            ),
        )
    return accepted


def _plan_with(monkeypatch, ops, *, request_render=False):  # noqa: ANN001, ANN202
    async def _planned(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=adapt_editor_action(reply="Done.", ops=ops, request_render=request_render),
            manifest_hash="a" * 64,
            context_hash="b" * 64,
        )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)


def _turn(turn_id):  # noqa: ANN001, ANN202
    with sync_session() as db:
        row = db.get(CreatorAgentTurn, uuid.UUID(turn_id))
        db.refresh(row)
        return row.editor_state, row.status


def _drafts(thread_id):  # noqa: ANN001, ANN202
    with sync_session() as db:
        rows = (
            db.execute(
                select(CreatorEditDraft)
                .where(CreatorEditDraft.thread_id == thread_id)
                .order_by(CreatorEditDraft.draft_revision)
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": r.id,
                "rev": r.draft_revision,
                "parent": r.parent_draft_id,
                "head": r.is_head,
                "snap": r.snapshot_json,
                "gen": r.base_generation_id,
            }
            for r in rows
        ]


@pytest.mark.asyncio
async def test_state_is_stored_first_wins_on_replay_hidden_from_events_and_cleared(
    monkeypatch,
) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    _plan_with(
        monkeypatch, [{"op": "patch_text_style", "bar_index": 0, "patch": {"color": "#FF0000"}}]
    )
    try:
        accepted = await _submit(
            user_id, thread_id, "Make it red", _state("First"), event_id="e1", revision=2
        )
        stored, _ = _turn(accepted.turn_id)
        assert stored["lanes"]["text_elements"][0]["text"] == "First"
        # A lost response replays with a FRESHER snapshot: no 409, first state wins.
        replay = await _submit(
            user_id,
            thread_id,
            "Make it red",
            _state("Second", sid="state-2"),
            event_id="e1",
            revision=2,
        )
        assert replay.replayed is True and replay.turn_id == accepted.turn_id
        assert _turn(accepted.turn_id)[0]["lanes"]["text_elements"][0]["text"] == "First"
        # The blob never rides the thread delta.
        with sync_session() as db:
            events = (
                db.execute(
                    select(CreationThreadEvent).where(CreationThreadEvent.thread_id == thread_id)
                )
                .scalars()
                .all()
            )
            assert "First" not in str([e.payload for e in events])
        assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))[
            "status"
        ] == "completed"
        assert _turn(accepted.turn_id) == (None, "completed")
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_flag_off_drops_state_and_stores_nothing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kria_editor_state_turns_enabled", False)
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    _plan_with(
        monkeypatch, [{"op": "patch_text_style", "bar_index": 0, "patch": {"color": "#FF0000"}}]
    )
    try:
        accepted = await _submit(user_id, thread_id, "Make it red", _state(), revision=2)
        assert _turn(accepted.turn_id)[0] is None
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        (draft,) = _drafts(thread_id)
        # Byte-identical to a state-less turn: no provenance anywhere.
        assert "client_state_id" not in draft["snap"]
        assert draft["snap"]["editor_payload"]["text_elements"][0]["text"] == "Old matcha hook"
        with sync_session() as db:
            applied = db.execute(
                select(CreationThreadEvent).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "draft_applied",
                )
            ).scalar_one()
            assert "based_on_client_state_id" not in applied.payload
            assert "editor_state_source" not in applied.payload
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_state_turn_chain_your_edits_then_chat_child_ignoring_the_head(monkeypatch) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    spied: list[str] = []
    from app.tasks import kria_runtime

    real = kria_runtime.resolve_editor_base

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        result = real(*args, **kwargs)
        spied.append(result.source)
        return result

    monkeypatch.setattr(kria_runtime, "resolve_editor_base", spy)
    try:
        # Turn 1 (no state): a fresh chat head built on generation-1.
        _plan_with(monkeypatch, [{"op": "edit_text", "bar_index": 0, "text": "Chat hook"}])
        first = await _submit(user_id, thread_id, "Retitle", revision=2)
        await asyncio.to_thread(run_kria_turn.run, first.turn_id)
        # Turn 2 (state): the creator edited the hook in the editor, unsaved.
        _plan_with(
            monkeypatch, [{"op": "patch_text_style", "bar_index": 0, "patch": {"color": "#FF0000"}}]
        )
        second = await _submit(user_id, thread_id, "Make it red", _state("Creator hook"))
        assert (await asyncio.to_thread(run_kria_turn.run, second.turn_id))["status"] == "completed"
        assert spied == ["variant", "client_state"]
        drafts = _drafts(thread_id)
        assert [d["rev"] for d in drafts] == [0, 1, 2]
        chat1, yours, chat2 = drafts
        assert yours["parent"] == chat1["id"] and chat2["parent"] == yours["id"]
        assert [d["head"] for d in drafts] == [False, False, True]
        assert yours["snap"]["intent"] == "Your edits"
        assert yours["snap"]["client_state_id"] == chat2["snap"]["client_state_id"] == "state-1"
        assert yours["gen"] == chat2["gen"] == "generation-1"
        bar = chat2["snap"]["editor_payload"]["text_elements"][0]
        # Built on the CLIENT's text + position, not the chat head ("Chat hook").
        assert (bar["text"], bar["x_frac"], bar["color"]) == ("Creator hook", 0.7, "#FF0000")
        with sync_session() as db:
            event = (
                db.execute(
                    select(CreationThreadEvent)
                    .where(
                        CreationThreadEvent.thread_id == thread_id,
                        CreationThreadEvent.event_type == "draft_applied",
                    )
                    .order_by(CreationThreadEvent.sequence.desc())
                )
                .scalars()
                .first()
            )
            assert event.payload["based_on_client_state_id"] == "state-1"
            assert event.payload["editor_state_source"] == "client_state"
            assert event.payload["can_undo"] is True
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_state_that_went_stale_during_the_model_call_is_refused(monkeypatch) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    _plan_with(
        monkeypatch, [{"op": "patch_text_style", "bar_index": 0, "patch": {"color": "#FF0000"}}]
    )
    try:
        accepted = await _submit(
            user_id, thread_id, "Make it red", _state(base="generation-0"), revision=2
        )
        assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))[
            "status"
        ] == "completed"
        assert _drafts(thread_id) == []
        with sync_session() as db:
            reply = (
                db.execute(
                    select(CreationThreadEvent).where(
                        CreationThreadEvent.thread_id == thread_id,
                        CreationThreadEvent.event_type == "assistant_response",
                    )
                )
                .scalars()
                .all()[-1]
            )
            assert reply.content == "Your video changed — reopen the editor and try again."
        assert _turn(accepted.turn_id) == (None, "completed")
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_speech_cut_with_unsaved_edits_is_an_honest_reply_not_a_crash(monkeypatch) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    _plan_with(
        monkeypatch,
        [{"op": "apply_speech_cut_candidate", "candidate_id": "reviewed-cut"}],
        request_render=True,
    )
    try:
        accepted = await _submit(user_id, thread_id, "Cut the silences", _state(), revision=2)
        assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))[
            "status"
        ] == "completed"
        assert _drafts(thread_id) == []
        with sync_session() as db:
            reply = (
                db.execute(
                    select(CreationThreadEvent).where(
                        CreationThreadEvent.thread_id == thread_id,
                        CreationThreadEvent.event_type == "assistant_response",
                    )
                )
                .scalars()
                .all()[-1]
            )
            assert reply.content == "Save your edits first, then I can cut the silences."
    finally:
        await async_engine.dispose()


def test_planner_and_compile_share_the_one_resolver() -> None:
    """Both callers import the resolver by name: patching it must move BOTH."""
    from app.kria import planner
    from app.tasks import kria_runtime

    assert planner.resolve_editor_base is resolve_editor_base
    assert kria_runtime.resolve_editor_base is resolve_editor_base
