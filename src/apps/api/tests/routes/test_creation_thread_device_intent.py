"""KRI-121 round 2: the iPhone app marks its projects "render on this device"."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

import app.routes.creation_threads as routes
from app.auth import NativeClient, get_current_user, is_native_client
from app.config import settings
from app.services.phone_destination import DEVICE_INTENT_KEY

USER_ID = uuid.UUID("0b6b1c52-3f0e-4c7a-9d51-6f1e2a3b4c5d")
ITEM_ID = uuid.UUID("5a1f0c3e-8b7d-4e2a-9c6f-1d3b5e7a9c0b")
INTERNAL = {"Authorization": "Bearer internal-key", "X-User-Id": str(USER_ID)}
MOBILE = {"Authorization": "Bearer mobile-access-token"}


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_key", "internal-key")
    monkeypatch.setattr(settings, "mobile_jwt_secret", "mobile-secret")


@pytest.fixture
def pilot(monkeypatch):
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [USER_ID])
    monkeypatch.setattr(settings, "phone_render_verified_features", ["stillImages"])


def test_the_dependency_reports_which_branch_authenticated(auth):
    probe = FastAPI()

    @probe.get("/probe")
    async def read(native_client: NativeClient = False) -> dict:
        return {"native": native_client}

    async def current_user() -> object:
        return SimpleNamespace(id=USER_ID)

    probe.dependency_overrides[get_current_user] = current_user
    client = TestClient(probe)
    assert client.get("/probe", headers=MOBILE).json() == {"native": True}
    assert client.get("/probe", headers=INTERNAL).json() == {"native": False}
    # The web proxy's key never counts as native, with or without a user id.
    assert client.get("/probe", headers={"Authorization": INTERNAL["Authorization"]}).json() == {
        "native": False
    }


def test_the_dependency_never_answers_for_an_unauthenticated_request(auth):
    probe = FastAPI()

    @probe.get("/probe")
    async def read(native_client: NativeClient = False) -> dict:
        return {"native": native_client}

    # No override: the real get_current_user runs first and rejects.
    assert TestClient(probe).get("/probe").status_code == 401


@pytest.mark.asyncio
async def test_a_mobile_token_is_native_only_while_mobile_auth_is_configured(auth, monkeypatch):
    user = SimpleNamespace(id=USER_ID)
    assert await is_native_client(user, None, MOBILE["Authorization"])
    assert not await is_native_client(user, str(USER_ID), MOBILE["Authorization"])
    monkeypatch.setattr(settings, "mobile_jwt_secret", "")
    assert not await is_native_client(user, None, MOBILE["Authorization"])


@pytest.mark.parametrize("path", ["", "/{thread_id}/messages", "/{thread_id}/actions"])
def test_stamping_routes_resolve_the_dependency(path):
    route = next(
        r for r in routes.router.routes if r.path == path and "POST" in (r.methods or set())
    )
    assert is_native_client in {dep.call for dep in route.dependant.dependencies}


def _creation(monkeypatch):
    user = SimpleNamespace(id=USER_ID, email="creator@example.com")
    db = Mock()
    db.get = AsyncMock(return_value=user)
    db.add = Mock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    plan = SimpleNamespace(id=uuid.uuid4(), ownership_epoch=0)
    item = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(routes, "_project", AsyncMock(return_value=(plan, item)))
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(
        "app.services.creator_direction_snapshot.resolve_snapshot_for_dispatch",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.creator_direction_snapshot.serialize_private_snapshot",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        "app.services.creator_direction_receipts.stamp_private_receipt",
        lambda *args, **kwargs: {},
    )
    return user, db


async def _create(user, db, **kwargs):
    await routes.create_thread(
        request=Request({"type": "http", "method": "POST", "path": "/creation-threads"}),
        body=routes.CreateBody(),
        user=user,
        db=db,
        **kwargs,
    )
    return db.add.call_args.args[0]


@pytest.mark.asyncio
async def test_a_native_pilot_thread_is_stamped_at_creation(pilot, monkeypatch):
    user, db = _creation(monkeypatch)
    thread = await _create(user, db, native_client=True)
    assert thread.state == {"media": [], "media_count": 0, DEVICE_INTENT_KEY: "device"}


@pytest.mark.asyncio
@pytest.mark.parametrize("enrolled", [False, True])
async def test_web_threads_and_accounts_outside_the_pilot_are_never_stamped(
    pilot, monkeypatch, enrolled
):
    user, db = _creation(monkeypatch)
    if not enrolled:
        monkeypatch.setattr(settings, "phone_render_user_ids", [uuid.uuid4()])
    # Web for the enrolled account, the native app for the outsider.
    thread = await _create(user, db, native_client=not enrolled)
    assert thread.state == {"media": [], "media_count": 0}
    # Callers that predate the dependency (and every direct call) read as web.
    assert (await _create(user, db)).state == {"media": [], "media_count": 0}


def test_existing_drafts_are_stamped_the_next_time_the_app_touches_them(pilot):
    user = SimpleNamespace(id=USER_ID)
    thread = SimpleNamespace(state={"edit_format": "montage", "media_count": 0})
    routes._stamp_device_intent(thread, user, native_client=False)
    assert DEVICE_INTENT_KEY not in thread.state
    routes._stamp_device_intent(thread, user, native_client=True)
    assert thread.state == {
        "edit_format": "montage",
        "media_count": 0,
        DEVICE_INTENT_KEY: "device",
    }
    stamped = thread.state
    routes._stamp_device_intent(thread, user, native_client=True)
    assert thread.state is stamped


def _message_fixture(monkeypatch, state):
    user = SimpleNamespace(id=USER_ID)
    item = SimpleNamespace(
        id=uuid.uuid4(),
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        clip_gcs_paths=[],
        clip_assignments=[],
    )
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=0,
        runtime_version=1,
        title="Photos",
        active_plan_item_id=item.id,
        active_job_id=None,
        active_creator_agent_session_id=None,
        state=state,
    )
    db = Mock()
    db.get = AsyncMock(return_value=item)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    append = AsyncMock()
    agent = AsyncMock(return_value=thread)
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_append", append)
    monkeypatch.setattr(routes, "_agent_message", agent)
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    return user, thread, db, append, agent


async def _send(user, thread, db, **kwargs):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": (f"test-{uuid.uuid4()}", 0),
        }
    )
    body = routes.MessageBody(
        message="Make a montage of these photos",
        client_event_id=f"message-{uuid.uuid4()}",
        expected_revision=0,
    )
    return await routes.message_thread(request, str(thread.id), body, user, db, **kwargs)


@pytest.mark.asyncio
async def test_a_visuals_only_pilot_project_reaches_the_agent_without_footage(pilot, monkeypatch):
    user, thread, db, append, agent = _message_fixture(
        monkeypatch, {"edit_format": "montage", "media": [], "media_count": 0}
    )
    visuals = AsyncMock(return_value=True)
    monkeypatch.setattr(routes, "item_visuals_only_on_device", visuals)
    await _send(user, thread, db, native_client=True)
    # The draft predates the stamp: this message stamps it, and the same
    # request already sees its Visuals as the project's sources.
    assert thread.state[DEVICE_INTENT_KEY] == "device"
    assert visuals.await_args.kwargs["thread_state"][DEVICE_INTENT_KEY] == "device"
    assert "media_prompt" not in [call.kwargs["event_type"] for call in append.await_args_list]
    agent.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("native_client", [False, True])
async def test_everyone_else_is_still_asked_for_footage(pilot, monkeypatch, native_client):
    user, thread, db, append, agent = _message_fixture(
        monkeypatch, {"edit_format": "montage", "media": [], "media_count": 0}
    )
    # The web never stamps; the app's project has no Visual the phone can draw.
    visuals = AsyncMock(return_value=False)
    monkeypatch.setattr(routes, "item_visuals_only_on_device", visuals)
    await _send(user, thread, db, native_client=native_client)
    assert "media_prompt" in [call.kwargs["event_type"] for call in append.await_args_list]
    assert visuals.await_count == (1 if native_client else 0)
    agent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_enabled", [False, True])
@pytest.mark.parametrize("enrolled", [False, True])
async def test_pilot_accounts_are_only_offered_the_runtime_that_renders_on_device(
    pilot, monkeypatch, runtime_enabled, enrolled
):
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", runtime_enabled)
    user = SimpleNamespace(id=USER_ID if enrolled else uuid.uuid4())
    manifest = await routes.capabilities(user)
    assert manifest["phone_rendering"]["enabled"] is enrolled
    assert manifest["runtime_versions"] == ([1, 2] if runtime_enabled and not enrolled else [1])
    # The web keeps every format, pilot or not.
    assert [entry["id"] for entry in manifest["formats"]] == list(routes._available_formats())


@pytest.mark.asyncio
@pytest.mark.parametrize("enrolled", [False, True])
async def test_the_app_on_a_pilot_account_only_offers_formats_the_iphone_renders(
    pilot, monkeypatch, enrolled
):
    """KRI-132 follow-up: `phone_subtitled_rendering_enabled` /
    `phone_narrated_rendering_enabled` default on, so once the underlying
    formats are enabled at all (`subtitled_archetype_enabled` /
    `narrated_archetype_enabled`), the picker offers Talking to camera too --
    not just Montage. Narrated still doesn't appear here: the `pilot` fixture
    only verifies `stillImages`, not `narrationAudio`, so
    `phone_render_supported_formats()` still excludes the narrated family
    (same rollout-flag gate the montage-family voiceover render already
    uses). See `test_the_app_on_a_pilot_account_offers_only_montage_when_
    rollout_flags_are_off` for the byte-identical-to-pre-rollout case.
    `slides` survives regardless (KRI-118 item 1: it's exempt from the
    phone-supported-formats filter entirely -- cloud-only render, no phone
    dispatch path)."""
    monkeypatch.setattr(settings, "narrated_archetype_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    user = SimpleNamespace(id=USER_ID if enrolled else uuid.uuid4())
    offered = [
        entry["id"] for entry in (await routes.capabilities(user, native_client=True))["formats"]
    ]
    everything = list(routes._available_formats())
    assert {"montage", "narrated", "talking_to_camera", "slides"} <= set(everything)
    # Narrated still has no verified narrationAudio in this fixture -- only
    # Montage, Talking to camera, and Slides are actually phone-renderable here.
    assert offered == (["montage", "talking_to_camera", "slides"] if enrolled else everything)


@pytest.mark.asyncio
async def test_the_app_on_a_pilot_account_offers_only_montage_when_rollout_flags_are_off(
    pilot, monkeypatch
):
    """Byte-identical to pre-KRI-132 for the ROLLOUT-gated formats: with the
    new rollout flags off (and no verified narrationAudio), an enrolled phone
    account's picker still shows only Montage among them, even though
    Talking to camera/Narrated are generally enabled formats. `slides` still
    shows too -- KRI-118 item 1: it's exempt from this filter entirely."""
    monkeypatch.setattr(settings, "narrated_archetype_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", False)
    monkeypatch.setattr(settings, "phone_narrated_rendering_enabled", False)
    user = SimpleNamespace(id=USER_ID)
    offered = [
        entry["id"] for entry in (await routes.capabilities(user, native_client=True))["formats"]
    ]
    assert offered == ["montage", "slides"]


