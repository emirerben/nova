"""Consent -> validated Save -> finished project -> inactive suggestion -> approval.

Failure inventory: docs/reviews/kri-648-learning.md (captured before source edits).
Real migrated PostgreSQL; no model/provider calls or private message fixtures.
"""

import copy
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models import (
    ContentPlan,
    CreatorMemoryItem,
    CreatorStyleLearningProject,
    Job,
    Persona,
    PlanItem,
    User,
)
from app.services.creator_direction import (
    CreatorDirectionResolver,
    CreatorDirectionService,
    StaleRevision,
)
from app.services.finished_style_learning import (
    record_finished_style_edit,
    reset_learning_history,
    set_learning_enabled,
    stage_style_edit,
)


@pytest_asyncio.fixture
async def journey(monkeypatch):
    engine = create_async_engine(settings.asyncpg_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uid = uuid.uuid4()
    monkeypatch.setattr(settings, "creator_memory_enabled", True)
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(settings, "creator_edit_learning_enabled", True)
    monkeypatch.setattr(settings, "creator_edit_learning_privacy_approved", True)
    monkeypatch.setattr(settings, "creator_edit_learning_user_ids", [str(uid)])
    async with factory() as db:
        db.add(User(id=uid, email=f"learning-{uid}@example.test"))
        await db.commit()
    async with factory() as db:
        yield SimpleNamespace(db=db, uid=uid)
        await db.rollback()
        await db.execute(
            update(PlanItem)
            .where(
                PlanItem.content_plan_id.in_(
                    select(ContentPlan.id).where(ContentPlan.user_id == uid)
                )
            )
            .values(current_job_id=None)
        )
        await db.execute(delete(Job).where(Job.user_id == uid))
        await db.execute(delete(User).where(User.id == uid))
        await db.commit()
    await engine.dispose()


async def opt_in(j):
    user = await j.db.get(User, j.uid)
    await set_learning_enabled(j.db, j.uid, True, user.creator_memory_revision, str(uuid.uuid4()))
    await j.db.commit()


async def project(j):
    persona = await j.db.scalar(select(Persona).where(Persona.user_id == j.uid))
    if persona is None:
        persona = Persona(user_id=j.uid, persona={})
        j.db.add(persona)
        await j.db.flush()
    plan = ContentPlan(user_id=j.uid, persona_id=persona.id)
    j.db.add(plan)
    await j.db.flush()
    item = PlanItem(content_plan_id=plan.id, position=0, idea="Style journey")
    j.db.add(item)
    await j.db.flush()
    job = Job(
        user_id=j.uid, mode="generative", content_plan_item_id=item.id, raw_storage_path="fixture"
    )
    j.db.add(job)
    await j.db.flush()
    item.current_job_id = job.id
    job.assembly_plan = {"variants": [{"variant_id": "v", "render_destination": "device"}]}
    await j.db.commit()
    return job


def rows(**props):
    return {
        "text_elements": [
            {
                "id": "title",
                "role": "generative_intro",
                "font_family": "Inter",
                "color": "#FFFFFF",
                "position": "bottom",
                "stroke_width": 2,
                "shadow_enabled": True,
                **props,
            }
        ]
    }


async def stage(j, job, before=None, after=None, *, temporary=False, digest=None):
    digest = digest or uuid.uuid4().hex * 2
    identity = SimpleNamespace(recipe_digest=digest, recipe_revision=1, variant_id="v")
    await stage_style_edit(
        j.db,
        j.uid,
        job,
        "v",
        before or rows(),
        after or rows(position="top", stroke_width=0, shadow_enabled=False),
        sections={"text_elements"},
        identity=identity,
        temporary=temporary,
    )
    await j.db.commit()
    return identity


async def finish(j, job, identity, *, phase="published"):
    status = SimpleNamespace(phase=phase, request=SimpleNamespace(identity=identity))
    await record_finished_style_edit(j.db, j.uid, job, "v", status)
    await j.db.commit()


async def suggestions(j):
    return list(
        (
            await j.db.scalars(
                select(CreatorMemoryItem).where(
                    CreatorMemoryItem.user_id == j.uid, CreatorMemoryItem.state == "suggested"
                )
            )
        ).all()
    )


@pytest.mark.asyncio
async def test_three_distinct_finished_projects_suggest_but_never_activate(journey):
    j = journey
    await opt_in(j)
    for index in range(3):
        job = await project(j)
        identity = await stage(j, job)
        await finish(j, job, identity, phase="syncing")
        if index < 2:
            assert await suggestions(j) == []
        await finish(j, job, identity)
        await finish(j, job, identity)  # duplicate publication
        if index < 2:
            assert await suggestions(j) == []
    items = await suggestions(j)
    assert {next(iter(i.structured_value["text_style_title"])) for i in items} == {
        "position",
        "stroke_width",
        "shadow_enabled",
    }
    assert all(i.evidence["supporting_video_count"] == 3 for i in items)
    assert (await CreatorDirectionResolver().snapshot(j.db, j.uid)).typed_overrides == {}
    assert (
        len(
            (
                await j.db.scalars(
                    select(CreatorStyleLearningProject).where(
                        CreatorStyleLearningProject.user_id == j.uid
                    )
                )
            ).all()
        )
        == 3
    )


@pytest.mark.asyncio
async def test_accept_merges_only_proposed_property_and_replays_revision(journey):
    j = journey
    await opt_in(j)
    service = CreatorDirectionService()
    user = await j.db.get(User, j.uid)
    await service.create_item(
        j.db,
        j.uid,
        instruction="My title choices",
        category="video_style",
        enforcement="default",
        normalized_key="text_style_title",
        structured_value={
            "text_style_title": {"color": "#FFCC00", "shadow_enabled": False, "stroke_width": 0}
        },
        expected_revision=user.creator_memory_revision,
        idempotency_key="base",
    )
    await j.db.commit()
    for _ in range(3):
        job = await project(j)
        identity = await stage(j, job, after=rows(position="top", font_family="Inter Medium"))
        await finish(j, job, identity)
    target = next(
        i
        for i in await suggestions(j)
        if "favorite_fonts" in i.structured_value["text_style_title"]
    )
    target_id = target.id
    user = await j.db.get(User, j.uid)
    rev = user.creator_memory_revision
    with pytest.raises(StaleRevision):
        await service.set_item_state(
            j.db,
            j.uid,
            target_id,
            state="active",
            required_state="suggested",
            expected_revision=rev - 1,
            idempotency_key="stale",
        )
    await j.db.rollback()
    op = await service.set_item_state(
        j.db,
        j.uid,
        target_id,
        state="active",
        required_state="suggested",
        expected_revision=rev,
        idempotency_key="accept",
    )
    await j.db.commit()
    duplicate = await service.set_item_state(
        j.db,
        j.uid,
        target_id,
        state="active",
        required_state="suggested",
        expected_revision=rev,
        idempotency_key="accept",
    )
    assert duplicate.id == op.id
    rule = (await CreatorDirectionResolver().snapshot(j.db, j.uid)).typed_overrides[
        "text_style_title"
    ]
    assert rule == {
        "color": "#FFCC00",
        "shadow_enabled": False,
        "stroke_width": 0,
        "favorite_fonts": ["Inter Medium"],
    }


@pytest.mark.asyncio
async def test_undo_temporary_conflicting_and_stale_recipe_do_not_vote(journey):
    j = journey
    await opt_in(j)
    job = await project(j)
    await stage(j, job)
    identity = await stage(
        j, job, before=rows(position="top", stroke_width=0, shadow_enabled=False), after=rows()
    )
    await finish(j, job, identity)
    record = (
        await j.db.scalars(
            select(CreatorStyleLearningProject).where(CreatorStyleLearningProject.user_id == j.uid)
        )
    ).one()
    assert record.finished_changes == {}
    for _ in range(3):
        job = await project(j)
        await stage(j, job, temporary=True)
        # Returning from a one-off look is still not evidence against it.
        # A later ordinary Save must not erase the earlier privacy scope.
        identity = await stage(
            j,
            job,
            before=rows(position="top", stroke_width=0, shadow_enabled=False),
            after=rows(position="middle"),
        )
        await finish(j, job, identity)
    assert await suggestions(j) == []
    job = await project(j)
    current = await stage(j, job)
    wrong = copy.copy(current)
    wrong.recipe_digest = "b" * 64
    await finish(j, job, wrong)
    assert await suggestions(j) == []


@pytest.mark.asyncio
async def test_latest_contradiction_retracts_and_old_votes_expire(journey):
    j = journey
    await opt_in(j)
    for _ in range(3):
        job = await project(j)
        identity = await stage(j, job, after=rows(position="top"))
        await finish(j, job, identity)
    assert len(await suggestions(j)) == 1
    job = await project(j)
    identity = await stage(j, job, after=rows(position="middle"))
    await finish(j, job, identity)
    assert await suggestions(j) == []
    for _ in range(5):
        job = await project(j)
        identity = await stage(j, job, after=rows())
        await finish(j, job, identity)
    assert await suggestions(j) == []


@pytest.mark.asyncio
async def test_dismiss_suppresses_until_reset_disable_retains_approved(journey):
    j = journey
    await opt_in(j)
    service = CreatorDirectionService()
    for _ in range(3):
        job = await project(j)
        identity = await stage(j, job, after=rows(position="top"))
        await finish(j, job, identity)
    item = (await suggestions(j))[0]
    user = await j.db.get(User, j.uid)
    await service.set_item_state(
        j.db,
        j.uid,
        item.id,
        state="dismissed",
        expected_revision=user.creator_memory_revision,
        idempotency_key="dismiss",
    )
    await j.db.commit()
    job = await project(j)
    identity = await stage(j, job, after=rows(position="top"))
    await finish(j, job, identity)
    assert await suggestions(j) == []
    user = await j.db.get(User, j.uid)
    await reset_learning_history(j.db, j.uid, user.creator_memory_revision, "reset")
    await j.db.commit()
    for _ in range(3):
        job = await project(j)
        identity = await stage(j, job, after=rows(position="top"))
        await finish(j, job, identity)
    item = (await suggestions(j))[0]
    user = await j.db.get(User, j.uid)
    await service.set_item_state(
        j.db,
        j.uid,
        item.id,
        state="active",
        required_state="suggested",
        expected_revision=user.creator_memory_revision,
        idempotency_key="accept-again",
    )
    await j.db.commit()
    user = await j.db.get(User, j.uid)
    await set_learning_enabled(j.db, j.uid, False, user.creator_memory_revision, "disable")
    await j.db.commit()
    assert not (
        await j.db.scalars(
            select(CreatorStyleLearningProject).where(CreatorStyleLearningProject.user_id == j.uid)
        )
    ).all()
    assert (await CreatorDirectionResolver().snapshot(j.db, j.uid)).typed_overrides[
        "text_style_title"
    ]["position"] == "top"


@pytest.mark.asyncio
async def test_default_off_gates_and_account_delete(journey, monkeypatch):
    j = journey
    job = await project(j)
    identity = await stage(j, job)
    await finish(j, job, identity)
    assert not (
        await j.db.scalars(
            select(CreatorStyleLearningProject).where(CreatorStyleLearningProject.user_id == j.uid)
        )
    ).all()
    await opt_in(j)
    monkeypatch.setattr(settings, "phone_text_styling_enabled", False)
    identity = await stage(j, job)
    await finish(j, job, identity)
    assert not (
        await j.db.scalars(
            select(CreatorStyleLearningProject).where(CreatorStyleLearningProject.user_id == j.uid)
        )
    ).all()
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(settings, "creator_edit_learning_privacy_approved", False)
    identity = await stage(j, job)
    await finish(j, job, identity)
    assert not (
        await j.db.scalars(
            select(CreatorStyleLearningProject).where(CreatorStyleLearningProject.user_id == j.uid)
        )
    ).all()
    monkeypatch.setattr(settings, "creator_edit_learning_privacy_approved", True)
    identity = await stage(j, job)
    await finish(j, job, identity)
    assert (
        await j.db.scalars(
            select(CreatorStyleLearningProject).where(CreatorStyleLearningProject.user_id == j.uid)
        )
    ).all()
    await j.db.execute(
        update(PlanItem)
        .where(
            PlanItem.content_plan_id.in_(select(ContentPlan.id).where(ContentPlan.user_id == j.uid))
        )
        .values(current_job_id=None)
    )
    await j.db.execute(delete(Job).where(Job.user_id == j.uid))
    await j.db.execute(delete(User).where(User.id == j.uid))
    await j.db.commit()
    assert not (
        await j.db.scalars(
            select(CreatorStyleLearningProject).where(CreatorStyleLearningProject.user_id == j.uid)
        )
    ).all()


@pytest.mark.asyncio
async def test_real_phone_save_verified_mp4_publication_memory_accept_and_owner_export(
    journey, monkeypatch, tmp_path
):
    """Actual Save compiler, HTTP publication byte probe, memory operations/export.

    The MP4 checks server acceptance; native pixel/preview parity has its own
    XCTest proof. Storage transport alone is local; ffprobe/checksums are real.
    """
    import hashlib
    import json
    import shutil
    import subprocess
    from datetime import UTC, datetime, timedelta

    import httpx

    from app.auth import get_current_user
    from app.database import get_db
    from app.kria.device_render import make_device_request
    from app.main import app
    from app.models import TemporaryMediaUpload
    from app.routes import device_render as routes
    from app.services.device_render import (
        device_record,
        device_status,
        pin_device_request,
        save_device_record,
    )
    from app.services.finished_style_learning import stage_prepared_style_edit
    from tests.routes.test_phone_editor_commit import phone_job, save

    j = journey
    await opt_in(j)
    final = tmp_path / "verified-final.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=1080x1920:r=30:d=3",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(final),
        ],
        check=True,
    )
    data = final.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(routes.limiter, "enabled", False)
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda path: SimpleNamespace(size=len(data), content_type="video/mp4", generation="42"),
    )
    monkeypatch.setattr(
        routes.storage,
        "download_generation_to_file",
        lambda path, local, **kwargs: shutil.copyfile(final, local),
    )
    monkeypatch.setattr(
        routes.storage, "signed_get_url", lambda *a, **k: "https://storage.example/fixture"
    )
    monkeypatch.setattr(routes, "upload_video_poster", lambda *a, **k: None)

    async def current_user():
        return await j.db.get(User, j.uid)

    app.dependency_overrides[get_current_user] = current_user

    async def database():
        yield j.db

    app.dependency_overrides[get_db] = database
    evidence = []
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for _ in range(3):
                job = await project(j)
                seed = phone_job(monkeypatch)
                job.assembly_plan = copy.deepcopy(seed.assembly_plan)
                old = device_status(seed, "guided_story").request
                pin_device_request(
                    job,
                    make_device_request(
                        job_id=job.id, variant_id="guided_story", revision=2, recipe=old.recipe
                    ),
                    base_generation="first",
                )
                await j.db.commit()
                prep = save(
                    job,
                    position="top",
                    stroke_width=0,
                    shadow_enabled=False,
                    font_family="Inter Medium",
                )
                await stage_prepared_style_edit(j.db, j.uid, job, "guided_story", prep)
                status = device_status(job, "guided_story")
                attempt = str(uuid.uuid4())
                path = f"{j.uid}/{job.id}/device/{attempt}.mp4"
                record = device_record(job, "guided_story")
                record["status"]["phase"] = "syncing"
                record["attempts"][attempt] = {"path": path, "size": len(data), "sha256": "0" * 64}
                save_device_record(job, "guided_story", record)
                j.db.add(
                    TemporaryMediaUpload(
                        user_id=j.uid,
                        object_path=path,
                        purpose="device_export",
                        retention_expires_at=datetime.now(UTC) + timedelta(hours=1),
                    )
                )
                await j.db.commit()
                payload = {
                    "identity": status.request.identity.model_dump(mode="json"),
                    "attempt_id": attempt,
                }
                job_id = job.id
                refused = await client.post(
                    f"/me/jobs/{job.id}/device-render/complete", json=payload
                )
                assert refused.status_code == 422, refused.text
                job = await j.db.get(Job, job_id)
                record = device_record(job, "guided_story")
                record["attempts"][attempt]["sha256"] = digest
                save_device_record(job, "guided_story", record)
                await j.db.commit()
                response = await client.post(
                    f"/me/jobs/{job.id}/device-render/complete", json=payload
                )
                assert response.status_code == 200, response.text
                response = await client.post(
                    f"/me/jobs/{job.id}/device-render/complete", json=payload
                )
                assert response.status_code == 200, response.text
                evidence.append(
                    {
                        "job_id": str(job.id),
                        "recipe_digest": status.request.identity.recipe_digest,
                        "checksum": digest,
                        "duplicate_export": response.status_code,
                    }
                )
            document = (await client.get("/me/memory")).json()
            target = next(
                i
                for i in document["suggestions"]
                if "favorite_fonts" in i["structured_value"]["text_style_title"]
            )
            assert target["evidence"]["supporting_video_count"] == 3
            assert document["active"] == []
            accepted = await client.post(
                f"/me/memory/items/{target['id']}/accept",
                json={"expected_revision": document["revision"], "idempotency_key": "http-accept"},
            )
            assert accepted.status_code == 200, accepted.text
            exported = await client.get("/me/export")
            assert exported.status_code == 200, exported.text
            learning = exported.json()["creator_memory"]["edit_learning"]
            assert len(learning["projects"]) == 3 and learning["enabled"]
            assert all(p["published_recipe_digest"] for p in learning["projects"])
            assert "Before" not in json.dumps(learning) and "After" not in json.dumps(learning)
            (tmp_path / "verified-learning-report.json").write_text(
                json.dumps(
                    {
                        "projects": evidence,
                        "suggestion": target,
                        "approved_preferences": (await client.get("/me/memory")).json()["active"],
                        "export_version": learning["evidence_version"],
                    },
                    indent=2,
                )
            )
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_font_learning_exclusion_needs_different_replacements_and_project_erasure(journey):
    from app.services.finished_style_learning import erase_project_learning

    j = journey
    await opt_in(j)
    jobs = []
    for font in ["Inter Medium", "Inter Medium", "Montserrat Bold"]:
        job = await project(j)
        identity = await stage(j, job, after=rows(font_family=font))
        await finish(j, job, identity)
        jobs.append(job)
    items = await suggestions(j)
    assert len(items) == 1
    assert items[0].structured_value == {"text_style_title": {"excluded_fonts": ["Inter"]}}
    removed_project = jobs[0].content_plan_item_id
    await erase_project_learning(j.db, j.uid, removed_project)
    await j.db.commit()
    assert await suggestions(j) == []
    item = await j.db.get(CreatorMemoryItem, items[0].id)
    assert str(removed_project) not in item.evidence["project_ids"]


@pytest.mark.asyncio
async def test_loaded_owner_cannot_collect_after_another_session_revokes_consent(journey):
    j = journey
    await opt_in(j)
    job = await project(j)
    engine = create_async_engine(settings.asyncpg_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        owner = await j.db.get(User, j.uid)
        assert owner.creator_edit_learning_enabled
        await j.db.commit()
        async with factory() as other:
            await set_learning_enabled(
                other, j.uid, False, owner.creator_memory_revision, "other-device-disable"
            )
            await other.commit()
        identity = await stage(j, job)
        await finish(j, job, identity)
        assert not (
            await j.db.scalars(
                select(CreatorStyleLearningProject).where(
                    CreatorStyleLearningProject.user_id == j.uid
                )
            )
        ).all()
    finally:
        await engine.dispose()
