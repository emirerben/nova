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

from app.schemas.slide_post import (
    MAX_SLIDE_TEXT_LENGTH,
    SlideEdits,
    SlidePostDraft,
    SlideRef,
    TextOverlay,
    parse_slide_post,
)


def test_slide_ref_without_edits_defaults_to_none() -> None:
    ref = SlideRef(id="a", asset_id=uuid.uuid4(), kind="image")
    assert ref.edits is None


def test_slide_ref_with_text_and_look_preset_round_trips() -> None:
    ref = SlideRef(
        id="a",
        asset_id=uuid.uuid4(),
        kind="image",
        edits=SlideEdits(
            text=TextOverlay(content="sold out", position="bottom"),
            look_preset="olive_film",
        ),
    )
    dumped = ref.model_dump(mode="json")
    restored = SlideRef.model_validate(dumped)
    assert restored.edits is not None
    assert restored.edits.text is not None
    assert restored.edits.text.content == "sold out"
    assert restored.edits.text.position == "bottom"
    assert restored.edits.look_preset == "olive_film"


def test_slide_edits_default_look_preset_is_none() -> None:
    assert SlideEdits().look_preset == "none"
    assert SlideEdits().text is None


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
