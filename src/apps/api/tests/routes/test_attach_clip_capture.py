"""KRI-189: the attach call accepts the phone's filming context and persists it.

Additive + lenient: a clip must never fail to register because a timestamp or a
place was missing or malformed, and a precise GPS fix must never be stored.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.requests import Request

import app.routes.creation_threads as routes
from app.kria.media_sources import MediaUploadContract
from app.routes.creation_threads import AttachBody, MediaInput, _media_path, attach_media
from app.services.clip_facts import assignment_facts, capture_from_assignment


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


def _contract() -> dict:
    return {
        "purpose": "analysis_proxy",
        "proxy": {
            "original": {
                "sha256": "a" * 64,
                "byte_count": 4000,
                "duration_s": 10,
                "width": 1080,
                "height": 1920,
                "has_audio": True,
            },
            "duration_s": 10,
            "width": 360,
            "height": 640,
            "frame_rate": 30,
        },
    }


async def _attach(monkeypatch: pytest.MonkeyPatch, media: MediaInput, *, proxy: bool):
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
    item = SimpleNamespace(
        id=thread.active_plan_item_id,
        clip_gcs_paths=[],
        clip_assignments=[],
        voiceover_gcs_path=None,
        audio_mode="kria",
        edit_proposal=None,
    )
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    append = AsyncMock()
    monkeypatch.setattr(routes, "_append", append)
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda _path: SimpleNamespace(size=100, content_type="video/mp4", generation="1"),
    )
    monkeypatch.setattr(routes, "_probe_registered_media", AsyncMock(return_value=(10.0, True)))
    db = Mock()
    db.get = AsyncMock(return_value=item)
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    if proxy:
        reservation = SimpleNamespace(
            media_id=media.media_id.strip(),
            upload_contract=_contract(),
            object_path=_media_path(user.id, thread.id, media.media_id.strip()),
        )
        result = Mock()
        result.scalars.return_value.all.return_value = [reservation]
        db.execute.return_value = result
    await attach_media(
        _request(),
        str(thread.id),
        AttachBody(media=[media], client_event_id="attach-capture", expected_revision=0),
        user,
        db,
    )
    return item, thread, append


def test_media_input_accepts_no_capture_fields() -> None:
    media = MediaInput(media_id="clip-1.mp4", kind="video")
    assert media.capture() is None


def test_media_input_rounds_location_to_two_decimals() -> None:
    media = MediaInput(
        media_id="clip-1.mp4",
        kind="video",
        coarse_location={"lat": 41.192345, "lon": 28.741199},
    )
    capture = media.capture()
    assert capture is not None
    assert (capture.coarse_location.lat, capture.coarse_location.lon) == (41.19, 28.74)


@pytest.mark.parametrize(
    "bad",
    [
        {"capture_time": "not-a-date"},
        {"coarse_location": {"lat": 999, "lon": 0}},
        {"coarse_location": {"lat": 1.0}},
        {"place": {"unexpected": "x"}},
    ],
)
def test_malformed_capture_field_is_dropped_not_rejected(bad: dict) -> None:
    media = MediaInput(media_id="clip-1.mp4", kind="video", **bad)
    assert media.capture() is None


def test_naive_capture_time_is_dropped() -> None:
    media = MediaInput(media_id="clip-1.mp4", kind="video", capture_time="2026-09-20T07:31:02")
    assert media.capture() is None


def test_audio_media_never_carries_capture() -> None:
    media = MediaInput(media_id="voice-1.webm", kind="audio", capture_time="2026-09-20T07:31:02Z")
    assert media.capture() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy", [False, True])
async def test_attach_persists_capture_on_the_assignment(
    monkeypatch: pytest.MonkeyPatch, proxy: bool
) -> None:
    media_id = "analysis-proxy-clip-1.mp4" if proxy else "clip-1.mp4"
    media = MediaInput(
        media_id=media_id,
        kind="video",
        filename="clip.mp4",
        capture_time="2026-09-20T07:31:02Z",
        coarse_location={"lat": 41.19234, "lon": 28.7411},
        place={"sub_locality": "Arnavutköy", "locality": "İstanbul", "country": "Türkiye"},
    )
    item, thread, append = await _attach(monkeypatch, media, proxy=proxy)

    assignment = item.clip_assignments[0]
    assert assignment["capture"] == {
        "capture_time": "2026-09-20T07:31:02Z",
        "coarse_location": {"lat": 41.19, "lon": 28.74},
        "place": {"sub_locality": "Arnavutköy", "locality": "İstanbul", "country": "Türkiye"},
    }
    facts = {(f.kind, f.provenance): f.value for f in assignment_facts(assignment)}
    assert facts[("capture_time", "exif")] == "2026-09-20T07:31:02Z"
    assert facts[("place", "geocode")] == "Arnavutköy, İstanbul, Türkiye"

    # Never echoed into the thread projection or the event payload.
    assert "capture" not in thread.state["media"][0]
    assert "_capture" not in thread.state["media"][0]
    assert "_capture" not in str(append.await_args.kwargs["payload"])

    if proxy:
        original = assignment["upload_contract"]["proxy"]["original"]
        assert original["capture"]["capture_time"] == "2026-09-20T07:31:02Z"
        # The receipt still validates as the strict contract it always was.
        MediaUploadContract.model_validate(assignment["upload_contract"])
        assert capture_from_assignment(assignment) is not None


@pytest.mark.asyncio
async def test_attach_without_capture_leaves_assignment_and_receipt_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = MediaInput(media_id="analysis-proxy-clip-1.mp4", kind="video", filename="clip.mp4")
    item, _thread, _append = await _attach(monkeypatch, media, proxy=True)
    assignment = item.clip_assignments[0]
    assert "capture" not in assignment
    assert "capture" not in assignment["upload_contract"]["proxy"]["original"]
    assert assignment_facts(assignment) == []
