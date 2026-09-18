"""KRI-121: a phone plan leaves out pool videos the iPhone engine cannot compose."""

from __future__ import annotations

import pytest

from app.config import settings
from app.schemas.edit_proposal import MediaRef
from app.services.edit_proposals import phone_renderable_media
from app.services.phone_visuals import MAX_PHONE_VISUAL_VIDEO_S

OWNER = "0b6b1c52-3f0e-4c7a-9d51-6f1e2a3b4c5d"
FOOTAGE = MediaRef(
    lane="clip",
    media_id="footage",
    gcs_path="users/u/plan/i/analysis-proxy-harbor.mp4",
    generation="1",
    kind="video",
    duration_s=20,
    analysis={"video_codec": "vp9", "pix_fmt": "yuv444p"},
)
PHOTO = MediaRef(
    lane="asset",
    media_id="photo",
    gcs_path="users/u/plan/i/pool/harbor.jpg",
    generation="2",
    kind="image",
)


def pool_video(*, duration_s=12.0, **analysis):
    return MediaRef(
        lane="asset",
        media_id="pool-video",
        gcs_path="users/u/plan/i/pool/harbor.mov",
        generation="3",
        kind="video",
        duration_s=duration_s,
        analysis={"subject": "harbor", **analysis},
    )


@pytest.fixture
def pilot(monkeypatch):
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [OWNER])
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "stillImages", "visualVideos"],
    )


@pytest.mark.parametrize(
    "video",
    [
        pool_video(video_codec="h264", pix_fmt="yuv420p"),
        pool_video(video_codec="hevc", pix_fmt="yuv420p10le"),
        pool_video(video_codec="hevc", pix_fmt="yuvj420p"),
        # Analyzed before these facts were recorded: binding re-checks the bytes.
        pool_video(),
        pool_video(video_codec="h264"),
        pool_video(pix_fmt="yuv420p"),
        pool_video(duration_s=None),
        pool_video(duration_s=MAX_PHONE_VISUAL_VIDEO_S),
    ],
    ids=[
        "h264",
        "hevc-10bit",
        "hevc-full-range",
        "unrecorded",
        "codec-only",
        "pix-fmt-only",
        "no-duration",
        "at-the-limit",
    ],
)
def test_a_composable_pool_video_stays_in_the_phone_plan(pilot, video):
    media = [FOOTAGE, PHOTO, video]
    assert phone_renderable_media(media, OWNER) == media


@pytest.mark.parametrize(
    "video",
    [
        pool_video(video_codec="vp9", pix_fmt="yuv420p"),
        pool_video(video_codec="av1", pix_fmt="yuv420p"),
        pool_video(video_codec="h264", pix_fmt="yuv420p10le"),
        pool_video(video_codec="h264", pix_fmt="yuv422p"),
        pool_video(video_codec="hevc", pix_fmt="yuv422p10le"),
        pool_video(video_codec="vp9"),
        pool_video(pix_fmt="yuv444p"),
        # What probe_video reports when ffprobe couldn't name them.
        pool_video(video_codec="unknown", pix_fmt="yuv420p"),
        pool_video(video_codec="h264", pix_fmt=""),
        pool_video(duration_s=MAX_PHONE_VISUAL_VIDEO_S + 0.5),
        pool_video(duration_s=2400, video_codec="h264", pix_fmt="yuv420p"),
    ],
    ids=[
        "vp9",
        "av1",
        "h264-10bit",
        "h264-422",
        "hevc-422",
        "codec-only",
        "pix-fmt-only",
        "unknown-codec",
        "unknown-pix-fmt",
        "past-the-limit",
        "long-recording",
    ],
)
def test_a_pool_video_the_phone_cannot_compose_is_left_out(pilot, video):
    assert phone_renderable_media([FOOTAGE, PHOTO, video], OWNER) == [FOOTAGE, PHOTO]
    # A footage-free phone item applies the same rule.
    assert phone_renderable_media([PHOTO, video], OWNER, visuals_only_device=True) == [PHOTO]


def test_device_footage_and_cloud_items_are_never_filtered_by_codec(pilot):
    video = pool_video(video_codec="vp9", pix_fmt="yuv420p", duration_s=2400)
    # Footage originals come from the iPhone itself; only the pool is checked.
    assert phone_renderable_media([FOOTAGE], OWNER) == [FOOTAGE]
    # Without a proxy or the destination decision the item renders in the cloud.
    cloud = [PHOTO, video]
    assert phone_renderable_media(cloud, OWNER) == cloud
