"""KRI-1117: the camera's background pick, written through the real route onto a real thread.

Failure modes: a pick that names another item's row (or another user's, or a made-up id) is
stored and later written into the plan; a closed gate or an old build can set one; the clear
path is gated too (a creator could never take a pick back); a replay of the same event stores
a second pick or bumps the revision twice; a stale revision overwrites a newer thread; the
manager's manifest never sees the stored pick.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.routes.creation_threads as routes
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.runtime import RuntimeFailure
from app.models import CreationThread, PlanItem, PlanItemAsset, User
from app.services.creator_capabilities import speaker_background_pick_from_state
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/",
            "headers": [],
            "client": (f"test-{uuid.uuid4()}", 0),
        }
    )


@pytest.fixture(autouse=True)
def _gate(monkeypatch):
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "speakerBackground"],
    )


def _seed_asset(
    item_id, user_id, *, kind="image", status="ready", client_upload_id=None
) -> uuid.UUID:
    asset_id = uuid.uuid4()
    with sync_session() as db:
        db.add(
            PlanItemAsset(
                id=asset_id,
                user_id=user_id,
                plan_item_id=item_id,
                kind=kind,
                gcs_path=f"users/{user_id}/plan/{item_id}/pool/{asset_id}.png",
                gcs_generation="42",
                status=status,
                client_upload_id=client_upload_id,
            )
        )
        db.commit()
    return asset_id


@pytest.fixture
def seeded():
    user_id, thread_id, _session_id = _seed_runtime_project()
    with sync_session() as db:
        item_id = db.get(CreationThread, thread_id).active_plan_item_id
    return SimpleNamespace(user_id=user_id, thread_id=thread_id, item_id=item_id)


async def _put(seeded, media_id, *, protocol=27, event=None, revision=None, client_upload_id=None):
    async with AsyncSessionLocal() as db:
        user = await db.get(User, seeded.user_id)
        thread = await db.get(CreationThread, seeded.thread_id)
        body = routes.SpeakerBackgroundBody(
            media_id=media_id,
            client_upload_id=client_upload_id,
            client_event_id=event or f"evt-{uuid.uuid4()}",
            expected_revision=thread.revision if revision is None else revision,
        )
        return await routes.set_speaker_background(
            _request(),
            str(seeded.thread_id),
            body,
            user,
            db,
            native_client=True,
            client_protocol=protocol,
        )


def _state(seeded) -> dict:
    with sync_session() as db:
        return dict(db.get(CreationThread, seeded.thread_id).state or {})


@pytest.mark.asyncio
async def test_a_pool_photo_of_this_item_becomes_the_pick(seeded):
    asset_id = _seed_asset(seeded.item_id, seeded.user_id)
    out = await _put(seeded, str(asset_id))

    assert _state(seeded)["speaker_background"] == {
        "media_id": f"asset-{asset_id}",
        "kind": "image",
    }
    assert speaker_background_pick_from_state(out.state) == f"asset-{asset_id}"


@pytest.mark.asyncio
async def test_the_manifests_spelling_is_accepted_and_a_video_keeps_its_kind(seeded):
    asset_id = _seed_asset(seeded.item_id, seeded.user_id, kind="video")
    await _put(seeded, f"asset-{asset_id}")
    assert _state(seeded)["speaker_background"] == {
        "media_id": f"asset-{asset_id}",
        "kind": "video",
    }


@pytest.mark.asyncio
async def test_the_stored_pick_is_handed_to_the_creator_manifest(seeded, monkeypatch):
    import sqlalchemy

    import app.services.creator_sessions as sessions
    from app.models import Persona

    asset_id = _seed_asset(seeded.item_id, seeded.user_id)
    await _put(seeded, str(asset_id))
    seen = {}
    real = sessions.resolve_creator_manifest

    def spy(**kwargs):
        seen["pick"] = kwargs.get("speaker_background_pick")
        return real(**kwargs)

    monkeypatch.setattr(sessions, "resolve_creator_manifest", spy)
    async with AsyncSessionLocal() as db:
        item = await db.get(PlanItem, seeded.item_id)
        persona = (
            await db.execute(sqlalchemy.select(Persona).where(Persona.user_id == seeded.user_id))
        ).scalar_one()
        await sessions.resolve_item_creator_context(db, item, persona=persona)
    assert seen["pick"] == f"asset-{asset_id}"

    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    async with AsyncSessionLocal() as db:
        item = await db.get(PlanItem, seeded.item_id)
        persona = (
            await db.execute(sqlalchemy.select(Persona).where(Persona.user_id == seeded.user_id))
        ).scalar_one()
        await sessions.resolve_item_creator_context(db, item, persona=persona)
    assert seen["pick"] is None


@pytest.mark.asyncio
async def test_a_row_of_another_item_or_user_is_refused_and_nothing_is_stored(seeded):
    other_user, other_thread, _ = _seed_runtime_project()
    with sync_session() as db:
        other_item = db.get(CreationThread, other_thread).active_plan_item_id
    foreign = _seed_asset(other_item, other_user)
    for media_id in (str(foreign), f"asset-{uuid.uuid4()}"):
        with pytest.raises(RuntimeFailure) as refused:
            await _put(seeded, media_id)
        assert (refused.value.status_code, refused.value.code) == (
            422,
            "speaker_background_media_not_in_pool",
        )
    assert "speaker_background" not in _state(seeded)


@pytest.mark.asyncio
async def test_a_failed_or_removed_row_is_not_a_pick(seeded):
    failed = _seed_asset(seeded.item_id, seeded.user_id, status="failed")
    with pytest.raises(RuntimeFailure) as refused:
        await _put(seeded, str(failed))
    assert refused.value.code == "speaker_background_media_not_in_pool"


@pytest.mark.asyncio
async def test_an_id_that_names_no_pool_row_is_refused(seeded):
    with pytest.raises(RuntimeFailure) as refused:
        await _put(seeded, "beach.jpg")
    assert (refused.value.status_code, refused.value.code) == (
        422,
        "speaker_background_media_invalid",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", ["flag", "token", "protocol"])
async def test_a_closed_gate_cannot_set_a_pick(seeded, monkeypatch, closed):
    asset_id = _seed_asset(seeded.item_id, seeded.user_id)
    protocol = 27
    if closed == "flag":
        monkeypatch.setattr(settings, "speaker_background_enabled", False)
    elif closed == "token":
        monkeypatch.setattr(
            settings,
            "phone_render_verified_features",
            [f for f in settings.phone_render_verified_features if f != "speakerBackground"],
        )
    else:
        protocol = 26
    with pytest.raises(RuntimeFailure) as refused:
        await _put(seeded, str(asset_id), protocol=protocol)
    assert (refused.value.status_code, refused.value.code) == (
        404,
        "speaker_background_unavailable",
    )
    assert "speaker_background" not in _state(seeded)


@pytest.mark.asyncio
async def test_a_pick_can_always_be_taken_back_even_with_the_gate_closed(seeded, monkeypatch):
    asset_id = _seed_asset(seeded.item_id, seeded.user_id)
    await _put(seeded, str(asset_id))
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    out = await _put(seeded, None, protocol=None)

    assert "speaker_background" not in _state(seeded)
    assert speaker_background_pick_from_state(out.state) is None


@pytest.mark.asyncio
async def test_replaying_the_same_event_changes_nothing_twice(seeded):
    asset_id = _seed_asset(seeded.item_id, seeded.user_id)
    first = await _put(seeded, str(asset_id), event="evt-fixed")
    again = await _put(seeded, str(asset_id), event="evt-fixed", revision=first.revision - 1)

    assert again.revision == first.revision
    other = _seed_asset(seeded.item_id, seeded.user_id)
    with pytest.raises(HTTPException) as reused:
        await _put(seeded, str(other), event="evt-fixed")
    assert reused.value.status_code == 409


@pytest.mark.asyncio
async def test_a_stale_revision_does_not_overwrite_a_newer_thread(seeded):
    asset_id = _seed_asset(seeded.item_id, seeded.user_id)
    with pytest.raises(HTTPException) as stale:
        await _put(seeded, str(asset_id), revision=0)
    assert stale.value.status_code == 409
    assert "speaker_background" not in _state(seeded)


@pytest.mark.asyncio
async def test_the_app_can_name_the_upload_it_reserved_while_it_is_still_in_flight(seeded):
    asset_id = _seed_asset(
        seeded.item_id, seeded.user_id, status="preparing", client_upload_id="ios-REC-1"
    )
    await _put(seeded, None, client_upload_id="ios-REC-1")
    assert _state(seeded)["speaker_background"] == {
        "media_id": f"asset-{asset_id}",
        "kind": "image",
    }
    # The reservation id works too, with the row still preparing.
    other = _seed_asset(seeded.item_id, seeded.user_id, status="preparing")
    await _put(seeded, str(other))
    assert _state(seeded)["speaker_background"]["media_id"] == f"asset-{other}"


@pytest.mark.asyncio
async def test_a_client_upload_id_of_another_item_names_nothing(seeded):
    other_user, other_thread, _ = _seed_runtime_project()
    with sync_session() as db:
        other_item = db.get(CreationThread, other_thread).active_plan_item_id
    _seed_asset(other_item, other_user, client_upload_id="ios-REC-2")
    with pytest.raises(RuntimeFailure) as refused:
        await _put(seeded, None, client_upload_id="ios-REC-2")
    assert refused.value.code == "speaker_background_media_not_in_pool"
    assert "speaker_background" not in _state(seeded)


def test_both_references_at_once_are_refused_at_validation():
    with pytest.raises(ValueError):
        routes.SpeakerBackgroundBody(
            media_id=str(uuid.uuid4()),
            client_upload_id="ios-x",
            client_event_id="e",
            expected_revision=0,
        )
