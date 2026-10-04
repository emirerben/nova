"""KRI-374 lane B: attaching, removing and advertising a creator-uploaded song."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

import app.routes.creation_threads as routes
from app.config import settings
from app.routes.creation_threads import (
    AttachBody,
    MediaInput,
    UploadBody,
    UploadFile,
    _media_path,
    attach_media,
    capabilities,
)

PHONE_FEATURES = ["basicComposition", "musicBed", "audioMix"]


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": (f"test-{uuid.uuid4()}", 0),
        }
    )


@pytest.fixture(autouse=True)
def _song_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "ios_device_only_mode", False)
    monkeypatch.setattr(settings, "phone_render_verified_features", PHONE_FEATURES)
    monkeypatch.setattr(settings, "user_song_montage_enabled", True)
    monkeypatch.setattr(settings, "user_song_max_duration_s", 600.0)


# ── request validation ───────────────────────────────────────────────────────


def test_role_applies_to_audio_only() -> None:
    with pytest.raises(ValidationError, match="audio only"):
        MediaInput(media_id="clip-1.mp4", kind="video", role="song")
    assert MediaInput(media_id="s-1.m4a", kind="audio", role="song").role == "song"
    assert MediaInput(media_id="v-1.m4a", kind="audio").role is None


def test_batch_allows_one_voiceover_plus_one_song_but_not_two_of_a_role() -> None:
    AttachBody(
        media=[
            MediaInput(media_id="v-1.m4a", kind="audio"),
            MediaInput(media_id="s-1.m4a", kind="audio", role="song"),
        ],
        client_event_id="e1",
        expected_revision=0,
    )
    with pytest.raises(ValueError, match="only one song"):
        AttachBody(
            media=[
                MediaInput(media_id="s-1.m4a", kind="audio", role="song"),
                MediaInput(media_id="s-2.m4a", kind="audio", role="song"),
            ],
            client_event_id="e2",
            expected_revision=0,
        )
    with pytest.raises(ValueError, match="only one voiceover"):
        AttachBody(
            media=[
                MediaInput(media_id="v-1.m4a", kind="audio"),
                MediaInput(media_id="v-2.m4a", kind="audio", role="voiceover"),
            ],
            client_event_id="e3",
            expected_revision=0,
        )


def test_upload_file_accepts_an_optional_role() -> None:
    upload = UploadFile(
        filename="song.m4a",
        content_type="audio/mp4",
        file_size_bytes=10,
        client_upload_id="song-1",
        role="song",
    )
    assert UploadBody(files=[upload]).files[0].role == "song"


# ── attach ───────────────────────────────────────────────────────────────────


def _item(**extra):  # noqa: ANN003, ANN202
    base = dict(
        clip_gcs_paths=[],
        clip_assignments=[],
        voiceover_gcs_path=None,
        song_gcs_path=None,
        song_generation=None,
        song_duration_s=None,
        song_filename=None,
        song_analysis=None,
        song_alignment=None,
        audio_mode="kria",
        edit_format="montage",
        current_job_id=None,
        edit_proposal=None,
    )
    base.update(extra)
    return SimpleNamespace(**base)


async def _attach(
    monkeypatch: pytest.MonkeyPatch,
    media: list[MediaInput],
    *,
    item: SimpleNamespace | None = None,
    duration_s: float = 30.0,
    content_type: str = "audio/mp4",
    enqueued: list | None = None,
):
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=0,
        active_job_id=None,
        active_creator_agent_session_id=None,
        active_plan_item_id=uuid.uuid4(),
        state={"media": [], "media_count": 0},
    )
    item = item or _item()
    item.id = thread.active_plan_item_id
    events = enqueued if enqueued is not None else []
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda _path: SimpleNamespace(size=100, content_type=content_type, generation="1234567"),
    )
    monkeypatch.setattr(
        routes, "_probe_registered_media", AsyncMock(return_value=(duration_s, True))
    )
    monkeypatch.setattr(
        "app.tasks.user_song.enqueue_user_song_analysis",
        lambda item_id, generation: events.append(("analysis", str(item_id), generation)),
    )
    monkeypatch.setattr(
        "app.tasks.user_song.enqueue_user_song_alignment",
        lambda item_id: events.append(("alignment", str(item_id))),
    )
    monkeypatch.setattr(
        "app.tasks.kria_clip_understanding.enqueue_clip_understanding", lambda _item_id: None
    )
    db = Mock()
    db.get = AsyncMock(return_value=item)
    db.execute = AsyncMock()

    async def commit() -> None:
        events.append(("commit",))

    db.commit = AsyncMock(side_effect=commit)
    db.refresh = AsyncMock()
    await attach_media(
        _request(),
        str(thread.id),
        AttachBody(media=media, client_event_id="attach-song", expected_revision=0),
        user,
        db,
    )
    return user, thread, item, events


@pytest.mark.asyncio
async def test_attach_song_fills_song_columns_not_voiceover(monkeypatch) -> None:
    user, thread, item, events = await _attach(
        monkeypatch,
        [MediaInput(media_id="song-1.m4a", kind="audio", role="song", filename="track.m4a")],
    )

    assert item.song_gcs_path == _media_path(user.id, thread.id, "song-1.m4a")
    assert item.song_generation == 1234567
    assert item.song_duration_s == 30.0
    assert item.song_filename == "track.m4a"
    assert item.audio_mode == "song"
    # Voiceover routing must never fire for a song.
    assert item.voiceover_gcs_path is None
    assert not hasattr(item, "voiceover_generation")
    # Planner can see "analysing" straight away.
    assert item.song_analysis["status"] == "pending"
    assert item.song_analysis["generation"] == 1234567
    # The thread projection names the media as a song and hides the storage path.
    public = thread.state["media"][0]
    assert public["role"] == "song"
    assert "_path" not in public and "gcs_path" not in public
    # Enqueue strictly AFTER the commit (dispatch enqueue-before-commit race).
    assert events == [("commit",), ("analysis", str(item.id), 1234567)]


@pytest.mark.asyncio
async def test_second_song_is_409_song_exists(monkeypatch) -> None:
    item = _item(song_gcs_path="users/u/creation-threads/t/old.m4a", song_generation=1)
    with pytest.raises(HTTPException) as exc:
        await _attach(
            monkeypatch,
            [MediaInput(media_id="song-2.m4a", kind="audio", role="song")],
            item=item,
        )
    assert (exc.value.status_code, exc.value.detail) == (409, "song_exists")
    assert item.song_gcs_path == "users/u/creation-threads/t/old.m4a"


@pytest.mark.asyncio
async def test_song_longer_than_the_cap_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(settings, "user_song_max_duration_s", 20.0)
    item = _item()
    with pytest.raises(HTTPException) as exc:
        await _attach(
            monkeypatch,
            [MediaInput(media_id="song-1.m4a", kind="audio", role="song")],
            item=item,
            duration_s=21.0,
        )
    assert exc.value.status_code == 422
    assert item.song_gcs_path is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "off",
    ["flag", "phone", "features"],
)
async def test_song_attach_is_refused_when_unavailable(monkeypatch, off) -> None:
    if off == "flag":
        monkeypatch.setattr(settings, "user_song_montage_enabled", False)
    elif off == "phone":
        monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    else:
        monkeypatch.setattr(settings, "phone_render_verified_features", ["basicComposition"])
    item = _item()
    with pytest.raises(HTTPException) as exc:
        await _attach(
            monkeypatch,
            [MediaInput(media_id="song-1.m4a", kind="audio", role="song")],
            item=item,
        )
    assert exc.value.status_code == 404
    assert item.song_gcs_path is None


@pytest.mark.asyncio
async def test_audio_without_a_role_is_still_the_voiceover(monkeypatch) -> None:
    flag_off = False
    monkeypatch.setattr(settings, "user_song_montage_enabled", flag_off)  # irrelevant to voiceover
    user, thread, item, events = await _attach(
        monkeypatch,
        [MediaInput(media_id="voice-1.webm", kind="audio", filename="voice.webm")],
        content_type="audio/webm",
    )

    assert item.voiceover_gcs_path == _media_path(user.id, thread.id, "voice-1.webm")
    assert item.audio_mode == "voiceover"
    assert item.song_gcs_path is None
    assert "role" not in thread.state["media"][0]
    assert events == [("commit",)]


@pytest.mark.asyncio
async def test_a_clip_attached_while_a_song_exists_requests_alignment(monkeypatch) -> None:
    item = _item(
        song_gcs_path="users/u/creation-threads/t/song.m4a",
        song_generation=5,
        audio_mode="song",
    )
    _user, _thread, item, events = await _attach(
        monkeypatch,
        [MediaInput(media_id="take-1.mp4", kind="video")],
        item=item,
        content_type="video/mp4",
    )

    assert item.audio_mode == "song"
    assert events == [("commit",), ("alignment", str(item.id))]


# ── removal ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_removing_the_song_clears_columns_and_returns_to_kria(monkeypatch) -> None:
    user_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    song_path = _media_path(user_id, thread_id, "song-1.m4a")
    item = _item(
        id=uuid.uuid4(),
        song_gcs_path=song_path,
        song_generation=99,
        song_duration_s=30.0,
        song_filename="track.m4a",
        song_analysis={"status": "ready"},
        song_alignment={"takes": {}},
        audio_mode="song",
    )
    thread = SimpleNamespace(
        id=thread_id,
        creator_id=user_id,
        status="active",
        revision=4,
        active_job_id=None,
        active_creator_agent_session_id=None,
        active_plan_item_id=item.id,
        state={
            "media": [{"media_id": "song-1.m4a", "kind": "audio", "role": "song"}],
            "media_count": 1,
        },
    )
    db = Mock()
    db.get = AsyncMock(return_value=item)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    deleted: list[str] = []

    async def to_thread(function, *args):  # noqa: ANN001, ANN202
        deleted.append(args[0] if args else function.__name__)

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_reject_input_mutation_while_rendering", AsyncMock())
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_ensure_speech_cleanup_preflight", AsyncMock(return_value=None))
    monkeypatch.setattr(routes.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.mutation_current_analysis_async",
        AsyncMock(return_value=None),
    )
    body = routes.ActionBody(
        action="remove_media",
        payload={"media_id": "song-1.m4a"},
        client_action_id="remove-song",
        expected_revision=4,
    )

    await routes.action_thread(_request(), str(thread.id), body, SimpleNamespace(id=user_id), db)

    assert item.song_gcs_path is None
    assert item.song_generation is None
    assert item.song_duration_s is None
    assert item.song_filename is None
    assert item.song_analysis is None
    assert item.song_alignment is None
    assert item.audio_mode == "kria"
    assert thread.state["media_count"] == 0
    # The object minted under this thread's exclusive prefix is deleted.
    assert song_path in deleted


# ── capabilities ─────────────────────────────────────────────────────────────


async def _caps(*, protocol: int | None = 2, user_id: uuid.UUID | None = None) -> dict:
    return await capabilities(
        SimpleNamespace(id=user_id or uuid.uuid4()),
        native_client=True,
        client_protocol=protocol,
    )


@pytest.mark.asyncio
async def test_song_capability_present_when_everything_holds() -> None:
    manifest = await _caps()
    song = manifest["media"]["song"]
    assert song["max"] == 1
    assert song["max_file_bytes"] == manifest["media"]["voiceover"]["max_file_bytes"]
    assert song["content_types"] == manifest["media"]["voiceover"]["content_types"]
    assert manifest["song_order_questions"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "off",
    ["flag", "phone", "features", "no_protocol", "old_protocol", "pilot_cohort"],
)
async def test_song_capability_absent_otherwise(monkeypatch, off) -> None:
    protocol: int | None = 2
    user_id = uuid.uuid4()
    if off == "flag":
        monkeypatch.setattr(settings, "user_song_montage_enabled", False)
    elif off == "phone":
        monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    elif off == "features":
        monkeypatch.setattr(settings, "phone_render_verified_features", ["musicBed"])
    elif off == "no_protocol":
        protocol = None
    elif off == "old_protocol":
        monkeypatch.setattr(settings, "kria_minimum_client_protocol", 5)
        protocol = 4
    else:
        monkeypatch.setattr(settings, "phone_render_user_ids", [uuid.uuid4()])

    manifest = await _caps(protocol=protocol, user_id=user_id)

    assert "song" not in manifest["media"]
    assert manifest["song_order_questions"] is False
    # Everything else on the manifest is untouched.
    assert manifest["media"]["voiceover"]["max"] == 1
