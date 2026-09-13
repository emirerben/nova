"""Legacy editor execution admission and transaction fences."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.routes import creation_threads as routes
from app.services import creation_editor_actions as actions


@pytest.mark.parametrize(
    "message",
    [
        "Remove the visual overlays I added",
        "Delete those uploaded images",
        "Get rid of the video layers",
    ],
)
def test_visual_removal_admission(message):
    assert actions.is_visual_removal_request(message)


@pytest.mark.parametrize(
    "message",
    [
        "Make a new edit",
        "Remove the captions",
        "Change the music",
        "Add an image",
        "Delete the video",
    ],
)
def test_existing_planner_requests_are_not_rerouted(message):
    assert not actions.is_visual_removal_request(message)


def test_selection_never_guesses_between_variants():
    job = SimpleNamespace(assembly_plan={"variants": [{"variant_id": "a"}, {"variant_id": "b"}]})
    assert actions.selected_variant(SimpleNamespace(state={}), job) is None
    assert (
        actions.selected_variant(SimpleNamespace(state={"selected_variant_id": "b"}), job)[
            "variant_id"
        ]
        == "b"
    )
    assert (
        actions.selected_variant(SimpleNamespace(state={"selected_variant_id": "missing"}), job)
        is None
    )


@pytest.fixture
def context(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4())
    variant = {
        "variant_id": "a",
        "render_generation_id": "g1",
        "visual_blocks": [{"id": "v", "kind": "media"}],
    }
    job = SimpleNamespace(
        id=uuid.uuid4(), status="variants_ready", assembly_plan={"variants": [variant]}
    )
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        revision=4,
        status="active",
        runtime_version=1,
        active_plan_item_id=uuid.uuid4(),
        state={"selected_variant_id": "a"},
    )
    body = SimpleNamespace(
        message="Remove the visual overlays I added",
        client_event_id="test-action",
        expected_revision=4,
    )
    db = SimpleNamespace(
        rollback=AsyncMock(),
        commit=AsyncMock(),
        get=AsyncMock(side_effect=[user, SimpleNamespace(current_job_id=job.id), job]),
        execute=AsyncMock(),
    )
    monkeypatch.setattr(
        routes, "_load_authorized_projection_rows", AsyncMock(return_value=(None, None, job, None))
    )
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(
        actions,
        "build_editor_snapshot",
        lambda *_: {"allowed_op_families": ["visual_media", "text"]},
    )
    response = SimpleNamespace(
        ops=[], outcome="unsupported", reply="These older overlay cards cannot be removed here."
    )
    monkeypatch.setattr(actions, "run_copilot_turn", AsyncMock(return_value=response))
    return db, thread, body, user, job, response


@pytest.mark.asyncio
async def test_unsupported_is_honest_and_never_executes(context):
    db, thread, body, user, _, _ = context
    await actions.execute_visual_removal(db, thread, body, user)
    db.rollback.assert_awaited_once()
    db.commit.assert_awaited_once()
    assert routes._append.await_args_list[-1].kwargs["event_type"] == "assistant_response"
    assert actions.run_copilot_turn.await_args.args[0].snapshot["allowed_op_families"] == [
        "visual_media"
    ]


@pytest.mark.asyncio
async def test_concurrent_revision_rejects_before_user_or_save(context, monkeypatch):
    db, thread, body, user, _, response = context

    async def changed(*args, **kwargs):
        thread.revision += 1
        return response

    monkeypatch.setattr(actions, "run_copilot_turn", changed)
    with pytest.raises(HTTPException) as error:
        await actions.execute_visual_removal(db, thread, body, user)
    assert error.value.status_code == 409
    routes._append.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_generation_rejects_before_user_or_save(context, monkeypatch):
    db, thread, body, user, job, response = context

    async def changed(*args, **kwargs):
        job.assembly_plan["variants"][0]["render_generation_id"] = "g2"
        return response

    monkeypatch.setattr(actions, "run_copilot_turn", changed)
    with pytest.raises(HTTPException) as error:
        await actions.execute_visual_removal(db, thread, body, user)
    assert error.value.detail == "baseline_conflict"
    routes._append.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_after_model_call_never_saves(context, monkeypatch):
    db, thread, body, user, _, _ = context
    monkeypatch.setattr(
        routes,
        "_duplicate",
        AsyncMock(return_value=SimpleNamespace(event_type="user_message", content=body.message)),
    )
    await actions.execute_visual_removal(db, thread, body, user)
    routes._append.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_ownership_failure_never_calls_model(context, monkeypatch):
    db, thread, body, user, _, _ = context
    monkeypatch.setattr(
        routes, "_load_authorized_projection_rows", AsyncMock(side_effect=HTTPException(404))
    )
    with pytest.raises(HTTPException):
        await actions.execute_visual_removal(db, thread, body, user)
    actions.run_copilot_turn.assert_not_awaited()
    routes._append.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_receipt_and_user_event_commit_before_enqueue(context, monkeypatch):
    db, thread, body, user, job, response = context
    response.ops = [{"op": "remove_visual_media", "target_ids": ["v"]}]
    response.outcome = "applied"
    payload = actions.EditorCommitRequest(base_generation="g1", visual_blocks=[])
    monkeypatch.setattr(actions, "compile_editor_ops", lambda *_: SimpleNamespace(payload=payload))
    db.execute.return_value = Mock()
    db.execute.return_value.scalars.return_value.all.return_value = []
    prep = {"generation": "g2", "sections": {"visual_blocks": True}}
    monkeypatch.setattr(actions, "prepare_editor_commit", Mock(return_value=prep))

    def enqueue(*args):
        db.commit.assert_awaited_once()
        assert len(routes._append.await_args_list) == 2
        assert routes._append.await_args_list[-1].kwargs["payload"]["outcome"] == "saved"

    monkeypatch.setattr(actions, "enqueue_editor_commit_render", enqueue)
    await actions.execute_visual_removal(db, thread, body, user)
    assert actions.prepare_editor_commit.call_args.args[2].base_generation == "g1"


@pytest.mark.asyncio
async def test_silently_ignored_section_cannot_claim_saved(context, monkeypatch):
    db, thread, body, user, job, response = context
    response.ops = [{"op": "remove_visual_media", "target_ids": ["v"]}]
    payload = actions.EditorCommitRequest(base_generation="g1", visual_blocks=[])
    monkeypatch.setattr(actions, "compile_editor_ops", lambda *_: SimpleNamespace(payload=payload))
    db.execute.return_value = Mock()
    db.execute.return_value.scalars.return_value.all.return_value = []
    monkeypatch.setattr(
        actions,
        "prepare_editor_commit",
        Mock(return_value={"generation": "g2", "sections": {"visual_blocks": False}}),
    )
    enqueue = Mock()
    monkeypatch.setattr(actions, "enqueue_editor_commit_render", enqueue)
    await actions.execute_visual_removal(db, thread, body, user)
    enqueue.assert_not_called()
    assert routes._append.await_args_list[-1].kwargs["payload"]["outcome"] == "unsupported"


@pytest.mark.asyncio
async def test_changed_selected_variant_rejects(context, monkeypatch):
    db, thread, body, user, _, response = context

    async def changed(*args, **kwargs):
        thread.state["selected_variant_id"] = "b"
        return response

    monkeypatch.setattr(actions, "run_copilot_turn", changed)
    with pytest.raises(HTTPException) as error:
        await actions.execute_visual_removal(db, thread, body, user)
    assert error.value.detail == "Editor target changed"
    routes._append.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_failure_reports_saved_and_terminalizes_own_generation(context, monkeypatch):
    db, thread, body, user, job, response = context
    response.ops = [{"op": "remove_visual_media", "target_ids": ["v"]}]
    payload = actions.EditorCommitRequest(base_generation="g1", visual_blocks=[])
    monkeypatch.setattr(actions, "compile_editor_ops", lambda *_: SimpleNamespace(payload=payload))
    db.execute.return_value = Mock()
    db.execute.return_value.scalars.return_value.all.return_value = []
    db.get.side_effect = [user, SimpleNamespace(current_job_id=job.id), job, job]

    def prepare(*args, **kwargs):
        job.assembly_plan["variants"][0].update(
            render_generation_id="g2", render_status="rendering", video_path="last-good.mp4"
        )
        return {"generation": "g2", "sections": {"visual_blocks": True}}

    monkeypatch.setattr(actions, "prepare_editor_commit", prepare)
    monkeypatch.setattr(
        actions, "enqueue_editor_commit_render", Mock(side_effect=RuntimeError("offline"))
    )
    await actions.execute_visual_removal(db, thread, body, user)
    assert db.commit.await_count == 2
    assert job.assembly_plan["variants"][0]["error_class"] == "render_enqueue_failed"
    assert job.status == "variants_ready_partial"
    assert (
        routes._append.await_args_list[-1].kwargs["payload"]["outcome"] == "render_enqueue_failed"
    )


@pytest.mark.asyncio
async def test_cancelled_job_cannot_be_edited(context):
    db, thread, body, user, job, _ = context
    job.status = "cancelled"
    with pytest.raises(HTTPException) as error:
        await actions.execute_visual_removal(db, thread, body, user)
    assert error.value.status_code == 409
    routes._append.assert_not_awaited()


@pytest.mark.asyncio
async def test_guided_stale_sources_never_stage_save(context, monkeypatch):
    from app.pipeline import guided_story
    from app.routes import plan_items

    db, thread, body, user, job, response = context
    job.assembly_plan["variants"][0]["resolved_archetype"] = "guided_story"
    response.ops = [{"op": "remove_visual_media", "target_ids": ["v"]}]
    payload = actions.EditorCommitRequest(
        base_generation="g1", visual_blocks=[], guided_revision_number=1
    )
    monkeypatch.setattr(actions, "compile_editor_ops", lambda *_: SimpleNamespace(payload=payload))
    db.execute.return_value = Mock()
    db.execute.return_value.scalars.return_value.all.return_value = []
    monkeypatch.setattr(guided_story, "validate_guided_snapshot", lambda _: (1, "digest", {}))
    monkeypatch.setattr(plan_items, "_proposal_media_is_current", AsyncMock(return_value=False))
    stage = Mock()
    monkeypatch.setattr(actions, "prepare_editor_commit", stage)
    await actions.execute_visual_removal(db, thread, body, user)
    stage.assert_not_called()
    assert routes._append.await_args_list[-1].kwargs["payload"]["outcome"] == "unsupported"


@pytest.mark.asyncio
async def test_long_valid_thread_message_gets_visible_response_without_model(context):
    db, thread, body, user, _, _ = context
    body.message = "Remove the visual overlays " + "x" * 2500
    await actions.execute_visual_removal(db, thread, body, user)
    actions.run_copilot_turn.assert_not_awaited()
    assert routes._append.await_args.kwargs["event_type"] == "assistant_response"
    assert "shorten" in routes._append.await_args.kwargs["content"]
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_full_length_client_event_id_fits_copilot_identity(context):
    db, thread, body, user, _, _ = context
    body.client_event_id = "a" * 160
    await actions.execute_visual_removal(db, thread, body, user)
    request = actions.run_copilot_turn.await_args.args[0]
    assert len(request.client_request_id) == 64
    assert routes._append.await_args_list[0].kwargs["client_event_id"] == body.client_event_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["no_effect", "clarification", "unsupported", "failed", "stale"]
)
async def test_no_ops_never_forwards_compound_success_claim(context, outcome):
    db, thread, body, user, _, response = context
    response.outcome = outcome
    response.reply = "Removed the uploaded visuals; captions are unchanged."
    await actions.execute_visual_removal(db, thread, body, user)
    reply = routes._append.await_args.kwargs["content"]
    assert "Nothing was changed" in reply
    assert "Removed the uploaded visuals" not in reply
    if outcome == "clarification":
        assert "Which uploaded image or video" in reply


@pytest.mark.asyncio
async def test_replaced_item_job_rejects_before_job_lock_or_save(context):
    db, thread, body, user, _, _ = context
    db.get.side_effect = [user, SimpleNamespace(current_job_id=uuid.uuid4())]
    with pytest.raises(HTTPException) as error:
        await actions.execute_visual_removal(db, thread, body, user)
    assert error.value.detail == "Editor target changed"
    assert db.get.await_args.args[0] is actions.PlanItem
    assert db.get.await_args.kwargs == {"populate_existing": True, "with_for_update": True}
    routes._append.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("generation,render_status", [("g3", "rendering"), ("g2", "ready")])
async def test_enqueue_exception_after_attempt_moved_does_not_report_failure(
    context, monkeypatch, generation, render_status
):
    db, thread, body, user, job, response = context
    response.ops = [{"op": "remove_visual_media", "target_ids": ["v"]}]
    payload = actions.EditorCommitRequest(base_generation="g1", visual_blocks=[])
    monkeypatch.setattr(actions, "compile_editor_ops", lambda *_: SimpleNamespace(payload=payload))
    db.execute.return_value = Mock()
    db.execute.return_value.scalars.return_value.all.return_value = []
    db.get.side_effect = [user, SimpleNamespace(current_job_id=job.id), job, job]
    monkeypatch.setattr(
        actions,
        "prepare_editor_commit",
        Mock(return_value={"generation": "g2", "sections": {"visual_blocks": True}}),
    )

    def enqueue(*args):
        job.assembly_plan["variants"][0].update(
            render_generation_id=generation, render_status=render_status
        )
        raise RuntimeError("queue acknowledgement lost")

    monkeypatch.setattr(actions, "enqueue_editor_commit_render", enqueue)
    await actions.execute_visual_removal(db, thread, body, user)
    assert len(routes._append.await_args_list) == 2
    assert routes._append.await_args.kwargs["payload"]["outcome"] == "saved"
    assert job.assembly_plan["variants"][0]["render_status"] == render_status
    assert "error_class" not in job.assembly_plan["variants"][0]
