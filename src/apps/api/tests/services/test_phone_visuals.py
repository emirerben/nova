import hashlib
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound

from app import storage
from app.pipeline.guided_story import GuidedStoryExecutionPlan, GuidedStoryMoment
from app.services import phone_visuals
from app.services.phone_sources import PhoneVisualBinding
from app.services.phone_visuals import MAX_PHONE_VISUAL_BYTES, bind_phone_visuals
from tests.pipeline.test_phone_guided_plan import fixture

USER_ID = uuid.UUID("0b6b1c52-3f0e-4c7a-9d51-6f1e2a3b4c5d")
ITEM_ID = uuid.UUID("7c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f")
JOB_ID = "3e2d1c0b-9a8f-4e7d-8c6b-5a4f3e2d1c0b"
PHOTO_ID = "5b3f6a1e-8f1c-4c55-9a8e-2f7d1c9b0a11"
PHOTO_PATH = f"users/{USER_ID}/plan/{ITEM_ID}/pool/photo.jpg"
PHOTO_GENERATION = "77"
PHOTO_BYTES = b"\xff\xd8pinned-photo-bytes"
# Fixed, not uuid4(): parametrize ids embed the paths, and xdist (CI) requires
# every worker to collect identical ids.
OTHER_ID = uuid.UUID("9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a")


def photo_moment(**changes):
    return GuidedStoryMoment.model_validate(
        {
            "moment_id": "photo-moment",
            "beat_id": "photo-beat",
            "topic": "Photo",
            "media_id": PHOTO_ID,
            "lane": "asset",
            "kind": "image",
            "gcs_path": PHOTO_PATH,
            "generation": PHOTO_GENERATION,
            "layout": "fullscreen",
            "source_start_s": 0,
            "source_end_s": 2,
            "output_start_s": 3,
            "output_end_s": 5,
            "duration_s": 2,
        }
        | changes
    )


def photo_plan():
    """The shared one-video phone plan followed by a 2s Visuals-pool photo."""
    plan, bindings = fixture()
    raw = plan.model_dump(mode="json")
    raw["story_timeline"].append(photo_moment().model_dump(mode="json"))
    raw["beat_windows"].append(
        {
            "beat_id": "photo-beat",
            "approved_duration_s": 2,
            "resolved_duration_s": 2,
            "start_s": 3,
            "end_s": 5,
        }
    )
    raw["selected_media_ids"].append(PHOTO_ID)
    raw["approved_duration_s"] = raw["resolved_duration_s"] = 5
    return GuidedStoryExecutionPlan.model_validate(raw), bindings


def photo_visual():
    return PhoneVisualBinding(
        media_id=PHOTO_ID,
        gcs_path=PHOTO_PATH,
        generation=PHOTO_GENERATION,
        sha256=hashlib.sha256(PHOTO_BYTES).hexdigest(),
        byte_count=len(PHOTO_BYTES),
    )


def photo_row(**changes):
    """A PlanItemAsset-like ready pool photo owned by the job's plan item."""
    return SimpleNamespace(
        **{
            "id": uuid.UUID(PHOTO_ID),
            "plan_item_id": ITEM_ID,
            "user_id": USER_ID,
            "status": "ready",
            "kind": "image",
            "gcs_path": PHOTO_PATH,
            "gcs_generation": PHOTO_GENERATION,
        }
        | changes
    )


OWNER = SimpleNamespace(user_id=USER_ID, content_plan_item_id=ITEM_ID)


def fake_session(rows, *, job=OWNER):
    """Answer the binder's two reads, and record whether a session is open."""
    state = {"open": False, "opened": 0}
    db = SimpleNamespace(
        get=lambda model, key: job,
        execute=lambda statement: SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: list(rows))
        ),
    )

    @contextmanager
    def open_session():
        state["open"] = True
        state["opened"] += 1
        try:
            yield db
        finally:
            state["open"] = False

    return open_session, state


def fake_storage(monkeypatch, objects, state=None):
    """Serve bytes only for an exact (path, generation) pin."""
    calls = []

    def download(path, local, *, generation):
        assert state is None or not state["open"], "storage I/O inside a DB session"
        calls.append((path, generation))
        if (path, generation) not in objects:
            raise FileNotFoundError(path)
        Path(local).write_bytes(objects[(path, generation)])

    monkeypatch.setattr(storage, "download_generation_to_file", download)
    return calls


def bind(open_session, timeline):
    return bind_phone_visuals(open_session, job_id=JOB_ID, story_timeline=timeline)


def test_binds_only_timeline_photos_to_their_pinned_bytes(monkeypatch):
    other_id = "9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"
    other_path = f"users/{USER_ID}/plan/{ITEM_ID}/pool/other.png"
    unused_id = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    plan, _ = fixture()
    video = plan.story_timeline[0]
    pool_video = photo_moment(media_id=unused_id, kind="video", gcs_path=f"{other_path}.mov")
    timeline = [
        video,
        photo_moment(),
        pool_video,
        photo_moment(moment_id="again", media_id=other_id, gcs_path=other_path, generation="5"),
        photo_moment(moment_id="repeat"),
    ]
    rows = [
        photo_row(),
        photo_row(id=uuid.UUID(other_id), gcs_path=other_path, gcs_generation="5"),
        photo_row(id=uuid.UUID(unused_id), gcs_path=f"{other_path}.mov", kind="video"),
    ]
    open_session, state = fake_session(rows)
    calls = fake_storage(
        monkeypatch,
        {
            (PHOTO_PATH, PHOTO_GENERATION): PHOTO_BYTES,
            (PHOTO_PATH, "78"): b"replacement bytes",
            (other_path, "5"): b"other",
        },
        state,
    )
    visuals = bind(open_session, timeline)
    assert visuals == (
        photo_visual(),
        PhoneVisualBinding(
            media_id=other_id,
            gcs_path=other_path,
            generation="5",
            sha256=hashlib.sha256(b"other").hexdigest(),
            byte_count=5,
        ),
    )
    # One download per photo at its approved generation; videos never bind.
    assert calls == [(PHOTO_PATH, PHOTO_GENERATION), (other_path, "5")]
    assert state["opened"] == 1


