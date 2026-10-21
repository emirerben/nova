"""Real Save transaction/reopen regression; no render broker or provider calls."""

import copy
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models import ContentPlan, CreatorEditDraft, Job, Persona, PlanItem, User
from app.routes.generative_jobs import EditorCommitRequest, TimelineSlotEdit
from app.routes.plan_items import _editor_commit_item


@pytest_asyncio.fixture
async def saved_project(monkeypatch):
    from tests.routes.test_editor_commit import _arm, _job

    _arm(monkeypatch)
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    engine = create_async_engine(settings.asyncpg_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uid = uuid.uuid4()
    async with factory() as db:
        db.add(User(id=uid, email=f"durable-save-{uid}@example.test"))
        await db.flush()
        persona = Persona(user_id=uid, persona={})
        db.add(persona)
        await db.flush()
        plan = ContentPlan(user_id=uid, persona_id=persona.id)
        db.add(plan)
        await db.flush()
        item = PlanItem(content_plan_id=plan.id, position=0, idea="Draft Save journey")
        db.add(item)
        await db.flush()
        seed = _job(
            editor_timeline_mode="authored",
            text_elements=[
                {
                    "id": "horse",
                    "text": "The Trojan Horse replica",
                    "start_s": 0,
                    "end_s": 3,
                    "role": "generative_intro",
                    "position": "middle",
                },
                {
                    "id": "road",
                    "text": "Road trip from Assos to Troya",
                    "start_s": 3,
                    "end_s": 5.4,
                    "role": "generative_intro",
                    "position": "middle",
                },
            ],
        )
        job = Job(
            id=seed.id,
            user_id=uid,
            content_plan_item_id=item.id,
            mode="generative",
            raw_storage_path="fixture",
            status="variants_ready",
            assembly_plan=seed.assembly_plan,
            all_candidates=seed.all_candidates,
        )
        db.add(job)
        await db.flush()
        item.current_job_id = job.id
        await db.commit()
        item_id = item.id
        yield SimpleNamespace(db=db, uid=uid, item=item, job=job)
        await db.rollback()
        await db.execute(update(PlanItem).where(PlanItem.id == item_id).values(current_job_id=None))
        await db.execute(delete(Job).where(Job.user_id == uid))
        await db.execute(delete(User).where(User.id == uid))
        await db.commit()
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [1, 2])
async def test_save_reopens_exact_authored_window_when_render_cannot_start(
    saved_project, monkeypatch, protocol
):
    j = saved_project

    def broker_unavailable(*args, **kwargs):
        raise RuntimeError("offline broker fixture")

    monkeypatch.setattr("app.routes.plan_items.enqueue_editor_commit_render", broker_unavailable)
    request = EditorCommitRequest(
        base_generation="2026-07-01T00:00:00Z",
        editor_state_version=protocol,
        deletions=[{"kind": "text", "id": "horse"}],
        timeline_slots=[
            TimelineSlotEdit(slot_id="s1", clip_index=0, in_s=7.54, duration_s=3),
            TimelineSlotEdit(slot_id="s2", clip_index=1, in_s=1, duration_s=0.794),
        ],
        text_elements=[
            {
                "id": "road",
                "text": "Road trip from Assos to Troya",
                "start_s": 0,
                "end_s": 3,
                "role": "generative_intro",
                "position": "middle",
            }
        ],
    )
    authored = copy.deepcopy(request.model_dump(mode="json"))
    if protocol == 1:
        with pytest.raises(HTTPException) as refused:
            await _editor_commit_item(
                str(j.item.id), "song_text", request, SimpleNamespace(id=j.uid), j.db
            )
        assert refused.value.detail["code"] == "TIMELINE_OUT_OF_BOUNDS"
        assert (
            await j.db.scalar(select(CreatorEditDraft).where(CreatorEditDraft.creator_id == j.uid))
            is None
        )
        return
    result = await _editor_commit_item(
        str(j.item.id), "song_text", request, SimpleNamespace(id=j.uid), j.db
    )
    assert result.ok
    # Expire ORM state: assertions must read a committed snapshot, not a
    # staging object that happened to survive the compiler/broker exception.
    j.db.expire_all()
    draft = await j.db.scalar(select(CreatorEditDraft).where(CreatorEditDraft.creator_id == j.uid))
    assert draft is not None
    sections = draft.snapshot_json["editor_payload"]["sections"]
    assert sections["timeline_slots"][0]["in_s"] == authored["timeline_slots"][0]["in_s"]
    assert sections["timeline_slots"][0]["duration_s"] == 3
    assert sections["timeline_slots"][1]["duration_s"] == 0.794
    assert sections["text_elements"] == authored["text_elements"]
    from app.services.editor_empty_drafts import attach_saved_editor_drafts, saved_editor_draft

    loaded = await j.db.get(Job, draft.base_job_id, populate_existing=True)
    await attach_saved_editor_drafts(j.db, loaded)
    reopened = saved_editor_draft(loaded, "song_text")
    assert reopened.snapshot["editor_payload"]["sections"] == sections