# --- "Continue with N visuals" reads what the iPhone can render from ----------


def _pool_row(**changes) -> dict:
    return {
        "id": uuid.uuid4(),
        "plan_item_id": ITEM_ID,
        "user_id": USER_ID,
        "status": "ready",
        "kind": "image",
        "deduplicated_to_asset_id": None,
    } | changes


@pytest_asyncio.fixture
async def pool_db():
    """Only the columns the count reads: the real table uses Postgres-only types."""

    metadata = sa.MetaData()
    table = sa.Table(
        "plan_item_assets",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("plan_item_id", sa.Uuid),
        sa.Column("user_id", sa.Uuid),
        sa.Column("status", sa.Text),
        sa.Column("kind", sa.Text),
        sa.Column("deduplicated_to_asset_id", sa.Uuid, nullable=True),
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.execute(
            table.insert(),
            [
                _pool_row(),
                _pool_row(),
                _pool_row(kind="video"),
                # None of these gives the phone anything to bind: not ready yet,
                # never will be, a duplicate, or another creator's or item's row.
                *(_pool_row(status=status) for status in ("analyzing", "queued", "failed")),
                _pool_row(status="preparing"),
                _pool_row(deduplicated_to_asset_id=uuid.uuid4()),
                _pool_row(user_id=uuid.uuid4()),
                _pool_row(plan_item_id=uuid.uuid4()),
            ],
        )
    async with async_sessionmaker(engine)() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verified_features", "expected"),
    [(["stillImages"], 2), (["visualVideos"], 1), (["stillImages", "visualVideos"], 3), ([], 0)],
    ids=["photos", "pool_videos", "both", "nothing_drawable"],
)
async def test_device_ready_counts_only_ready_visuals_the_iphone_draws(
    pilot, monkeypatch, pool_db, verified_features, expected
):
    monkeypatch.setattr(settings, "phone_render_verified_features", verified_features)
    assert await routes._device_ready_visual_count(pool_db, ITEM_ID, USER_ID) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["outside_the_cohort", "kill_switch"])
async def test_device_ready_is_zero_for_an_account_that_does_not_render_on_the_iphone(
    pilot, monkeypatch, pool_db, case
):
    if case == "kill_switch":
        monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    else:
        monkeypatch.setattr(settings, "phone_render_user_ids", [uuid.uuid4()])
    assert await routes._device_ready_visual_count(pool_db, ITEM_ID, USER_ID) == 0


def test_the_projection_carries_device_ready_beside_the_quota_count():
    item = SimpleNamespace(edit_format="montage", voiceover_gcs_path=None)
    visuals = routes._media_capabilities(
        item=item, clip_count=0, visual_count=9, device_ready_visual_count=2
    )["visuals"]
    # The quota count is unchanged; the app gates "Continue" on device_ready.
    assert (visuals["current"], visuals["device_ready"]) == (9, 2)
    legacy_caller = routes._media_capabilities(item=item, clip_count=0, visual_count=9)
    assert legacy_caller["visuals"]["device_ready"] == 0
