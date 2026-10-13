"""Slide stickers (KRI-647 / KRI-460): save preservation, gating, freshness, the
sticker-asset-is-never-a-slide rule and the chat paths that rebuild a draft.

Failure modes (listed before the code was written):
 T1  an older app / the web editor saves a slide without `stickers` and the stickers vanish
 T2  the kill switch is off but a PUT still writes or clears stickers
 T3  a build that cannot decode stickers (protocol < 8) saves them without a 426, or the
     capability is offered to it
 T4  a sticker pointing at someone else's / a non-image / an unready asset is accepted
 T5  an export rendered by a worker that predates stickers reads as current
 T6  a chat edit on a stickered slide rebuilds it and drops the stickers
 T7  a sticker's pool image becomes a slide (compose/propose/PUT) or counts as a visual
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.agents._schemas.edit_format import (
    SLIDES_DRAWING_RENDERER_VERSION,
    SLIDES_STICKER_RENDERER_VERSION,
)
from app.config import settings
from app.pipeline.slide_post.build import SLIDE_IMAGE_NORMALIZER_VERSION, SLIDE_WATERMARK_VERSION
from app.routes import plan_items
from app.schemas.slide_post import SlideEdits, SlidePostDraft, SlideRef, sticker_asset_ids

POOL_IMAGE = uuid.uuid4()


def _sticker(**kw) -> dict:
    base = {
        "id": "st1",
        "asset_id": str(POOL_IMAGE),
        "x_frac": 0.5,
        "y_frac": 0.4,
        "width_frac": 0.3,
        "rotation_deg": 12,
    }
    return {**base, **kw}


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


def _body(asset_id: uuid.UUID, edits: dict | None = None):
    slide: dict = {"id": "slide", "asset_id": str(asset_id), "kind": "image"}
    if edits is not None:
        slide["edits"] = edits
    return plan_items.SlidePostDraftBody.model_validate(
        {"platform_profile": "tiktok_photo", "slides": [slide], "expected_version": 2}
    )


def _saved(item: SimpleNamespace):
    return (item.slide_post["slides"][0].get("edits") or {}).get("stickers")


def _wire(monkeypatch: pytest.MonkeyPatch, item: SimpleNamespace, *, validate: bool = False):
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "_owned_ready_slide_assets", AsyncMock(return_value=[]))
    monkeypatch.setattr(plan_items, "_validate_slide_ref_ownership", lambda *_a: None)
    monkeypatch.setattr(plan_items, "_maybe_rebuild_slide_post", AsyncMock())
    monkeypatch.setattr(plan_items, "flag_modified", lambda *_a: None)
    monkeypatch.setattr(plan_items, "plan_item_response", lambda it: it)
    if not validate:
        monkeypatch.setattr(plan_items, "_validate_slide_stickers", AsyncMock())


async def _put(monkeypatch, item, body, *, protocol: int | None = 8, validate=False, db=None):
    _wire(monkeypatch, item, validate=validate)
    await plan_items.put_slide_post_draft(
        str(item.id), body, _user(), db or AsyncMock(), False, protocol
    )


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "slide_post_stickers_enabled", True)


# ── T1 / T2: the three-state save contract and the kill switch ───────────────────────────


@pytest.mark.asyncio
async def test_missing_key_keeps_null_clears_and_a_value_sets(monkeypatch) -> None:
    asset_id = uuid.uuid4()
    item = _item(asset_id, SlideEdits(stickers=[_sticker()]))
    await _put(monkeypatch, item, _body(asset_id, {"look_preset": "olive_film"}), protocol=7)
    assert _saved(item)[0]["id"] == "st1"  # an older build that dropped the key

    item.slide_post["version"] = 3
    body = plan_items.SlidePostDraftBody.model_validate(
        {
            "platform_profile": "tiktok_photo",
            "slides": [
                {
                    "id": "slide",
                    "asset_id": str(asset_id),
                    "kind": "image",
                    "edits": {"stickers": [_sticker(id="new", x_frac=0.1)]},
                }
            ],
            "expected_version": 3,
        }
    )
    await _put(monkeypatch, item, body)
    assert [s["id"] for s in _saved(item)] == ["new"]

    item.slide_post["version"] = 4
    cleared = plan_items.SlidePostDraftBody.model_validate(
        {
            "platform_profile": "tiktok_photo",
            "slides": [
                {
                    "id": "slide",
                    "asset_id": str(asset_id),
                    "kind": "image",
                    "edits": {"stickers": None},
                }
            ],
            "expected_version": 4,
        }
    )
    await _put(monkeypatch, item, cleared)
    assert _saved(item) is None


@pytest.mark.asyncio
async def test_an_empty_list_clears_while_a_missing_key_keeps(monkeypatch) -> None:
    # iOS sends `"stickers": []` when the last sticker is removed and omits the key when nil.
    asset_id = uuid.uuid4()
    item = _item(asset_id, SlideEdits(stickers=[_sticker()]))
    await _put(monkeypatch, item, _body(asset_id, {"stickers": []}))
    assert _saved(item) is None  # stored form omits the key entirely

    keep = _item(asset_id, SlideEdits(stickers=[_sticker()]))
    await _put(monkeypatch, keep, _body(asset_id, {"look_preset": "olive_film"}))
    assert [s["id"] for s in _saved(keep)] == ["st1"]
    # An empty list needs no protocol-8 build (it carries nothing to decode).
    gated = _item(asset_id, SlideEdits(stickers=[_sticker()]))
    await _put(monkeypatch, gated, _body(asset_id, {"stickers": []}), protocol=None)
    assert _saved(gated) is None


@pytest.mark.asyncio
async def test_a_legacy_text_save_keeps_the_stickers(monkeypatch) -> None:
    from app.schemas.slide_post import SlideTextElement

    asset_id = uuid.uuid4()
    item = _item(
        asset_id,
        SlideEdits(
            stickers=[_sticker()],
            texts=[SlideTextElement(id="t1", text="Lisbon", position="bottom")],
        ),
    )
    await _put(
        monkeypatch,
        item,
        _body(asset_id, {"text": {"content": "Porto", "position": "bottom"}}),
        protocol=7,
    )
    saved = item.slide_post["slides"][0]["edits"]
    assert saved["texts"][0]["text"] == "Porto" and saved["stickers"][0]["id"] == "st1"


@pytest.mark.asyncio
async def test_flag_off_the_stored_stickers_win_and_none_can_be_added(monkeypatch) -> None:
    monkeypatch.setattr(settings, "slide_post_stickers_enabled", False)
    asset_id = uuid.uuid4()
    stored = _item(asset_id, SlideEdits(stickers=[_sticker()]))
    await _put(monkeypatch, stored, _body(asset_id, {"stickers": [_sticker(id="other")]}))
    assert [s["id"] for s in _saved(stored)] == ["st1"]
    stored.slide_post["version"] = 3
    clear = plan_items.SlidePostDraftBody.model_validate(
        {
            "platform_profile": "tiktok_photo",
            "slides": [
                {
                    "id": "slide",
                    "asset_id": str(asset_id),
                    "kind": "image",
                    "edits": {"stickers": None},
                }
            ],
            "expected_version": 3,
        }
    )
    await _put(monkeypatch, stored, clear)
    assert _saved(stored) is not None
    clean = _item(asset_id, None)
    await _put(monkeypatch, clean, _body(asset_id, {"stickers": [_sticker()]}))
    assert _saved(clean) is None


# ── T3: protocol gate (426) and the capability ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [None, 4, 7])
async def test_a_save_carrying_stickers_from_an_old_build_is_refused_with_426(
    monkeypatch, protocol
) -> None:
    asset_id = uuid.uuid4()
    item = _item(asset_id, None)
    with pytest.raises(HTTPException) as exc:
        await _put(
            monkeypatch, item, _body(asset_id, {"stickers": [_sticker()]}), protocol=protocol
        )
    assert exc.value.status_code == 426
    assert exc.value.detail == {"code": "client_update_required", "feature": "slide_stickers"}
    assert _saved(item) is None  # nothing written


@pytest.mark.asyncio
async def test_old_builds_may_still_save_slides_without_stickers(monkeypatch) -> None:
    asset_id = uuid.uuid4()
    item = _item(asset_id, None)
    await _put(monkeypatch, item, _body(asset_id, {"look_preset": "olive_film"}), protocol=None)
    assert item.slide_post["version"] == 3


def test_defaults_and_wire_shape() -> None:
    from app.routes.creation_threads import CreationCapabilitiesOut

    fresh = type(settings)(_env_file=None)
    assert fresh.slide_post_stickers_enabled is True
    assert settings.slide_post_sticker_min_client_protocol == 8
    assert CreationCapabilitiesOut.model_fields["slide_post_stickers"].default is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "protocol", "expected"),
    [
        (True, None, False),
        (True, 7, False),
        (True, 8, True),
        (True, 9, True),
        (False, 8, False),
    ],
)
async def test_capability_needs_the_flag_and_a_protocol_8_build(
    monkeypatch, flag, protocol, expected
) -> None:
    from app.routes.creation_threads import capabilities

    monkeypatch.setattr(settings, "slide_post_stickers_enabled", flag)
    caps = await capabilities(_user(), False, protocol)
    assert caps["slide_post_stickers"] is expected


# ── T4 / T7: asset validation and "a sticker image is never a slide" ─────────────────────


def _rows_db(rows: list[uuid.UUID]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = [SimpleNamespace(id=r) for r in rows]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_a_new_sticker_must_be_a_ready_image_of_this_item(monkeypatch) -> None:
    asset_id = uuid.uuid4()
    item = _item(asset_id, None)
    body = _body(asset_id, {"stickers": [_sticker()]})
    with pytest.raises(HTTPException) as exc:
        await _put(monkeypatch, item, body, validate=True, db=_rows_db([]))
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "slide_sticker_invalid"
    assert str(POOL_IMAGE) in exc.value.detail["message"]
    assert _saved(item) is None

    await _put(monkeypatch, item, body, validate=True, db=_rows_db([POOL_IMAGE]))
    assert _saved(item)[0]["asset_id"] == str(POOL_IMAGE)


@pytest.mark.asyncio
async def test_a_stored_sticker_whose_image_was_deleted_does_not_block_saves(monkeypatch) -> None:
    asset_id = uuid.uuid4()
    item = _item(asset_id, SlideEdits(stickers=[_sticker()]))
    db = _rows_db([])  # the pool image is gone
    await _put(
        monkeypatch,
        item,
        _body(asset_id, {"stickers": [_sticker()], "look_preset": "olive_film"}),
        validate=True,
        db=db,
    )
    assert item.slide_post["version"] == 3
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_sticker_image_cannot_also_be_a_slide(monkeypatch) -> None:
    asset_id = uuid.uuid4()
    item = _item(asset_id, None)
    body = _body(asset_id, {"stickers": [_sticker(asset_id=str(asset_id))]})
    with pytest.raises(HTTPException) as exc:
        await _put(monkeypatch, item, body, validate=True, db=_rows_db([asset_id]))
    assert exc.value.status_code == 422 and "cannot also be a slide" in exc.value.detail["message"]


def test_compose_and_propose_never_turn_a_sticker_image_into_a_slide() -> None:
    other = uuid.uuid4()
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[
            SlideRef(
                id="s",
                asset_id=other,
                kind="image",
                edits=SlideEdits(stickers=[_sticker()]),
            )
        ],
    )
    pool = [SimpleNamespace(id=other), SimpleNamespace(id=POOL_IMAGE)]
    # The default pool (no explicit ids) drops the sticker image...
    kept = plan_items._without_sticker_assets(draft, pool, None)
    assert [a.id for a in kept] == [other]
    # ...naming it explicitly is a 422, and a draft without stickers is untouched.
    with pytest.raises(HTTPException) as exc:
        plan_items._without_sticker_assets(draft, pool, [POOL_IMAGE])
    assert exc.value.status_code == 422
    assert plan_items._without_sticker_assets(None, pool, None) == pool
    assert sticker_asset_ids(draft) == {POOL_IMAGE}


# ── T5: export freshness ─────────────────────────────────────────────────────────────────


def test_a_stickered_draft_is_stale_below_the_sticker_renderer() -> None:
    asset_id = uuid.uuid4()

    def current(edits, stamp):
        draft = SlidePostDraft(
            platform_profile="tiktok_photo",
            slides=[SlideRef(id="a", asset_id=asset_id, kind="image", edits=edits)],
            version=3,
            rendered_version=3,
        )
        slide_post: dict = {
            "validation": {"errors": []},
            "normalizer_version": SLIDE_IMAGE_NORMALIZER_VERSION,
            "watermark_version": SLIDE_WATERMARK_VERSION,
        }
        if stamp is not None:
            slide_post["renderer_version"] = stamp
        variant = {
            "render_status": "ready",
            "slide_post": slide_post,
            "slides": [{"asset_id": str(asset_id), "asset_gcs_path": "private/a.jpg"}],
        }
        return plan_items._slide_post_export_is_current(draft, variant)

    assert SLIDES_STICKER_RENDERER_VERSION == 5 > SLIDES_DRAWING_RENDERER_VERSION
    stickered = SlideEdits(stickers=[_sticker()])
    for stale in (None, 2, 3, SLIDES_DRAWING_RENDERER_VERSION):
        assert current(stickered, stale) is False
    assert current(stickered, SLIDES_STICKER_RENDERER_VERSION) is True
    # No stickers: current at any version, exactly as before.
    for stamp in (None, 2, 3, 4, 5):
        assert current(SlideEdits(), stamp) is True


def test_chat_edit_backfill_restores_omitted_stickers_and_respects_null() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    stored = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[
            SlideRef(id="a", asset_id=a, kind="image", edits=SlideEdits(stickers=[_sticker()])),
            SlideRef(id="b", asset_id=b, kind="image", edits=SlideEdits(stickers=[_sticker()])),
        ],
        version=2,
    )
    sent = SlidePostDraft.model_validate(
        {
            "platform_profile": "tiktok_photo",
            "version": 2,
            "slides": [
                {"id": "a", "asset_id": str(a), "kind": "image"},
                {"id": "b", "asset_id": str(b), "kind": "image", "edits": {"stickers": None}},
            ],
        }
    )
    out = plan_items._backfill_slide_post_sizes(sent, stored)
    assert out.slides[0].edits is not None and out.slides[0].edits.stickers
    assert out.slides[1].edits is not None and not out.slides[1].edits.stickers


# ── T6: the chat copilot rebuilds slides ─────────────────────────────────────────────────


def _chat_helpers():
    from tests.services import test_slide_post_chat_edit as helpers  # noqa: PLC0415

    return helpers


@pytest.mark.parametrize(
    "op",
    [
        {"op": "add_text", "text": "Hello", "start_s": 0.0, "end_s": 1.0},
        {"op": "set_look_preset", "slot_index": 0, "look_preset": "stadium_diffusion"},
    ],
)
def test_a_chat_change_to_a_stickered_slide_keeps_its_stickers(op) -> None:
    h = _chat_helpers()
    assets = [h._asset(), h._asset()]
    draft = h._draft(assets, edits={0: SlideEdits(stickers=[_sticker(), _sticker(id="st2")])})
    compiled, _ = h._edit(draft, assets, [op])
    edits = compiled.draft.slides[0].edits
    assert edits is not None and edits.texts or edits.look_preset != "none"
    assert [s.id for s in edits.stickers or []] == ["st1", "st2"]
    assert compiled.draft.slides[1].edits is None


def test_the_snapshot_flags_stickered_slides_and_is_unchanged_without_them() -> None:
    from app.services.slide_post_chat_edit import build_slide_post_snapshot

    h = _chat_helpers()
    assets = [h._asset(), h._asset()]
    flagged = build_slide_post_snapshot(
        h._draft(assets, edits={1: SlideEdits(stickers=[_sticker()])}), h._by_id(assets)
    )
    assert [s.get("has_stickers") for s in flagged["slots"]] == [None, True]
    plain = build_slide_post_snapshot(h._draft(assets), h._by_id(assets))
    assert all("has_stickers" not in slot for slot in plain["slots"])
