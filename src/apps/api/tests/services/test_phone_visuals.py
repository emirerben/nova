import hashlib
import math
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound

from app import storage
from app.pipeline.guided_story import GuidedStoryExecutionPlan, GuidedStoryMoment
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.probe import ProbeError, ProbeTimeout, VideoProbe
from app.services import phone_visuals
from app.services.phone_sources import PhoneVisualBinding
from app.services.phone_visuals import (
    MAX_PHONE_VISUAL_BYTES,
    MAX_PHONE_VISUAL_VIDEO_BYTES,
    bind_phone_visuals,
)
from tests.pipeline.test_phone_guided_plan import fixture

USER_ID = uuid.UUID("0b6b1c52-3f0e-4c7a-9d51-6f1e2a3b4c5d")
ITEM_ID = uuid.UUID("7c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f")
JOB_ID = "3e2d1c0b-9a8f-4e7d-8c6b-5a4f3e2d1c0b"
PHOTO_ID = "5b3f6a1e-8f1c-4c55-9a8e-2f7d1c9b0a11"
PHOTO_PATH = f"users/{USER_ID}/plan/{ITEM_ID}/pool/photo.jpg"
PHOTO_GENERATION = "77"
PHOTO_BYTES = b"\xff\xd8pinned-photo-bytes"
VIDEO_ID = "c4e1d2f3-6a7b-4c8d-9e0f-1a2b3c4d5e6f"
VIDEO_PATH = f"users/{USER_ID}/plan/{ITEM_ID}/pool/clip.mov"
VIDEO_GENERATION = "91"
VIDEO_BYTES = b"\x00\x00\x00\x14ftypqt  pinned-video-bytes"
BOTH = frozenset({"image", "video"})
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


def video_moment(**changes):
    """A 2s window of a Visuals-pool video; the fake probe says it runs 6s."""
    return photo_moment(
        **{
            "moment_id": "pool-video-moment",
            "beat_id": "pool-video-beat",
            "topic": "Pool video",
            "media_id": VIDEO_ID,
            "kind": "video",
            "gcs_path": VIDEO_PATH,
            "generation": VIDEO_GENERATION,
            "source_start_s": 1,
            "source_end_s": 3,
        }
        | changes
    )


def pool_plan(*moments):
    """The shared one-video phone plan followed by 2s Visuals-pool moments."""
    plan, bindings = fixture()
    raw = plan.model_dump(mode="json")
    for moment in moments:
        start = raw["resolved_duration_s"]
        placed = moment.model_copy(update={"output_start_s": start, "output_end_s": start + 2})
        raw["story_timeline"].append(placed.model_dump(mode="json"))
        raw["beat_windows"].append(
            {
                "beat_id": placed.beat_id,
                "approved_duration_s": 2,
                "resolved_duration_s": 2,
                "start_s": start,
                "end_s": start + 2,
            }
        )
        raw["selected_media_ids"].append(placed.media_id)
        raw["approved_duration_s"] = raw["resolved_duration_s"] = start + 2
    return GuidedStoryExecutionPlan.model_validate(raw), bindings


def photo_plan():
    return pool_plan(photo_moment())


def photo_visual():
    return PhoneVisualBinding(
        media_id=PHOTO_ID,
        gcs_path=PHOTO_PATH,
        generation=PHOTO_GENERATION,
        sha256=hashlib.sha256(PHOTO_BYTES).hexdigest(),
        byte_count=len(PHOTO_BYTES),
    )


