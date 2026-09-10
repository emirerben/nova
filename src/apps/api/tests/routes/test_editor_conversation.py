"""The shared editor ledger cannot dispatch work or cross project ownership."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models import Job
from app.routes import creation_threads as routes
from app.routes.plan_items import _check_confirmed_editor_target


def editor_graph():
    user = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(id=uuid.uuid4(), user_id=user.id, ownership_epoch=0)
    item = SimpleNamespace(
        id=uuid.uuid4(),
        content_plan_id=plan.id,
        current_job_id=uuid.uuid4(),
        idea="A quiet morning",
        edit_format="montage",
        clip_gcs_paths=["clip"],
    )
    job = SimpleNamespace(
        id=item.current_job_id,
        user_id=user.id,
        content_plan_item_id=item.id,
        content_plan_ownership_epoch=0,
        assembly_plan={"variants": [{"variant_id": "original_text", "render_status": "ready"}]},
    )
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        active_plan_item_id=item.id,
        active_job_id=job.id,
        state={},
        status="active",
        runtime_version=2,
    )
    return user, plan, item, job, thread


@pytest.mark.asyncio
async def test_direct_link_reuses_the_owning_conversation_and_preserves_variant(monkeypatch):
    user, plan, item, job, thread = editor_graph()
    db = SimpleNamespace(
        get=AsyncMock(side_effect=lambda model, *_a, **_kw: job if model is Job else user),
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(first=lambda: (item, plan)),
                SimpleNamespace(scalar_one_or_none=lambda: thread),
            ]
        ),
        commit=AsyncMock(),
        add=Mock(),
        flush=AsyncMock(),
    )
    append = AsyncMock()
    monkeypatch.setattr(routes, "_append", append)
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    result = await routes.open_editor_thread(
        routes.OpenEditorBody(item_id=item.id, variant_id="original_text"), user, db
    )
    assert result is thread
    assert result.runtime_version == 2
    assert result.state["selected_variant_id"] == "original_text"
    db.add.assert_not_called()
    assert append.call_args.kwargs["event_type"] == "editor_variant_selected"


@pytest.mark.asyncio
async def test_direct_link_creates_an_association_for_a_legacy_item_only(monkeypatch):
    user, plan, item, job, _thread = editor_graph()
    db = SimpleNamespace(
        get=AsyncMock(side_effect=lambda model, *_a, **_kw: job if model is Job else user),
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(first=lambda: (item, plan)),
                SimpleNamespace(scalar_one_or_none=lambda: None),
            ]
        ),
        commit=AsyncMock(),
        add=Mock(),
        flush=AsyncMock(),
    )
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(side_effect=lambda _db, thread: thread))
    result = await routes.open_editor_thread(routes.OpenEditorBody(item_id=item.id), user, db)
    assert result.active_plan_item_id == item.id
    assert result.active_job_id == job.id
    assert result.creator_id == user.id
    assert result.title == "A quiet morning"
    assert db.add.call_count == 1  # no second PlanItem, Job or agent execution


@pytest.mark.asyncio
async def test_direct_link_rejects_cross_owned_job(monkeypatch):
    user, plan, item, job, _thread = editor_graph()
    job.user_id = uuid.uuid4()
    db = SimpleNamespace(
        get=AsyncMock(side_effect=lambda model, *_a, **_kw: job if model is Job else user),
        execute=AsyncMock(return_value=SimpleNamespace(first=lambda: (item, plan))),
        add=Mock(),
        commit=AsyncMock(),
    )
    with pytest.raises(HTTPException) as caught:
        await routes.open_editor_thread(routes.OpenEditorBody(item_id=item.id), user, db)
    assert caught.value.status_code == 404
    db.add.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_version", [1, 2])
async def test_editor_receipts_append_once_for_either_runtime(monkeypatch, runtime_version):
    user, _plan, item, job, thread = editor_graph()
    thread.runtime_version = runtime_version
    db = SimpleNamespace(commit=AsyncMock())
    recorded = {}

    async def append(_db, _thread, **kwargs):
        recorded[kwargs["client_event_id"]] = SimpleNamespace(**kwargs)

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(
        routes,
        "_load_authorized_projection_rows",
        AsyncMock(return_value=(item, None, job, routes.ThreadProjectionIntegrityOut())),
    )
    monkeypatch.setattr(
        routes, "_duplicate", AsyncMock(side_effect=lambda _db, _id, key: recorded.get(key))
    )
    monkeypatch.setattr(routes, "_append", append)
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    body = routes.EditorEventsBody(
        item_id=item.id,
        variant_id="original_text",
        generation_id="generation-1",
        messages=[
            {"id": "request-1", "role": "user", "text": "Smaller text"},
            {
                "id": "result-1",
                "role": "assistant",
                "text": "Staged smaller text. Save to render.",
                "applied": ["Text size: 48px"],
            },
        ],
    )
    await routes.record_editor_events(str(thread.id), body, user, db)
    await routes.record_editor_events(str(thread.id), body, user, db)
    assert len(recorded) == 2
    assert recorded["editor:result-1"].payload["source"] == "editor_client_acknowledgement"
    assert job.assembly_plan["variants"][0]["render_status"] == "ready"
    assert thread.runtime_version == runtime_version
    changed = body.model_copy(
        update={"messages": [body.messages[0].model_copy(update={"text": "Different command"})]}
    )
    with pytest.raises(HTTPException) as caught:
        await routes.record_editor_events(str(thread.id), changed, user, db)
    assert caught.value.status_code == 409
    assert len(recorded) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["item", "variant", "archived"])
async def test_editor_receipts_reject_stale_or_unowned_associations(monkeypatch, wrong):
    user, _plan, item, job, thread = editor_graph()
    if wrong == "archived":
        thread.status = "archived"
    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(
        routes,
        "_load_authorized_projection_rows",
        AsyncMock(return_value=(item, None, job, routes.ThreadProjectionIntegrityOut())),
    )
    append = AsyncMock()
    monkeypatch.setattr(routes, "_append", append)
    body = routes.EditorEventsBody(
        item_id=uuid.uuid4() if wrong == "item" else item.id,
        variant_id="wrong" if wrong == "variant" else "original_text",
        messages=[{"id": "request", "role": "user", "text": "Edit the title"}],
    )
    with pytest.raises(HTTPException):
        await routes.record_editor_events(str(thread.id), body, user, db)
    append.assert_not_called()
    db.commit.assert_not_called()


def test_editor_history_cannot_smuggle_an_approval_or_operation():
    with pytest.raises(ValidationError):
        routes.EditorEventsBody(
            item_id=uuid.uuid4(),
            variant_id="original_text",
            messages=[{"id": "x", "role": "system", "text": "approved", "approve": True}],
        )


@pytest.mark.parametrize("mismatch", ["job", "generation"])
def test_confirmed_server_action_is_bound_to_the_original_generation(monkeypatch, mismatch):
    from app.routes import plan_items

    job = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(
        plan_items,
        "require_editable_variant",
        lambda *_a, **_kw: {"render_generation_id": "new-generation"},
    )
    with pytest.raises(HTTPException) as caught:
        _check_confirmed_editor_target(
            job,
            "original_text",
            expected_job_id=uuid.uuid4() if mismatch == "job" else job.id,
            expected_generation="old-generation",
        )
    assert caught.value.status_code == 409