@pytest.mark.asyncio
async def test_unresolvable_render_still_commits_draft_before_dispatch(saved_project, monkeypatch):
    j = saved_project
    monkeypatch.setattr(
        "app.routes.plan_items.enqueue_editor_commit_render",
        lambda *a, **k: pytest.fail("unresolvable render was dispatched"),
    )
    response = await _editor_commit_item(
        str(j.item.id),
        "song_text",
        EditorCommitRequest(
            base_generation="2026-07-01T00:00:00Z",
            editor_state_version=2,
            timeline_slots=[TimelineSlotEdit(slot_id="s1", clip_index=0, in_s=0, duration_s=11)],
        ),
        SimpleNamespace(id=j.uid),
        j.db,
    )
    assert response.ok and response.render_started is False
    assert "footage" in response.render_recovery
    j.db.expire_all()
    draft = await j.db.scalar(select(CreatorEditDraft).where(CreatorEditDraft.creator_id == j.uid))
    assert (
        draft.snapshot_json["editor_payload"]["sections"]["timeline_slots"][0]["duration_s"] == 11
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["owner", "stale", "unknown_slot", "unknown_source", "nonfinite", "oversized"]
)
async def test_protocol_two_refuses_invalid_save_without_durable_write(
    saved_project, monkeypatch, case
):
    from pydantic import ValidationError

    j = saved_project
    monkeypatch.setattr(
        "app.routes.plan_items.enqueue_editor_commit_render",
        lambda *a, **k: pytest.fail("invalid Save dispatched"),
    )
    data = dict(
        base_generation="2026-07-01T00:00:00Z",
        editor_state_version=2,
        timeline_slots=[dict(slot_id="s1", clip_index=0, in_s=7.54, duration_s=3)],
    )
    actor = j.uid
    if case == "owner":
        actor = uuid.uuid4()
    if case == "stale":
        data["base_generation"] = "older-generation"
    if case == "unknown_slot":
        data["timeline_slots"][0]["slot_id"] = "real-looking-unknown-slot"
    if case == "unknown_source":
        data["timeline_slots"][0]["clip_index"] = 999
    if case == "nonfinite":
        data["timeline_slots"][0]["in_s"] = float("nan")
    if case == "oversized":
        data["timeline_slots"] *= 1001
    with pytest.raises((HTTPException, ValidationError)) as rejected:
        body = EditorCommitRequest.model_validate(data)
        await _editor_commit_item(
            str(j.item.id), "song_text", body, SimpleNamespace(id=actor), j.db
        )
    if case == "unknown_slot":
        assert rejected.value.status_code == 409
    assert (
        await j.db.scalar(select(CreatorEditDraft).where(CreatorEditDraft.creator_id == j.uid))
        is None
    )