def test_story_without_photos_touches_neither_database_nor_storage(monkeypatch):
    open_session, state = fake_session([photo_row()])
    calls = fake_storage(monkeypatch, {})
    plan, _ = fixture()
    assert bind(open_session, plan.story_timeline) == ()
    assert state["opened"] == 0
    assert calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"plan_item_id": OTHER_ID},
        {"user_id": OTHER_ID},
        {"status": "failed"},
        {"status": "analyzing"},
        {"kind": "video"},
        {"gcs_path": f"users/{USER_ID}/plan/{ITEM_ID}/pool/replaced.jpg"},
        {"gcs_generation": "78"},
        {"gcs_generation": None},
    ],
)
def test_changed_or_foreign_row_rejects(monkeypatch, changes):
    open_session, _ = fake_session([photo_row(**changes)])
    calls = fake_storage(monkeypatch, {(PHOTO_PATH, PHOTO_GENERATION): PHOTO_BYTES})
    with pytest.raises(ValueError, match="no longer this story's ready visual"):
        bind(open_session, [photo_moment()])
    assert calls == []


def test_missing_row_rejects(monkeypatch):
    open_session, _ = fake_session([])
    calls = fake_storage(monkeypatch, {(PHOTO_PATH, PHOTO_GENERATION): PHOTO_BYTES})
    with pytest.raises(ValueError, match="no longer this story's ready visual"):
        bind(open_session, [photo_moment()])
    assert calls == []


@pytest.mark.parametrize(
    "path",
    [
        f"users/{OTHER_ID}/plan/{ITEM_ID}/pool/photo.jpg",
        f"users/{USER_ID}/plan/{OTHER_ID}/pool/photo.jpg",
        f"users/{USER_ID}/plan/{ITEM_ID}/pool/../../other/pool/photo.jpg",
        f"users/{USER_ID}/plan/{ITEM_ID}/pool/./photo.jpg",
        f"users/{USER_ID}/plan/{ITEM_ID}/pool//photo.jpg",
        f"users/{USER_ID}/plan/{ITEM_ID}/pool/",
        f"users/{USER_ID}/plan/{ITEM_ID}/pool/a\\photo.jpg",
        "music/track/audio.mp3",
    ],
)
def test_path_outside_this_items_pool_rejects_even_when_row_agrees(monkeypatch, path):
    open_session, _ = fake_session([photo_row(gcs_path=path)])
    calls = fake_storage(monkeypatch, {(path, PHOTO_GENERATION): PHOTO_BYTES})
    with pytest.raises(ValueError, match="no longer this story's ready visual"):
        bind(open_session, [photo_moment(gcs_path=path)])
    assert calls == []


@pytest.mark.parametrize("job", [None, SimpleNamespace(user_id=USER_ID, content_plan_item_id=None)])
def test_job_without_its_plan_item_rejects(monkeypatch, job):
    open_session, _ = fake_session([photo_row()], job=job)
    calls = fake_storage(monkeypatch, {(PHOTO_PATH, PHOTO_GENERATION): PHOTO_BYTES})
    with pytest.raises(ValueError, match="plan item"):
        bind(open_session, [photo_moment()])
    assert calls == []


@pytest.mark.parametrize("media_id", ["not-a-uuid", PHOTO_ID.upper(), PHOTO_ID.replace("-", "")])
def test_noncanonical_photo_identity_rejects(monkeypatch, media_id):
    open_session, state = fake_session([photo_row()])
    fake_storage(monkeypatch, {(PHOTO_PATH, PHOTO_GENERATION): PHOTO_BYTES})
    with pytest.raises(ValueError, match="identity is invalid"):
        bind(open_session, [photo_moment(media_id=media_id)])
    assert state["opened"] == 0


def test_one_photo_pinned_to_two_generations_rejects(monkeypatch):
    open_session, _ = fake_session([photo_row()])
    fake_storage(monkeypatch, {(PHOTO_PATH, PHOTO_GENERATION): PHOTO_BYTES})
    with pytest.raises(ValueError, match="two sources"):
        bind(open_session, [photo_moment(), photo_moment(moment_id="later", generation="78")])


@pytest.mark.parametrize(
    "error", [FileNotFoundError(PHOTO_PATH), NotFound("gone")], ids=["local", "gcs"]
)
def test_missing_generation_rejects(monkeypatch, error):
    open_session, _ = fake_session([photo_row()])

    def download(*args, **kwargs):
        raise error

    monkeypatch.setattr(storage, "download_generation_to_file", download)
    with pytest.raises(ValueError, match="bytes are unavailable"):
        bind(open_session, [photo_moment()])


@pytest.mark.parametrize("size", [0, 17])
def test_empty_or_oversized_bytes_reject(monkeypatch, size):
    monkeypatch.setattr(phone_visuals, "MAX_PHONE_VISUAL_BYTES", 16)
    open_session, _ = fake_session([photo_row()])
    fake_storage(monkeypatch, {(PHOTO_PATH, PHOTO_GENERATION): b"x" * size})
    with pytest.raises(ValueError, match="size is out of bounds"):
        bind(open_session, [photo_moment()])


def test_size_cap_matches_the_pool_upload_cap():
    from app.routes.plan_items import _MAX_POOL_IMAGE_BYTES

    assert MAX_PHONE_VISUAL_BYTES == _MAX_POOL_IMAGE_BYTES
