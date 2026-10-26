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
@pytest.mark.parametrize("initial_slot_id", [None, "cs-369BA950-52A-27"])
async def test_new_slot_identity_survives_two_saves_and_reopen(
    saved_project, monkeypatch, initial_slot_id
):
    j = saved_project
    monkeypatch.setattr("app.routes.plan_items.enqueue_editor_commit_render", lambda *a, **k: None)
    first = await _editor_commit_item(
        str(j.item.id),
        "song_text",
        EditorCommitRequest(
            base_generation="2026-07-01T00:00:00Z",
            editor_state_version=2,
            timeline_slots=[
                TimelineSlotEdit(slot_id=initial_slot_id, clip_index=0, in_s=7.54, duration_s=3)
            ],
        ),
        SimpleNamespace(id=j.uid),
        j.db,
    )
    rows = first.draft.snapshot["editor_payload"]["sections"]["timeline_slots"]
    assert rows[0]["slot_id"]
    assert not rows[0]["slot_id"].startswith("cs-")
    assert rows[0]["clip_index"] == 0
    assert rows[0]["in_s"] == 7.54
    assert rows[0]["duration_s"] == 3
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
    j.db.expire_all()
    draft = await j.db.scalar(select(CreatorEditDraft).where(CreatorEditDraft.creator_id == j.uid))
    assert draft.creator_id == j.uid
    assert draft.snapshot_json["editor_payload"]["sections"]["timeline_slots"][0] == rows[0]
    from app.services.editor_empty_drafts import attach_saved_editor_drafts, saved_editor_draft

    loaded = await j.db.get(Job, draft.base_job_id, populate_existing=True)
    assert loaded.user_id == j.uid
    await attach_saved_editor_drafts(j.db, loaded)
    reopened = saved_editor_draft(loaded, "song_text")
    assert reopened.snapshot["editor_payload"]["sections"]["timeline_slots"][0] == rows[0]


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


@pytest_asyncio.fixture
async def talking_project(saved_project, monkeypatch):
    from app import storage
    from tests.routes.test_editor_save_contract_matrix import _talking_edit_job

    j = saved_project
    seed = _talking_edit_job(monkeypatch, job_id=j.job.id)
    j.job.assembly_plan = seed.assembly_plan
    j.job.all_candidates = seed.all_candidates
    await j.db.commit()
    monkeypatch.setattr(storage, "object_metadata", lambda _: SimpleNamespace(generation="1"))
    return j


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["trim", "slide", "reorder", "combined"])
async def test_talking_actual_save_reopen_recipe(talking_project, operation):
    from app.routes import generative_jobs as gj
    from app.services.device_render import device_status
    from app.services.editor_empty_drafts import attach_saved_editor_drafts, saved_editor_draft

    j = talking_project
    item_id, job_id = str(j.item.id), j.job.id
    original = gj.editor_deletion_timeline(j.job, j.job.assembly_plan["variants"][0])

    async def save(rows, generation, deletions=None):
        return await _editor_commit_item(
            item_id,
            "original_text",
            EditorCommitRequest(
                base_generation=generation,
                editor_state_version=2,
                timeline_slots=[{k: v for k, v in r.items() if v is not None} for r in rows],
                deletions=deletions or [],
            ),
            SimpleNamespace(id=j.uid),
            j.db,
            native_client=True,
        )

    deleted = await save(original, "first", [{"kind": "clip", "id": original[1]["slot_id"]}])
    assert deleted.ok
    j.db.expire_all()
    loaded = await j.db.get(Job, job_id, populate_existing=True)
    await attach_saved_editor_drafts(j.db, loaded)
    snapshot = saved_editor_draft(loaded, "original_text")
    rows = [
        copy.deepcopy(r)
        for r in snapshot.snapshot["editor_payload"]["sections"]["timeline_slots"]
        if not r.get("removed")
    ]
    if operation in {"trim", "combined"}:
        rows[1].update(in_s=10, duration_s=2, source_start_locked=True)
    if operation == "slide":
        rows[1].update(in_s=7, source_start_locked=True)
    if operation in {"reorder", "combined"}:
        rows.reverse()
    result = await save(rows, deleted.generation)
    assert result.ok and result.render_started is not False, result
    j.db.expire_all()
    loaded = await j.db.get(Job, job_id, populate_existing=True)
    await attach_saved_editor_drafts(j.db, loaded)
    reopened = saved_editor_draft(loaded, "original_text")
    sections = reopened.snapshot["editor_payload"]["sections"]
    assert [
        (r["in_s"], r["duration_s"]) for r in sections["timeline_slots"] if not r.get("removed")
    ] == [(r["in_s"], r["duration_s"]) for r in rows]
    assert not any("Bravo" in c["text"] for c in sections["caption_cues"])
    assert not any(
        (row.get("source_params") or {}).get("source") == "caption_cue"
        for row in sections.get("text_elements", [])
    )
    delta = next(
        w for c in sections["caption_cues"] for w in c.get("words", []) if w["text"] == "Delta"
    )
    assert delta["start_s"] == pytest.approx(
        {"trim": 2.2, "slide": 5.2, "reorder": 2.2, "combined": 0.2}[operation]
    )
    recipe = device_status(loaded, "original_text").request.recipe
    assert recipe.duration == pytest.approx(sum(r["duration_s"] for r in rows))
    with pytest.raises(HTTPException) as stale:
        await save(rows, deleted.generation)
    assert stale.value.status_code == 409 and stale.value.detail == "baseline_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", ["pending", "auto_projected", "disable", "own_captions"])