def video_visual(**changes):
    """What binding VIDEO_BYTES yields under the default ``fake_probe``."""
    return PhoneVisualBinding.model_validate(
        {
            "media_id": VIDEO_ID,
            "gcs_path": VIDEO_PATH,
            "generation": VIDEO_GENERATION,
            "sha256": hashlib.sha256(VIDEO_BYTES).hexdigest(),
            "byte_count": len(VIDEO_BYTES),
            "kind": "video",
            "duration_s": 6.05,
            "width": 1920,
            "height": 1080,
            "orientation_degrees": 0,
        }
        | changes
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


def video_row(**changes):
    return photo_row(
        **{
            "id": uuid.UUID(VIDEO_ID),
            "kind": "video",
            "gcs_path": VIDEO_PATH,
            "gcs_generation": VIDEO_GENERATION,
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


def fake_probe(monkeypatch, state=None, **changes):
    """Stand in for ffprobe; records the bytes each probed path held."""
    probed = []

    def probe(path):
        assert state is None or not state["open"], "probe inside a DB session"
        probed.append((path, Path(path).read_bytes()))
        return VideoProbe(
            **{
                # The container outlasts the last frame by its AAC padding.
                "duration_s": 6.05,
                "video_stream_duration_s": 6.0,
                "fps": 30.0,
                "width": 1920,
                "height": 1080,
                "has_audio": True,
                "codec": "h264",
                "pix_fmt": "yuv420p",
                "aspect_ratio": "16:9",
                "file_size_bytes": len(VIDEO_BYTES),
            }
            | changes
        )

    monkeypatch.setattr(phone_visuals, "probe_video", probe)
    return probed


def bind(open_session, timeline, **kwargs):
    return bind_phone_visuals(open_session, job_id=JOB_ID, story_timeline=timeline, **kwargs)


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
    # One download per photo at its approved generation; by default videos
    # never bind.
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


def test_size_cap_matches_the_pool_video_upload_cap():
    from app.routes.plan_items import _MAX_POOL_VIDEO_BYTES

    assert MAX_PHONE_VISUAL_VIDEO_BYTES == _MAX_POOL_VIDEO_BYTES


def test_binds_a_pool_video_with_what_its_pinned_bytes_probe_as(monkeypatch):
    open_session, state = fake_session([video_row()])
    calls = fake_storage(monkeypatch, {(VIDEO_PATH, VIDEO_GENERATION): VIDEO_BYTES}, state)
    # An iPhone portrait recording: coded landscape, display matrix -90.
    probed = fake_probe(monkeypatch, state, rotation_degrees=-90.0)
    visuals = bind(
        open_session,
        [video_moment(), video_moment(moment_id="repeat", source_start_s=3, source_end_s=5)],
        kinds=frozenset({"video"}),
    )
    # Coded size plus orientation, as a device original's descriptor states it;
    # the container duration the planner measured, not the shorter stream.
    assert visuals == (video_visual(orientation_degrees=90),)
    assert visuals[0].render_asset().media_kind == "video"
    assert calls == [(VIDEO_PATH, VIDEO_GENERATION)]
    [(path, seen)] = probed
    assert seen == VIDEO_BYTES
    assert not Path(path).exists()
    assert state["opened"] == 1


@pytest.mark.parametrize(
    "kinds, expected",
    [
        (frozenset({"image"}), ["photo"]),
        (frozenset({"video"}), ["video"]),
        (BOTH, ["photo", "video"]),
        (frozenset(), []),
    ],
    ids=["images", "videos", "both", "none"],
)
def test_only_requested_kinds_bind(monkeypatch, kinds, expected):
    open_session, state = fake_session([photo_row(), video_row()])
    objects = {
        (PHOTO_PATH, PHOTO_GENERATION): PHOTO_BYTES,
        (VIDEO_PATH, VIDEO_GENERATION): VIDEO_BYTES,
    }
    calls = fake_storage(monkeypatch, objects, state)
    probed = fake_probe(monkeypatch, state)
    plan, _ = fixture()
    timeline = [plan.story_timeline[0], photo_moment(), video_moment()]
    bound = {"photo": photo_visual(), "video": video_visual()}
    pins = {"photo": (PHOTO_PATH, PHOTO_GENERATION), "video": (VIDEO_PATH, VIDEO_GENERATION)}
    assert bind(open_session, timeline, kinds=kinds) == tuple(bound[name] for name in expected)
    assert calls == [pins[name] for name in expected]
    # A photo is never probed, and an unrequested kind costs no I/O at all.
    assert len(probed) == expected.count("video")
    assert state["opened"] == (1 if expected else 0)


def test_photo_receipts_keep_the_shape_earlier_jobs_persisted(monkeypatch):
    open_session, _ = fake_session([photo_row(), video_row()])
    fake_storage(
        monkeypatch,
        {
            (PHOTO_PATH, PHOTO_GENERATION): PHOTO_BYTES,
            (VIDEO_PATH, VIDEO_GENERATION): VIDEO_BYTES,
        },
    )
    fake_probe(monkeypatch)
    photo, video = bind(open_session, [photo_moment(), video_moment()], kinds=BOTH)
    assert photo.model_dump(mode="json") == {
        "media_id": PHOTO_ID,
        "gcs_path": PHOTO_PATH,
        "generation": PHOTO_GENERATION,
        "sha256": hashlib.sha256(PHOTO_BYTES).hexdigest(),
        "byte_count": len(PHOTO_BYTES),
    }
    row = video.model_dump(mode="json")
    assert row == photo.model_dump(mode="json") | {
        "media_id": VIDEO_ID,
        "gcs_path": VIDEO_PATH,
        "generation": VIDEO_GENERATION,
        "sha256": hashlib.sha256(VIDEO_BYTES).hexdigest(),
        "byte_count": len(VIDEO_BYTES),
        "kind": "video",
        "duration_s": 6.05,
        "width": 1920,
        "height": 1080,
        "orientation_degrees": 0,
    }
    # The editor and the grant rebuild bindings from these persisted rows.
    assert PhoneVisualBinding.model_validate(row) == video


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "image"},
        {"status": "analyzing"},
        {"plan_item_id": OTHER_ID},
        {"gcs_generation": "92"},
    ],
    ids=["kind", "status", "plan_item", "generation"],
)
def test_changed_or_mismatched_video_row_rejects(monkeypatch, changes):
    open_session, _ = fake_session([video_row(**changes)])
    calls = fake_storage(monkeypatch, {(VIDEO_PATH, VIDEO_GENERATION): VIDEO_BYTES})
    probed = fake_probe(monkeypatch)
    with pytest.raises(ValueError, match="approved video is no longer this story's ready visual"):
        bind(open_session, [video_moment()], kinds=BOTH)
    assert calls == probed == []


