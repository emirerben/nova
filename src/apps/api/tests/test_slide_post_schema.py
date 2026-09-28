"""Schema tests for the per-slide `edits` field (plans/024 follow-up
eng-review, 2026-09-10) — `SlideEdits`/`TextOverlay` on `SlideRef`.

The FFmpeg-facing behavior these edits drive is covered by real-ffmpeg
tests in tests/pipeline/test_slide_post_build.py; this file is pure
Pydantic validation.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.routes.plan_items import _slide_post_export_is_current
from app.schemas.slide_post import (
    MAX_SLIDE_TEXT_LENGTH,
    SlideEdits,
    SlidePostDraft,
    SlideRef,
    TextOverlay,
    parse_slide_post,
)


@pytest.mark.parametrize("position", ["top", "center", "bottom"])
def test_text_overlay_accepts_every_valid_position(position: str) -> None:
    TextOverlay(content="hi", position=position)


def test_text_overlay_rejects_invalid_position() -> None:
    with pytest.raises(ValidationError):
        TextOverlay(content="hi", position="left")


def test_text_overlay_rejects_empty_content() -> None:
    with pytest.raises(ValidationError):
        TextOverlay(content="", position="top")


def test_text_overlay_rejects_content_over_the_cap() -> None:
    with pytest.raises(ValidationError):
        TextOverlay(content="x" * (MAX_SLIDE_TEXT_LENGTH + 1), position="top")


def test_text_overlay_accepts_content_at_the_cap() -> None:
    TextOverlay(content="x" * MAX_SLIDE_TEXT_LENGTH, position="top")


def test_slide_post_draft_with_edited_slide_parses_via_parse_slide_post() -> None:
    draft = SlidePostDraft(
        platform_profile="instagram_carousel",
        slides=[
            SlideRef(
                id="s0",
                asset_id=uuid.uuid4(),
                kind="video",
                edits=SlideEdits(look_preset="golden_hour"),
            ),
        ],
        cover_index=0,
    )
    restored = parse_slide_post(draft.model_dump(mode="json"))
    assert restored is not None
    assert restored.slides[0].edits is not None
    assert restored.slides[0].edits.look_preset == "golden_hour"


def test_legacy_slide_ref_json_without_edits_field_still_parses() -> None:
    """A draft persisted before this feature shipped has no `edits` key at
    all on its slides — must not fail closed on old, legitimate data."""
    legacy = {
        "schema_version": 1,
        "version": 1,
        "platform_profile": "tiktok_photo",
        "slides": [{"id": "s0", "asset_id": str(uuid.uuid4()), "kind": "image"}],
        "cover_index": 0,
        "caption": "",
        "rendered_version": None,
        "user_edited": False,
    }
    restored = parse_slide_post(legacy)
    assert restored is not None
    assert restored.slides[0].edits is None


def test_export_freshness_requires_complete_unique_rendered_assets() -> None:
    draft = SlidePostDraft(
        platform_profile="instagram_carousel",
        slides=[
            SlideRef(id="a", asset_id=uuid.uuid4(), kind="image"),
            SlideRef(id="b", asset_id=uuid.uuid4(), kind="image"),
        ],
        version=3,
        rendered_version=3,
    )
    valid = {
        "render_status": "ready",
        "slides": [
            {"asset_id": str(draft.slides[0].asset_id), "asset_gcs_path": "private/a.jpg"},
            {"asset_id": str(draft.slides[1].asset_id), "asset_gcs_path": "private/b.jpg"},
        ],
        "slide_post": {"validation": {"errors": []}},
    }
    assert _slide_post_export_is_current(draft, valid)
    assert not _slide_post_export_is_current(
        draft, {**valid, "slides": [valid["slides"][0], valid["slides"][0]]}
    )
    assert not _slide_post_export_is_current(
        draft, {**valid, "slides": [{"asset_id": str(draft.slides[0].asset_id)}]}
    )
    assert not _slide_post_export_is_current(
        draft.model_copy(update={"rendered_version": 2}), valid
    )