async def test_missing_speech_words_save_exact_draft_and_recover(
    talking_project, monkeypatch, resolution
):
    from app.routes import generative_jobs as gj
    from app.services.editor_empty_drafts import attach_saved_editor_drafts, saved_editor_draft

    j = talking_project
    item_id, job_id = str(j.item.id), j.job.id
    assembly = copy.deepcopy(j.job.assembly_plan)
    assembly.pop("_speech_cleanup_internal", None)
    j.job.assembly_plan = assembly
    await j.db.commit()
    rows = gj.editor_deletion_timeline(j.job, assembly["variants"][0])
    first = await _editor_commit_item(
        item_id,
        "original_text",
        EditorCommitRequest(
            base_generation="first",
            editor_state_version=2,
            timeline_slots=rows,
            deletions=[{"kind": "clip", "id": rows[1]["slot_id"]}],
        ),
        SimpleNamespace(id=j.uid),
        j.db,
        native_client=True,
    )
    rows = [
        copy.deepcopy(r)
        for r in j.job.assembly_plan["variants"][0]["user_timeline"]["slots"]
        if not r.get("removed")
    ]
    rows[0].update(in_s=1, source_start_locked=True)
    extra = {}
    if resolution == "disable":
        extra["caption_meta"] = {"enabled": False}
    if resolution == "own_captions":
        extra["caption_cues"] = [
            {"id": "creator", "text": "My chosen caption", "start_s": 0, "end_s": 2}
        ]
    if resolution == "auto_projected":
        from app.pipeline.speech_cut_state import project_speech_basis

        basis = j.job.assembly_plan["variants"][0]["speech_timing_basis"]
        extra["caption_cues"] = project_speech_basis(basis["caption_cues"], rows, captions=True)
    response = await _editor_commit_item(
        item_id,
        "original_text",
        EditorCommitRequest(
            base_generation=first.generation,
            editor_state_version=2,
            timeline_slots=[{k: v for k, v in r.items() if v is not None} for r in rows],
            **extra,
        ),
        SimpleNamespace(id=j.uid),
        j.db,
        native_client=True,
    )
    assert response.ok
    if resolution in {"pending", "auto_projected"}:
        assert response.render_started is False and "caption" in response.render_recovery.lower()
    else:
        assert response.render_started is not False, response.render_recovery
    j.db.expire_all()
    loaded = await j.db.get(Job, job_id, populate_existing=True)
    await attach_saved_editor_drafts(j.db, loaded)
    sections = saved_editor_draft(loaded, "original_text").snapshot["editor_payload"]["sections"]
    assert sections["timeline_slots"][0]["in_s"] == 1
    assert sections["timeline_slots"][0]["source_start_locked"]

    if resolution == "pending":
        # Empty visible captions still carry the missing source evidence state.
        rows = copy.deepcopy(sections["timeline_slots"])
        rows[0].update(in_s=5, duration_s=2)
        rows[1].update(in_s=5, duration_s=2)
        again = await _editor_commit_item(
            item_id,
            "original_text",
            EditorCommitRequest(
                base_generation=response.generation,
                editor_state_version=2,
                timeline_slots=[{k: v for k, v in r.items() if v is not None} for r in rows],
            ),
            SimpleNamespace(id=j.uid),
            j.db,
            native_client=True,
        )
        assert again.ok and again.render_started is False
        assert not j.job.assembly_plan["variants"][0]["caption_cues"]
        again = await _editor_commit_item(
            item_id,
            "original_text",
            EditorCommitRequest(
                base_generation=again.generation,
                editor_state_version=2,
                timeline_slots=[{k: v for k, v in r.items() if v is not None} for r in rows],
            ),
            SimpleNamespace(id=j.uid),
            j.db,
            native_client=True,
        )
        assert again.ok and again.render_started is False