def test_one_visual_cannot_be_both_a_photo_and_a_video(monkeypatch):
    open_session, state = fake_session([photo_row()])
    fake_storage(monkeypatch, {(PHOTO_PATH, PHOTO_GENERATION): PHOTO_BYTES})
    with pytest.raises(ValueError, match="two sources"):
        bind(open_session, [photo_moment(), photo_moment(moment_id="v", kind="video")], kinds=BOTH)
    assert state["opened"] == 0


@pytest.mark.parametrize(
    "moment, row, size, fits",
    [
        (video_moment, video_row, 9, True),
        (video_moment, video_row, 16, True),
        (video_moment, video_row, 17, False),
        (video_moment, video_row, 0, False),
        (photo_moment, photo_row, 8, True),
        (photo_moment, photo_row, 9, False),
    ],
    ids=["video-over-photo-cap", "video-at-cap", "video-over-cap", "video-empty", "photo", "big"],
)
def test_each_kind_has_its_own_size_cap(monkeypatch, moment, row, size, fits):
    monkeypatch.setattr(phone_visuals, "MAX_PHONE_VISUAL_BYTES", 8)
    monkeypatch.setattr(phone_visuals, "MAX_PHONE_VISUAL_VIDEO_BYTES", 16)
    open_session, _ = fake_session([row()])
    pinned = moment()
    # A video keeps its leading ISO-BMFF box so only the size decides.
    payload = (VIDEO_BYTES + b"x" * size)[:size] if pinned.kind == "video" else b"x" * size
    fake_storage(monkeypatch, {(pinned.gcs_path, pinned.generation): payload})
    probed = fake_probe(monkeypatch)
    if fits:
        [visual] = bind(open_session, [pinned], kinds=BOTH)
        assert (visual.kind, visual.byte_count) == (pinned.kind, size)
        return
    with pytest.raises(ValueError, match="size is out of bounds"):
        bind(open_session, [pinned], kinds=BOTH)
    # An oversized video is rejected before ffprobe ever opens it.
    assert probed == []


