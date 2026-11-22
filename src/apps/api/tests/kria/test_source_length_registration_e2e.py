"""End-to-end: a source over its format's length limit is refused when it is registered (KRI-1554).

Alanur's six-minute talk uploaded, was planned and approved, and only then failed with
``subtitled_clip_too_long``. An app build without the pick-time check must be refused at upload
registration instead: at `upload-urls` when its proxy names the original's length, and at
`/media` (attach) from the real ffprobe of the uploaded bytes otherwise.

Real FastAPI app over ASGI, real Postgres (the scratch `*_test` database), real ffprobe on real
MP4 files and the real upload verifier, reusing `test_replace_media_e2e`'s world. Only the object
store is replaced: it serves local files.

Failure modes this file answers:
  late refusal      a 6-minute Talking clip gets an upload URL or registers, then fails late
  silent refusal    the refusal does not name the limit
  leak              a montage clip of the same length is refused (the Talking row leaks)
  off by one        a 4 min 50 s Talking clip is refused
  partial write     a refused attach changes the item or the thread
"""

# ruff: noqa: F811 -- imported pytest fixtures
from __future__ import annotations

import hashlib
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.database import sync_session
from app.models import CreationThread, CreationThreadUploadReservation, PlanItem
from tests.kria.test_replace_media_e2e import (  # noqa: F401 - fixtures
    PROXY,
    World,
    _api,
    _dispose_async_engine,
    _flags,
    object_store,
    seed_world,
)

SIX_MINUTES = 360.0
FOUR_FIFTY = 290.0


def _encode_long(path: Path, seconds: float) -> None:
    # A small, low-rate picture keeps a six-minute file quick to make; the length is what counts.
    subprocess.run(
        [
            *("ffmpeg", "-y", "-v", "error"),
            *("-f", "lavfi", "-i", f"testsrc=size=160x284:rate=5:duration={seconds}"),
            *("-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest"),
            *("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac"),
            str(path),
        ],
        check=True,
    )


def _probe_s(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(out.stdout.strip())


def _contract(path: Path) -> dict:
    """The proxy contract an iPhone sends: it names the original's measured length."""
    duration = _probe_s(path)
    return {
        "purpose": "analysis_proxy",
        "proxy": {
            "original": {
                "kind": "video",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "byte_count": path.stat().st_size,
                "duration_s": duration,
                "width": 1080,
                "height": 1920,
                "has_audio": True,
            },
            "duration_s": duration,
            "width": 160,
            "height": 284,
            "frame_rate": 5.0,
        },
    }


@pytest.fixture(scope="module")
def long_takes(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("long-takes")
    files = {"six": root / "six.mp4", "four_fifty": root / "four_fifty.mp4"}
    _encode_long(files["six"], SIX_MINUTES)
    _encode_long(files["four_fifty"], FOUR_FIFTY)
    return files


def _reserve(world: World, label: str, file: Path) -> str:
    """What an older build's completed `upload-urls` + PUT leaves behind."""
    media_id = f"{PROXY}ios-{label}-{uuid.uuid4().hex[:8]}.mp4"
    path = f"users/{world.user_id}/creation-threads/{world.thread_id}/{media_id}"
    world.paths[media_id] = path
    world.files[path] = file
    with sync_session() as db:
        db.add(
            CreationThreadUploadReservation(
                thread_id=world.thread_id,
                creator_id=world.user_id,
                media_id=media_id,
                object_path=path,
                upload_contract=_contract(file),
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        db.commit()
    return media_id


def _upload_body(file: Path, client_id: str) -> dict:
    return {
        "files": [
            {
                "filename": file.name,
                "content_type": "video/mp4",
                "file_size_bytes": file.stat().st_size,
                "client_upload_id": client_id,
                "upload_contract": _contract(file),
            }
        ]
    }


async def _attach(api, media_id: str):  # noqa: ANN001, ANN202
    return await api.post(
        "/media",
        {
            "media": [{"media_id": media_id, "kind": "video", "filename": "talk.mp4"}],
            "client_event_id": f"ios-attach-{uuid.uuid4().hex[:10]}",
            "expected_revision": api.revision(),
        },
    )


def _item_and_thread(world: World) -> tuple[list, int]:
    with sync_session() as db:
        item = db.get(PlanItem, world.item_id)
        thread = db.get(CreationThread, world.thread_id)
        return list(item.clip_gcs_paths or []), int(thread.revision)


@pytest.mark.asyncio
async def test_six_minute_talking_clip_is_refused_at_upload_urls(long_takes, object_store) -> None:
    world = seed_world({}, labels=(), edit_format="subtitled", job=None)
    api = _api(world, object_store)

    refused = await api.post("/upload-urls", _upload_body(long_takes["six"], "ios-six-1"))

    assert refused.status_code == 422, refused.text
    assert "5 minutes" in refused.text
    with sync_session() as db:
        assert (
            db.query(CreationThreadUploadReservation).filter_by(thread_id=world.thread_id).count()
            == 0
        )


@pytest.mark.asyncio
async def test_six_minute_talking_clip_is_refused_at_attach(long_takes, object_store) -> None:
    """A reservation made before the check (or without a proxy) still cannot register."""
    world = seed_world({}, labels=(), edit_format="subtitled", job=None)
    api = _api(world, object_store)
    media_id = _reserve(world, "six", long_takes["six"])
    object_store.registry[world.paths[media_id]] = long_takes["six"]
    before = _item_and_thread(world)

    refused = await _attach(api, media_id)

    assert refused.status_code == 422, refused.text
    assert "5 minutes" in refused.text
    assert _item_and_thread(world) == before


@pytest.mark.asyncio
async def test_four_minute_fifty_talking_clip_registers(long_takes, object_store) -> None:
    world = seed_world({}, labels=(), edit_format="subtitled", job=None)
    api = _api(world, object_store)

    reserved = await api.post("/upload-urls", _upload_body(long_takes["four_fifty"], "ios-450-1"))
    assert reserved.status_code == 200, reserved.text
    media_id = reserved.json()[0]["media_id"]
    path = f"users/{world.user_id}/creation-threads/{world.thread_id}/{media_id}"
    object_store.registry[path] = long_takes["four_fifty"]

    attached = await _attach(api, media_id)

    assert attached.status_code == 200, attached.text
    clips = attached.json()["media_capabilities"]["clips"]
    assert clips["max_duration_s"] == 300.0
    assert _item_and_thread(world)[0] == [path]


@pytest.mark.asyncio
async def test_six_minute_montage_clip_registers(long_takes, object_store) -> None:
    world = seed_world({}, labels=(), edit_format="montage", job=None)
    api = _api(world, object_store)

    reserved = await api.post("/upload-urls", _upload_body(long_takes["six"], "ios-montage-1"))
    assert reserved.status_code == 200, reserved.text
    media_id = reserved.json()[0]["media_id"]
    path = f"users/{world.user_id}/creation-threads/{world.thread_id}/{media_id}"
    object_store.registry[path] = long_takes["six"]

    attached = await _attach(api, media_id)

    assert attached.status_code == 200, attached.text
    assert attached.json()["media_capabilities"]["clips"]["max_duration_s"] == 1800.0