@pytest.mark.asyncio
async def test_concurrent_talking_saves_accept_one_revision(talking_project):
    import asyncio

    from app.routes import generative_jobs as gj

    j = talking_project
    item_id = str(j.item.id)
    rows = gj.editor_deletion_timeline(j.job, j.job.assembly_plan["variants"][0])
    body = EditorCommitRequest(
        base_generation="first",
        editor_state_version=2,
        timeline_slots=rows,
        deletions=[{"kind": "clip", "id": rows[1]["slot_id"]}],
    )
    factory = async_sessionmaker(j.db.bind, expire_on_commit=False)
    await j.db.commit()

    async def save():
        async with factory() as db:
            try:
                result = await _editor_commit_item(
                    item_id,
                    "original_text",
                    body.model_copy(deep=True),
                    SimpleNamespace(id=j.uid),
                    db,
                    native_client=True,
                )
                return result.ok
            except HTTPException as exc:
                await db.rollback()
                return exc.status_code, exc.detail

    results = await asyncio.gather(save(), save())
    assert results.count(True) == 1
    assert (409, "baseline_conflict") in results


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["owner", "source_generation", "bounds", "foreign_slot"])
async def test_talking_route_security_and_bounds_leave_no_draft(
    talking_project, monkeypatch, fault
):
    from app import storage
    from app.routes import generative_jobs as gj

    j = talking_project
    rows = gj.editor_deletion_timeline(j.job, j.job.assembly_plan["variants"][0])
    before = copy.deepcopy(j.job.assembly_plan)
    if fault == "source_generation":
        monkeypatch.setattr(
            storage, "object_metadata", lambda _: SimpleNamespace(generation="replaced")
        )
    if fault == "bounds":
        rows[0].update(in_s=11, duration_s=2)
    if fault == "foreign_slot":
        rows[0]["slot_id"] += "-foreign"
    with pytest.raises(HTTPException) as error:
        await _editor_commit_item(
            str(j.item.id),
            "original_text",
            EditorCommitRequest(
                base_generation="first",
                editor_state_version=2,
                timeline_slots=rows,
                deletions=[{"kind": "clip", "id": rows[1]["slot_id"]}],
            ),
            SimpleNamespace(id=uuid.uuid4() if fault == "owner" else j.uid),
            j.db,
            native_client=True,
        )
    assert (
        error.value.status_code
        == {"owner": 404, "source_generation": 409, "bounds": 422, "foreign_slot": 409}[fault]
    )
    assert j.job.assembly_plan == before
    assert (
        await j.db.scalar(select(CreatorEditDraft).where(CreatorEditDraft.creator_id == j.uid))
        is None
    )
