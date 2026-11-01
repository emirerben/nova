"""Slide tilt (KRI-1138): save preservation, gating, freshness and the chat-edit backfill.

Failure modes (listed before the code was written):
 R1  an older app / the web editor saves a slide without `tilt` and the stored tilt vanishes
 R2  the kill switch is off but a PUT still writes or clears a tilt
 R3  a build that cannot decode tilt (protocol < 28) saves one without a 426, or is offered the
     capability
 R4  an export rendered by a worker that predates tilt reads as current (it would be upright)
 R5  the chat-edit route rebuilds a slide from a client draft and drops the stored tilt
 R6  the dispatch stamp moves (a mixed deploy would reject queued jobs) or the export stamp is
     lower than an older feature's
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.agents._schemas.edit_format import (
    SLIDES_DRAWING_RENDERER_VERSION,
    SLIDES_RENDERER_VERSION,
    SLIDES_STICKER_RENDERER_VERSION,
    SLIDES_TILT_RENDERER_VERSION,
)
from app.config import settings
from app.pipeline.slide_post.build import SLIDE_IMAGE_NORMALIZER_VERSION, SLIDE_WATERMARK_VERSION
from app.routes import plan_items
from app.schemas.slide_post import SlideEdits, SlidePostDraft, SlideRef

TILT = {"rotation_deg": 12.5, "backdrop": {"kind": "blur"}}


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4())


def _item(asset_id: uuid.UUID, edits: SlideEdits | None) -> SimpleNamespace:
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="slide", asset_id=asset_id, kind="image", edits=edits)],
        version=2,
    )
    return SimpleNamespace(
        id=uuid.uuid4(), edit_format="slides", slide_post=draft.model_dump(mode="json")
    )


def _body(asset_id: uuid.UUID, edits: dict | None = None, version: int = 2):
    slide: dict = {"id": "slide", "asset_id": str(asset_id), "kind": "image"}
    if edits is not None:
        slide["edits"] = edits
    return plan_items.SlidePostDraftBody.model_validate(
        {"platform_profile": "tiktok_photo", "slides": [slide], "expected_version": version}
    )


def _saved(item: SimpleNamespace):
    return (item.slide_post["slides"][0].get("edits") or {}).get("tilt")


async def _put(monkeypatch, item, body, *, protocol: int | None = 28):
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "_owned_ready_slide_assets", AsyncMock(return_value=[]))
    monkeypatch.setattr(plan_items, "_validate_slide_ref_ownership", lambda *_a: None)
    monkeypatch.setattr(plan_items, "_maybe_rebuild_slide_post", AsyncMock())
    monkeypatch.setattr(plan_items, "flag_modified", lambda *_a: None)
    monkeypatch.setattr(plan_items, "plan_item_response", lambda it: it)
    await plan_items.put_slide_post_draft(str(item.id), body, _user(), AsyncMock(), False, protocol)


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "media_tilt_enabled", True)


# ── R1 / R2: the three-state save contract and the kill switch ───────────────────────────


@pytest.mark.asyncio
async def test_missing_key_keeps_null_clears_and_a_value_sets(monkeypatch) -> None:
    asset_id = uuid.uuid4()
    item = _item(asset_id, SlideEdits(tilt=TILT))
    # An older build (or the web editor) re-encodes the slide without the key it cannot see.
    await _put(monkeypatch, item, _body(asset_id, {"look_preset": "olive_film"}), protocol=27)
    assert _saved(item)["rotation_deg"] == 12.5

    item.slide_post["version"] = 3
    await _put(
        monkeypatch,
        item,
        _body(asset_id, {"tilt": {"rotation_deg": -40}}, version=3),
    )
    assert _saved(item) == {"rotation_deg": -40.0, "backdrop": {"kind": "blank"}}

    item.slide_post["version"] = 4
    await _put(monkeypatch, item, _body(asset_id, {"tilt": None}, version=4))
    assert _saved(item) is None


@pytest.mark.asyncio
async def test_an_edits_null_save_from_the_web_editor_keeps_the_tilt(monkeypatch) -> None:
    asset_id = uuid.uuid4()
    item = _item(asset_id, SlideEdits(tilt=TILT))
    await _put(monkeypatch, item, _body(asset_id, None), protocol=None)
    assert _saved(item)["rotation_deg"] == 12.5


@pytest.mark.asyncio
async def test_a_legacy_text_save_keeps_the_tilt(monkeypatch) -> None:
    from app.schemas.slide_post import SlideTextElement

    asset_id = uuid.uuid4()
    item = _item(
        asset_id,
        SlideEdits(tilt=TILT, texts=[SlideTextElement(id="t1", text="Hi")]),
    )
    # The web editor sends the legacy `text` mirror and no `texts`, `tilt` or `fit`.
    sent = {"text": {"content": "Hello", "position": "bottom"}, "look_preset": "none"}
    await _put(monkeypatch, item, _body(asset_id, sent), protocol=None)
    assert _saved(item)["rotation_deg"] == 12.5


@pytest.mark.asyncio
async def test_flag_off_the_stored_tilt_wins_and_none_can_be_added(monkeypatch) -> None:
    monkeypatch.setattr(settings, "media_tilt_enabled", False)
    asset_id = uuid.uuid4()
    stored = _item(asset_id, SlideEdits(tilt=TILT))
    await _put(monkeypatch, stored, _body(asset_id, {"tilt": {"rotation_deg": 90}}))
    assert _saved(stored)["rotation_deg"] == 12.5
    stored.slide_post["version"] = 3
    await _put(monkeypatch, stored, _body(asset_id, {"tilt": None}, version=3))
    assert _saved(stored) is not None
    clean = _item(asset_id, None)
    await _put(monkeypatch, clean, _body(asset_id, {"tilt": TILT}))
    assert _saved(clean) is None


# ── R3: protocol gate (426) and the capability ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [None, 8, 27])
async def test_a_save_carrying_a_tilt_from_an_old_build_is_refused_with_426(
    monkeypatch, protocol
) -> None:
    asset_id = uuid.uuid4()
    item = _item(asset_id, None)
    with pytest.raises(HTTPException) as exc:
        await _put(monkeypatch, item, _body(asset_id, {"tilt": TILT}), protocol=protocol)
    assert exc.value.status_code == 426
    assert exc.value.detail == {"code": "client_update_required", "feature": "slide_tilt"}
    assert _saved(item) is None


@pytest.mark.asyncio
async def test_old_builds_may_still_save_slides_without_a_tilt(monkeypatch) -> None:
    asset_id = uuid.uuid4()
    item = _item(asset_id, SlideEdits(tilt=TILT))
    await _put(monkeypatch, item, _body(asset_id, {"look_preset": "none"}), protocol=None)
    assert item.slide_post["version"] == 3


def test_defaults_and_wire_shape() -> None:
    from app.routes.creation_threads import CreationCapabilitiesOut

    fresh = type(settings)(_env_file=None)
    assert fresh.media_tilt_enabled is False
    assert settings.media_tilt_min_client_protocol == 28
    assert CreationCapabilitiesOut.model_fields["slide_post_tilt"].default is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "protocol", "expected"),
    [
        (True, None, False),
        (True, 27, False),
        (True, 28, True),
        (True, 29, True),
        (False, 28, False),
    ],
)
async def test_capability_needs_the_flag_and_a_protocol_28_build(
    monkeypatch, flag, protocol, expected
) -> None:
    from app.routes.creation_threads import capabilities

    monkeypatch.setattr(settings, "media_tilt_enabled", flag)
    caps = await capabilities(_user(), False, protocol)
    assert caps["slide_post_tilt"] is expected


# ── R4 / R6: the freshness fence ─────────────────────────────────────────────────────────


def _variant(renderer_version: int) -> dict:
    return {
        "render_status": "ready",
        "slides": [
            {"asset_id": str(uuid.UUID(int=1)), "asset_gcs_path": "generative-jobs/j/n.jpg"}
        ],
        "slide_post": {
            "validation": {"ok": True, "errors": []},
            "normalizer_version": SLIDE_IMAGE_NORMALIZER_VERSION,
            "watermark_version": SLIDE_WATERMARK_VERSION,
            "renderer_version": renderer_version,
        },
    }


def _draft(edits: SlideEdits | None) -> SlidePostDraft:
    return SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="a", asset_id=uuid.UUID(int=1), kind="image", edits=edits)],
        version=2,
        rendered_version=2,
    )


def test_a_tilted_draft_is_stale_below_the_tilt_renderer() -> None:
    current = plan_items._slide_post_export_is_current
    tilted = _draft(SlideEdits(tilt=TILT))
    assert not current(tilted, _variant(SLIDES_STICKER_RENDERER_VERSION))
    assert not current(tilted, _variant(SLIDES_DRAWING_RENDERER_VERSION))
    assert not current(
        tilted,
        {**_variant(6), "slide_post": {**_variant(6)["slide_post"], "renderer_version": None}},
    )
    assert current(tilted, _variant(SLIDES_TILT_RENDERER_VERSION))
    # An untilted draft is current whatever the worker version (no mass re-render on deploy).
    assert current(_draft(None), _variant(2))
    assert current(_draft(SlideEdits(tilt={"rotation_deg": 0})), _variant(2))


def test_the_dispatch_stamp_stays_and_the_export_stamp_is_the_newest() -> None:
    assert SLIDES_RENDERER_VERSION == 2
    assert SLIDES_TILT_RENDERER_VERSION == 6 > SLIDES_STICKER_RENDERER_VERSION


# ── R5: the chat-edit backfill ───────────────────────────────────────────────────────────


def test_chat_edit_backfill_restores_an_omitted_tilt_and_respects_null() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    stored = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[
            SlideRef(id="a", asset_id=a, kind="image", edits=SlideEdits(tilt=TILT)),
            SlideRef(id="b", asset_id=b, kind="video", edits=SlideEdits(tilt=TILT)),
        ],
        version=2,
    )
    sent = SlidePostDraft.model_validate(
        {
            "platform_profile": "tiktok_photo",
            "version": 2,
            "slides": [
                {"id": "a", "asset_id": str(a), "kind": "image"},
                {"id": "b", "asset_id": str(b), "kind": "video", "edits": {"tilt": None}},
            ],
        }
    )
    out = plan_items._backfill_slide_post_sizes(sent, stored)
    assert out.slides[0].edits is not None and out.slides[0].edits.tilt is not None
    assert out.slides[1].edits is None or out.slides[1].edits.tilt is None
