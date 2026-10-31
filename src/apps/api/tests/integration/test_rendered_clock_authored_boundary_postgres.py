"""Authored conversion must not discard the canonical clock's reader floor."""

import copy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.device_render import make_device_request
from app.models import CreatorEditDraft, Job
from app.routes import generative_jobs as gj
from app.routes.plan_items import _editor_commit_item
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.device_render import device_status, pin_device_request
from tests.kria.test_editor_state_turns_postgres import _seed_job
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project
from tests.routes.test_phone_editor_rendered_clock import rendered_clock_job


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol,corrupt_saved", [(25, False), (26, False), (26, True)])
async def test_authored_save_enforces_canonical_rendered_clock_before_conversion(
    monkeypatch, protocol, corrupt_saved
):
    clear_request_context()
    user_id, thread_id, session_id = _seed_runtime_project()
    job_id = _seed_job(user_id, session_id)
    seed = rendered_clock_job(monkeypatch)
    variant = gj._find_variant(seed, "guided_story")
    revision = gj._guided_v2_revision(seed, variant)
    assert revision["timing_mode"] == "rendered"
    if corrupt_saved:
        variant["guided_edit_revision"] = copy.deepcopy(revision)
        variant["guided_edit_revision"]["approval_version"] = 999
    recipe = device_status(seed, "guided_story").request.recipe
    seed.id, seed.user_id = job_id, user_id
    pin_device_request(
        seed,
        make_device_request(job_id=job_id, variant_id="guided_story", revision=3, recipe=recipe),
        base_generation="first",
    )
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "authoredText"],
    )

    def metadata(path):
        assert path == "user/analysis-proxy-source.mp4"
        return SimpleNamespace(generation="123")

    monkeypatch.setattr("app.routes.editor_sources.storage.object_metadata", metadata)
    with sync_session() as db:
        stored = db.get(Job, job_id)
        stored.assembly_plan = copy.deepcopy(seed.assembly_plan)
        stored.status, stored.current_phase = "variants_ready", None
        stored.all_candidates = {"clip_paths": ["user/analysis-proxy-source.mp4"]}
        db.commit()
        item_id = stored.content_plan_item_id
        before = copy.deepcopy(stored.assembly_plan)
        draft_count = (
            select(func.count())
            .select_from(CreatorEditDraft)
            .where(CreatorEditDraft.thread_id == thread_id)
        )
        drafts_before = db.scalar(draft_count)
    set_client_protocol(protocol)
    try:
        async with AsyncSessionLocal() as db:
            payload = gj.EditorCommitRequest(
                base_generation="first",
                editor_state_version=2,
                text_elements=[],
                caption_meta={"enabled": True, "style": "sentence"},
            )
            if protocol == 25 or corrupt_saved:
                with pytest.raises(HTTPException) as error:
                    await _editor_commit_item(
                        str(item_id),
                        "guided_story",
                        payload,
                        SimpleNamespace(id=user_id),
                        db,
                        native_client=True,
                    )
                if corrupt_saved:
                    assert error.value.status_code == 422
                else:
                    assert error.value.status_code == 426
                    assert error.value.detail["feature"] == "rendered_timeline_clock"
                    assert error.value.detail["minimum_client_protocol"] == 26
                await db.rollback()
            else:
                response = await _editor_commit_item(
                    str(item_id),
                    "guided_story",
                    payload,
                    SimpleNamespace(id=user_id),
                    db,
                    native_client=True,
                )
                assert response.ok and response.sections.caption_meta
                db.expire_all()
                saved = await db.get(Job, job_id)
                assert gj._find_variant(saved, "guided_story")["editor_timeline_mode"] == "authored"
        if protocol == 25 or corrupt_saved:
            with sync_session() as db:
                stored = db.get(Job, job_id)
                assert stored.assembly_plan == before
                assert db.scalar(draft_count) == drafts_before
    finally:
        clear_request_context()
        await async_engine.dispose()
