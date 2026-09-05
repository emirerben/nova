"""Regression locks for chat-first media parity with PlanItem uploads.

The chat workspace is only a different entry point into the existing PlanItem
pipeline. These tests intentionally pin the shared primary-footage contract so
the chat route cannot silently reintroduce a smaller private limit.
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.routes.creation_threads import (
    AttachBody,
    MediaInput,
    UploadBody,
    UploadFile,
    _media_capabilities,
)
from app.routes.plan_items import _MAX_BYTES_PER_FILE, _MAX_CLIPS_PER_ITEM


def _video_upload(index: int, *, size: int = 1) -> UploadFile:
    return UploadFile(
        filename=f"clip-{index}.mp4",
        content_type="video/mp4",
        file_size_bytes=size,
        client_upload_id=f"clip-{index}",
    )


def _video_media(index: int) -> MediaInput:
    return MediaInput(
        media_id=f"clip-{index}",
        kind="video",
        filename=f"clip-{index}.mp4",
        content_type="video/mp4",
    )


def test_chat_upload_reservation_uses_the_plan_item_clip_count_ceiling() -> None:
    """The chat reservation batch must accept the same 50 primary clips."""

    assert _MAX_CLIPS_PER_ITEM == 50
    body = UploadBody(files=[_video_upload(index) for index in range(_MAX_CLIPS_PER_ITEM)])
    assert len(body.files) == 50
    with pytest.raises(ValidationError):
        UploadBody(files=[_video_upload(index) for index in range(_MAX_CLIPS_PER_ITEM + 1)])


def test_chat_upload_reservation_uses_the_plan_item_file_size_ceiling() -> None:
    """A valid PlanItem-sized source must not be rejected by chat validation."""

    assert _MAX_BYTES_PER_FILE == 4 * 1024 * 1024 * 1024
    upload = _video_upload(0, size=_MAX_BYTES_PER_FILE)
    assert upload.file_size_bytes == _MAX_BYTES_PER_FILE
    with pytest.raises(ValidationError):
        _video_upload(0, size=_MAX_BYTES_PER_FILE + 1)


def test_chat_media_attachment_batch_matches_primary_clip_ceiling() -> None:
    """Attaching reserved footage cannot impose a second 20-file ceiling."""

    body = AttachBody(
        media=[_video_media(index) for index in range(_MAX_CLIPS_PER_ITEM)],
        client_event_id="attach-50",
        expected_revision=0,
    )
    assert len(body.media) == _MAX_CLIPS_PER_ITEM
    with pytest.raises(ValidationError):
        AttachBody(
            media=[_video_media(index) for index in range(_MAX_CLIPS_PER_ITEM + 1)],
            client_event_id="attach-51",
            expected_revision=0,
        )


def test_chat_media_capabilities_expose_the_existing_plan_item_pools() -> None:
    """Chat clients receive the PlanItem clip, Visuals, and voiceover rules."""

    item = SimpleNamespace(edit_format="montage", voiceover_gcs_path=None)
    capabilities = _media_capabilities(item=item, clip_count=4, visual_count=7)

    assert capabilities["clips"] == {
        "current": 4,
        "max": 50,
        "server_max": 50,
        "max_file_bytes": 4 * 1024 * 1024 * 1024,
        "content_types": ["video/mp4", "video/quicktime"],
        "format": "montage",
    }
    assert capabilities["visuals"]["current"] == 7
    assert capabilities["visuals"]["max"] == 100
    assert capabilities["visuals"]["max_file_bytes"] == {
        "image": 25 * 1024 * 1024,
        "video": 512 * 1024 * 1024,
    }
    assert capabilities["voiceover"]["max"] == 1


def test_visual_media_is_exposed_as_a_separate_plan_item_pool() -> None:
    """Chat advertises Visuals separately; the pool owns its upload path/rows."""

    item = SimpleNamespace(edit_format="montage", voiceover_gcs_path=None)
    capabilities = _media_capabilities(item=item, clip_count=0, visual_count=0)
    assert capabilities["visuals"]["content_types"]
    assert capabilities["visuals"]["max"] == 100