@pytest.mark.parametrize(
    "error", [ProbeError("moov atom not found"), ProbeTimeout("hung")], ids=["corrupt", "timeout"]
)
def test_unreadable_video_rejects(monkeypatch, error):
    open_session, _ = fake_session([video_row()])
    fake_storage(monkeypatch, {(VIDEO_PATH, VIDEO_GENERATION): VIDEO_BYTES})

    def probe(path):
        raise error

    monkeypatch.setattr(phone_visuals, "probe_video", probe)
    with pytest.raises(ValueError, match="could not be read"):
        bind(open_session, [video_moment()], kinds=BOTH)


@pytest.mark.parametrize(
    "changes",
    [
        {"duration_s": 0.0},
        {"duration_s": math.nan},
        {"duration_s": math.inf},
        {"duration_s": -1.0},
        {"width": 0},
        {"height": 0},
    ],
    ids=["zero-duration", "nan-duration", "endless", "negative-duration", "no-width", "no-height"],
)
def test_video_without_usable_duration_or_size_rejects(monkeypatch, changes):
    open_session, _ = fake_session([video_row()])
    fake_storage(monkeypatch, {(VIDEO_PATH, VIDEO_GENERATION): VIDEO_BYTES})
    fake_probe(monkeypatch, **changes)
    with pytest.raises(ValueError, match="could not be read"):
        bind(open_session, [video_moment()], kinds=BOTH)


@pytest.mark.parametrize("stream_s", [None, 6.0, 5.9], ids=["no-stream", "padded", "short"])
def test_video_binds_the_container_duration_the_planner_measured(monkeypatch, stream_s):
    # autoplace stores probe.duration_s as the pool row's duration_s; the
    # planner draws windows inside it, and the engine clamps to the real track.
    open_session, _ = fake_session([video_row()])
    fake_storage(monkeypatch, {(VIDEO_PATH, VIDEO_GENERATION): VIDEO_BYTES})
    fake_probe(monkeypatch, video_stream_duration_s=stream_s)
    assert bind(open_session, [video_moment()], kinds=BOTH) == (video_visual(duration_s=6.05),)


def test_a_window_the_planner_drew_to_the_container_end_compiles_as_approved(monkeypatch):
    # Bound at the 6.0s video stream, the refit used to move this window off
    # its approved source range; bound at the planner's duration it stays put.
    open_session, _ = fake_session([video_row()])
    fake_storage(monkeypatch, {(VIDEO_PATH, VIDEO_GENERATION): VIDEO_BYTES})
    fake_probe(monkeypatch)
    moment = video_moment(source_start_s=4.05, source_end_s=6.05)
    plan, bindings = pool_plan(moment)
    visuals = bind(open_session, [moment], kinds=BOTH)
    clip = compile_phone_guided_plan(plan, bindings, visuals).tracks[0].clips[-1]
    assert (clip.source_start, clip.source_duration) == pytest.approx((4.05, 2))


@pytest.mark.parametrize(
    "box", [b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"], ids=bytes.decode
)
def test_iso_bmff_videos_the_engine_opens_bind(monkeypatch, box):
    payload = b"\x00\x00\x00\x08" + box + b"rest-of-the-file"
    open_session, _ = fake_session([video_row()])
    fake_storage(monkeypatch, {(VIDEO_PATH, VIDEO_GENERATION): payload})
    fake_probe(monkeypatch)
    [visual] = bind(open_session, [video_moment()], kinds=BOTH)
    assert visual.byte_count == len(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01webm",
        b"RIFF\x00\x00\x00\x00AVI LIST",
        b"\x00\x00\x01\xba\x44\x00\x04\x00mpeg-ps",
        b"ftyp\x00\x00\x00\x08qt  ",
        b"\x00\x00\x00",
    ],
    ids=["webm", "avi", "mpeg-ps", "misaligned-ftyp", "truncated"],
)
def test_video_the_engine_cannot_open_rejects_before_probing(monkeypatch, payload):
    # VisualVideoFile.fileExtension would throw on the phone after the download.
    open_session, _ = fake_session([video_row()])
    fake_storage(monkeypatch, {(VIDEO_PATH, VIDEO_GENERATION): payload})
    probed = fake_probe(monkeypatch)
    with pytest.raises(ValueError, match="not an MP4 or MOV file"):
        bind(open_session, [video_moment()], kinds=BOTH)
    assert probed == []


