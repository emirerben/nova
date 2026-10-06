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

from app.pipeline.slide_post.build import SLIDE_IMAGE_NORMALIZER_VERSION, SLIDE_WATERMARK_VERSION
from app.routes.plan_items import _slide_post_export_is_current
from app.schemas.slide_post import (
    MAX_SLIDE_TEXT_LENGTH,
    SlideEdits,
    SlidePostDraft,
    SlideRef,
    SlideTextElement,
    TextOverlay,
    merge_legacy_text_edits,
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
        "slide_post": {
            "validation": {"errors": []},
            "normalizer_version": SLIDE_IMAGE_NORMALIZER_VERSION,
            "watermark_version": SLIDE_WATERMARK_VERSION,
        },
    }
    assert _slide_post_export_is_current(draft, valid)
    # Rendered by an older decode recipe (or before the stamp existed) => stale.
    for stale in (SLIDE_IMAGE_NORMALIZER_VERSION - 1, None):
        assert not _slide_post_export_is_current(
            draft,
            {**valid, "slide_post": {**valid["slide_post"], "normalizer_version": stale}},
        )
    # Rendered before the watermark (KRI-472) or an older one => stale, so an
    # unbranded export re-renders instead of downloading.
    for stale in (SLIDE_WATERMARK_VERSION - 1, None):
        assert not _slide_post_export_is_current(
            draft,
            {**valid, "slide_post": {**valid["slide_post"], "watermark_version": stale}},
        )
    assert not _slide_post_export_is_current(
        draft, {**valid, "slides": [valid["slides"][0], valid["slides"][0]]}
    )
    assert not _slide_post_export_is_current(
        draft, {**valid, "slides": [{"asset_id": str(draft.slides[0].asset_id)}]}
    )
    assert not _slide_post_export_is_current(
        draft.model_copy(update={"rendered_version": 2}), valid
    )


class TestSlideRichText:
    def test_legacy_jsonb_without_texts_parses(self):
        edits = SlideEdits.model_validate({"text": {"content": "Hi", "position": "top"}})
        assert edits.texts is None and edits.text is not None
        lifted = edits.effective_texts()
        assert len(lifted) == 1 and lifted[0].background == "box" and lifted[0].text == "Hi"
        assert lifted[0].position == "top"

    def test_texts_mirror_into_legacy_text(self):
        edits = SlideEdits(
            texts=[
                SlideTextElement(id="a", text="One", position="top"),
                SlideTextElement(id="b", text="Two"),
            ]
        )
        assert edits.text == TextOverlay(content="One", position="top")
        assert SlideEdits(texts=[]).text is None

    def test_custom_position_mirrors_to_nearest_bucket(self):
        edits = SlideEdits(
            texts=[SlideTextElement(id="a", text="x", position="custom", y_frac=0.9)]
        )
        assert edits.text.position == "bottom"

    def test_bad_font_color_size_count_and_ids_rejected(self):
        with pytest.raises(ValidationError):
            SlideTextElement(id="a", text="x", font_family="Comic Sans")
        with pytest.raises(ValidationError):
            SlideTextElement(id="a", text="x", color="red")
        with pytest.raises(ValidationError):
            SlideTextElement(id="a", text="x", size_px=500)
        with pytest.raises(ValidationError):
            SlideEdits(texts=[SlideTextElement(id=str(i), text="x") for i in range(5)])
        with pytest.raises(ValidationError):
            SlideEdits(
                texts=[SlideTextElement(id="a", text="x"), SlideTextElement(id="a", text="y")]
            )
        with pytest.raises(ValidationError):
            SlideTextElement(id="a", text="x" * 121)

    def test_merge_keeps_stored_texts_when_old_client_omits_them(self):
        aid = uuid.uuid4()
        stored_edits = SlideEdits(texts=[SlideTextElement(id="a", text="Lisbon", color="#FF0000")])
        stored = SlidePostDraft(
            platform_profile="tiktok_photo",
            slides=[SlideRef(id="s", asset_id=aid, kind="image", edits=stored_edits)],
        )

        def body(edits: dict) -> SlideRef:
            return SlideRef.model_validate(
                {"id": "s", "asset_id": str(aid), "kind": "image", "edits": edits}
            )

        # Old client round-trips the mirror unchanged, no `texts` key.
        out = merge_legacy_text_edits(
            stored, [body({"text": {"content": "Lisbon", "position": "bottom"}})]
        )
        assert out[0].edits.texts[0].color == "#FF0000"
        # Old client changed the legacy text: lands in texts[0], style kept.
        out = merge_legacy_text_edits(
            stored, [body({"text": {"content": "Porto", "position": "bottom"}})]
        )
        edits = out[0].edits
        assert edits.texts[0].text == "Porto" and edits.texts[0].color == "#FF0000"
        assert edits.texts[0].edited is True and edits.text.content == "Porto"
        # Old client cleared the text: texts[0] dropped.
        assert merge_legacy_text_edits(stored, [body({"look_preset": "none"})])[0].edits.texts == []
        # Explicit texts from a new client are taken as sent.
        assert merge_legacy_text_edits(stored, [body({"texts": []})])[0].edits.texts == []
        assert merge_legacy_text_edits(None, [body({"texts": []})])[0].edits.texts == []