@pytest.mark.asyncio
async def test_new_slot_identity_survives_two_saves_and_reopen(saved_project, monkeypatch):
    j = saved_project
    monkeypatch.setattr("app.routes.plan_items.enqueue_editor_commit_render", lambda *a, **k: None)
    first = await _editor_commit_item(
        str(j.item.id),
        "song_text",
        EditorCommitRequest(
            base_generation="2026-07-01T00:00:00Z",
            editor_state_version=2,
            timeline_slots=[TimelineSlotEdit(clip_index=0, in_s=7.54, duration_s=3)],
        ),
        SimpleNamespace(id=j.uid),
        j.db,
    )
    rows = first.draft.snapshot["editor_payload"]["sections"]["timeline_slots"]
    assert rows[0]["slot_id"]
    second = await _editor_commit_item(
        str(j.item.id),
        "song_text",
        EditorCommitRequest(
            base_generation=first.generation,
            editor_state_version=2,
            timeline_slots=[TimelineSlotEdit.model_validate(rows[0])],
        ),
        SimpleNamespace(id=j.uid),
        j.db,
    )
    assert second.draft.snapshot["editor_payload"]["sections"]["timeline_slots"][0] == rows[0]


@pytest.mark.asyncio
async def test_late_render_cannot_replace_protocol_two_snapshot(saved_project, monkeypatch):
    import app.tasks.generative_build as gb
    from tests.tasks.test_render_generation_guard import _patch_sessions

    j = saved_project
    monkeypatch.setattr("app.routes.plan_items.enqueue_editor_commit_render", lambda *a, **k: None)
    response = await _editor_commit_item(
        str(j.item.id),
        "song_text",
        EditorCommitRequest(
            base_generation="2026-07-01T00:00:00Z",
            editor_state_version=2,
            timeline_slots=[TimelineSlotEdit(slot_id="s1", clip_index=0, in_s=7.54, duration_s=3)],
        ),
        SimpleNamespace(id=j.uid),
        j.db,
    )
    before = copy.deepcopy(j.job.assembly_plan)
    _patch_sessions(monkeypatch, j.job)
    monkeypatch.setattr(gb, "_attach_variant_posters", lambda patch, **kw: (patch, []))
    assert not gb._update_variant_entry(
        str(j.job.id),
        "song_text",
        {"render_status": "ready", "text_elements": []},
        expected_render_gen_id="2026-07-01T00:00:00Z",
    )
    assert j.job.assembly_plan == before
    draft = await j.db.get(CreatorEditDraft, uuid.UUID(response.draft.draft_id))
    assert draft.snapshot_json["editor_payload"]["sections"]["timeline_slots"][0]["in_s"] == 7.54


@pytest.mark.asyncio
async def test_owned_expired_source_keeps_exact_draft_with_recovery(saved_project, monkeypatch):
    j = saved_project
    from app.routes.generative_jobs import _durable_sources_prefix

    path = _durable_sources_prefix(j.job) + "expired.mp4"
    candidates = copy.deepcopy(j.job.all_candidates)
    candidates["clip_paths"][0] = path
    j.job.all_candidates = candidates
    await j.db.commit()
    monkeypatch.setattr("app.storage.object_exists", lambda path: False)
    monkeypatch.setattr(
        "app.routes.plan_items.enqueue_editor_commit_render",
        lambda *a, **k: pytest.fail("missing source dispatched"),
    )
    response = await _editor_commit_item(
        str(j.item.id),
        "song_text",
        EditorCommitRequest(
            base_generation="2026-07-01T00:00:00Z",
            editor_state_version=2,
            timeline_slots=[TimelineSlotEdit(slot_id="s1", clip_index=0, in_s=1, duration_s=3)],
        ),
        SimpleNamespace(id=j.uid),
        j.db,
    )
    assert response.ok and response.render_started is False
    assert "reconnect" in response.render_recovery.lower()
    assert response.draft.snapshot["editor_payload"]["sections"]["timeline_slots"][0]["in_s"] == 1