@pytest.mark.parametrize(
    "codec, pix_fmt",
    [
        ("h264", "yuv420p"),
        ("h264", "yuvj420p"),
        ("hevc", "yuv420p"),
        ("hevc", "yuvj420p"),
        ("hevc", "yuv420p10le"),
    ],
)
def test_h264_and_hevc_420_videos_bind(monkeypatch, codec, pix_fmt):
    open_session, _ = fake_session([video_row()])
    fake_storage(monkeypatch, {(VIDEO_PATH, VIDEO_GENERATION): VIDEO_BYTES})
    fake_probe(monkeypatch, codec=codec, pix_fmt=pix_fmt)
    assert bind(open_session, [video_moment()], kinds=BOTH) == (video_visual(),)


@pytest.mark.parametrize(
    "codec, pix_fmt",
    [
        ("vp9", "yuv420p"),
        ("av1", "yuv420p"),
        ("prores", "yuv422p10le"),
        ("mpeg4", "yuv420p"),
        ("h264", "yuv420p10le"),
        ("h264", "yuv422p"),
        ("h264", "yuv444p"),
        ("hevc", "yuv422p10le"),
        ("h264", ""),
        ("unknown", "yuv420p"),
    ],
)
def test_video_the_engine_cannot_compose_rejects(monkeypatch, codec, pix_fmt):
    # AVComposition has no conversion step: AV1/VP9 or a 10-bit/4:2:2 H.264
    # track would download and then fail the export on the phone.
    open_session, _ = fake_session([video_row()])
    fake_storage(monkeypatch, {(VIDEO_PATH, VIDEO_GENERATION): VIDEO_BYTES})
    fake_probe(monkeypatch, codec=codec, pix_fmt=pix_fmt)
    with pytest.raises(ValueError, match="H.264 or HEVC with 4:2:0 color"):
        bind(open_session, [video_moment()], kinds=BOTH)


@pytest.mark.parametrize(
    "rotation, orientation",
    [(0, 0), (-90, 90), (90, 270), (180, 180), (-180, 180), (270, 90), (-270, 270), (360, 0)],
)
def test_display_matrix_rotation_becomes_the_originals_clockwise_orientation(
    monkeypatch, rotation, orientation
):
    open_session, _ = fake_session([video_row()])
    fake_storage(monkeypatch, {(VIDEO_PATH, VIDEO_GENERATION): VIDEO_BYTES})
    fake_probe(monkeypatch, rotation_degrees=float(rotation))
    [visual] = bind(open_session, [video_moment()], kinds=BOTH)
    # Size stays coded: the compiler and the engine pair it with the orientation.
    assert (visual.width, visual.height) == (1920, 1080)
    assert visual.orientation_degrees == orientation


@pytest.mark.parametrize("rotation", [45.0, -30.0, math.nan], ids=["45", "-30", "nan"])
def test_skewed_display_matrix_fails_closed(monkeypatch, rotation):
    open_session, _ = fake_session([video_row()])
    fake_storage(monkeypatch, {(VIDEO_PATH, VIDEO_GENERATION): VIDEO_BYTES})
    fake_probe(monkeypatch, rotation_degrees=rotation)
    with pytest.raises(ValueError, match="rotation is unsupported"):
        bind(open_session, [video_moment()], kinds=BOTH)
