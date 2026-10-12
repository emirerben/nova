"""Additive attachment contracts keep shared source identity and post identity separate."""

import uuid

import pytest
from pydantic import ValidationError

from app.routes.creation_threads import MediaInput
from app.routes.plan_items import RegisterAssetBody


def test_thread_library_attachment_keeps_independent_media_id():
    asset, source = uuid.uuid4(), uuid.uuid4()
    body = MediaInput(
        media_id="analysis-proxy-project-clip.mp4",
        kind="video",
        library_asset_id=asset,
        library_source_id=source,
    )
    assert body.library_asset_id == asset
    assert body.library_source_id == source
    assert body.media_id == "analysis-proxy-project-clip.mp4"


def test_editor_library_attachment_needs_no_untrusted_path():
    asset, source = uuid.uuid4(), uuid.uuid4()
    body = RegisterAssetBody(library_asset_id=asset, library_source_id=source)
    assert body.gcs_path is None and body.content_type is None
    assert body.library_asset_id == asset


def test_source_id_requires_asset_and_legacy_contract_still_requires_path():
    with pytest.raises(ValidationError):
        MediaInput(media_id="clip.mp4", kind="video", library_source_id=uuid.uuid4())
    with pytest.raises(ValidationError):
        RegisterAssetBody(library_source_id=uuid.uuid4())
    with pytest.raises(ValidationError):
        RegisterAssetBody(content_type="image/png")
    assert RegisterAssetBody(gcs_path="users/owner/photo.png", content_type="image/png").gcs_path
