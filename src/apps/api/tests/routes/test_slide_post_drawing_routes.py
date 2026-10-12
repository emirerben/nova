"""Static slide drawings (KRI-589 PR2): save preservation, gating, export freshness
and the chat/propose paths that rebuild a draft.

Failure modes (listed before the code was written):
 R1  an older app / the web editor saves a slide without `drawing` and the creator's ink vanishes
 R2  the kill switch is off but a PUT still writes or clears ink
 R3  the capability is offered to a build that cannot paint/round-trip it (protocol < 7)
 R4  an export rendered by a worker that predates drawings reads as current
 R5  a chat edit (text/look/framing on a drawn slide) rebuilds the slide and drops the ink
 R6  an AI re-proposal drops the ink
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.edit_format import (
    SLIDES_DRAWING_RENDERER_VERSION,
    SLIDES_SIZES_RENDERER_VERSION,
)
from app.config import settings
from app.pipeline.slide_post.build import SLIDE_IMAGE_NORMALIZER_VERSION, SLIDE_WATERMARK_VERSION
from app.routes import plan_items
from app.schemas.slide_post import (
    SlideDrawing,
    SlideEdits,
    SlidePostDraft,
    SlideRef,
    SlideTextElement,
)

DRAWING = {
    "canvas_aspect": 0.5625,
    "strokes": [
        {
            "color": "#FF3B30",
            "opacity": 1.0,
            "width_frac": 0.02,
            "texture": "pen",
            "seed": 5,
            "points": [[0.1, 0.5], [0.5, 0.2], [0.9, 0.5]],
        }
    ],
}
OTHER = {**DRAWING, "canvas_aspect": 1.0}


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4())


def _wire_put(monkeypatch: pytest.MonkeyPatch, item: SimpleNamespace) -> None:
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "_owned_ready_slide_assets", AsyncMock(return_value=[]))
    monkeypatch.setattr(plan_items, "_validate_slide_ref_ownership", lambda *_a: None)
    monkeypatch.setattr(plan_items, "_maybe_rebuild_slide_post", AsyncMock())
    monkeypatch.setattr(plan_items, "flag_modified", lambda *_a: None)
    monkeypatch.setattr(plan_items, "plan_item_response", lambda it: it)


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


def _saved_drawing(item: SimpleNamespace):
    return (item.slide_post["slides"][0].get("edits") or {}).get("drawing")


@pytest.fixture()
def _drawing_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "slide_post_drawing_enabled", True)


async def _put(monkeypatch, item, body) -> None:
    _wire_put(monkeypatch, item)
    await plan_items.put_slide_post_draft(str(item.id), body, _user(), AsyncMock())


# ── R1: the three-state save contract ────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_save_without_the_key_keeps_the_stored_drawing(monkeypatch, _drawing_on) -> None:
    asset_id = uuid.uuid4()
    item = _item(asset_id, SlideEdits(drawing=DRAWING))
    # An older iOS build / the web editor: `edits` present but no `drawing` key.
    await _put(monkeypatch, item, _body(asset_id, {"look_preset": "olive_film"}))
    assert _saved_drawing(item) == SlideDrawing.model_validate(DRAWING).model_dump(mode="json")
    assert item.slide_post["slides"][0]["edits"]["look_preset"] == "olive_film"
    assert item.slide_post["version"] == 3


@pytest.mark.asyncio
async def test_null_edits_keep_the_stored_drawing(monkeypatch, _drawing_on) -> None:
    asset_id = uuid.uuid4()
    item = _item(asset_id, SlideEdits(drawing=DRAWING))
    await _put(monkeypatch, item, _body(asset_id))
    assert _saved_drawing(item) is not None


@pytest.mark.asyncio
async def test_explicit_null_clears_and_a_value_sets(monkeypatch, _drawing_on) -> None:
    asset_id = uuid.uuid4()
    item = _item(asset_id, SlideEdits(drawing=DRAWING))
    await _put(monkeypatch, item, _body(asset_id, {"drawing": None}))
    assert _saved_drawing(item) is None

    item.slide_post["version"] = 3
    body = plan_items.SlidePostDraftBody.model_validate(
        {
            "platform_profile": "tiktok_photo",
            "slides": [
                {
                    "id": "slide",
                    "asset_id": str(asset_id),
                    "kind": "image",
                    "edits": {"drawing": OTHER},
                }
            ],
            "expected_version": 3,
        }
    )
    await _put(monkeypatch, item, body)
    assert _saved_drawing(item)["canvas_aspect"] == 1.0


@pytest.mark.asyncio
async def test_a_web_style_legacy_text_save_keeps_the_drawing(monkeypatch, _drawing_on) -> None:
    # No `texts` key: the legacy-merge path rebuilds the slide's edits from scratch.
    asset_id = uuid.uuid4()
    item = _item(
        asset_id,
        SlideEdits(
            drawing=DRAWING, texts=[SlideTextElement(id="t1", text="Lisbon", position="bottom")]
        ),
    )
    await _put(
        monkeypatch,
        item,
        _body(asset_id, {"text": {"content": "Porto", "position": "bottom"}}),
    )
    saved = item.slide_post["slides"][0]["edits"]
    assert saved["texts"][0]["text"] == "Porto"
    assert saved["drawing"] is not None


@pytest.mark.asyncio
async def test_a_native_save_with_texts_and_a_new_drawing_takes_both(
    monkeypatch, _drawing_on
) -> None:
    asset_id = uuid.uuid4()
    item = _item(
        asset_id, SlideEdits(texts=[SlideTextElement(id="t1", text="Lisbon", position="bottom")])
    )
    await _put(
        monkeypatch,
        item,
        _body(asset_id, {"texts": [{"id": "t1", "text": "Lisbon"}], "drawing": DRAWING}),
    )
    assert _saved_drawing(item) is not None


# ── R2: the kill switch ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_the_flag_off_the_stored_drawing_wins_both_ways(monkeypatch) -> None:
    monkeypatch.setattr(settings, "slide_post_drawing_enabled", False)
    asset_id = uuid.uuid4()
    stored = _item(asset_id, SlideEdits(drawing=DRAWING))
    # A new value and an explicit clear are both ignored.
    await _put(monkeypatch, stored, _body(asset_id, {"drawing": OTHER}))
    assert _saved_drawing(stored)["canvas_aspect"] == 0.5625
    stored.slide_post["version"] = 3
    clear = plan_items.SlidePostDraftBody.model_validate(
        {
            "platform_profile": "tiktok_photo",
            "slides": [
                {
                    "id": "slide",
                    "asset_id": str(asset_id),
                    "kind": "image",
                    "edits": {"drawing": None},
                }
            ],
            "expected_version": 3,
        }
    )
    await _put(monkeypatch, stored, clear)
    assert _saved_drawing(stored) is not None
    # ...and a post that never had one cannot gain one.
    clean = _item(asset_id, None)
    await _put(monkeypatch, clean, _body(asset_id, {"drawing": DRAWING}))
    assert _saved_drawing(clean) is None


def test_defaults_and_wire_shape() -> None:
    from app.routes.creation_threads import CreationCapabilitiesOut

    assert settings.slide_post_drawing_enabled is True
    assert settings.slide_post_drawing_min_client_protocol == 7
    assert CreationCapabilitiesOut.model_fields["slide_post_drawing"].default is False


# ── R3: capability gating ────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "protocol", "expected"),
    [
        (True, None, False),
        (True, 4, False),
        (True, 5, False),
        (True, 6, False),
        (True, 7, True),
        (True, 8, True),
        (False, 7, False),
        (False, None, False),
    ],
)
async def test_capability_needs_the_flag_and_a_protocol_7_build(
    monkeypatch, flag: bool, protocol: int | None, expected: bool
) -> None:
    from app.routes.creation_threads import capabilities

    monkeypatch.setattr(settings, "slide_post_drawing_enabled", flag)
    caps = await capabilities(_user(), False, protocol)
    assert caps["slide_post_drawing"] is expected


# ── R4: export freshness ─────────────────────────────────────────────────────


def _variant(stamp: int | None) -> dict:
    slide_post: dict = {
        "validation": {"errors": []},
        "normalizer_version": SLIDE_IMAGE_NORMALIZER_VERSION,
        "watermark_version": SLIDE_WATERMARK_VERSION,
    }
    if stamp is not None:
        slide_post["renderer_version"] = stamp
    return {"render_status": "ready", "slide_post": slide_post}


def test_a_drawn_draft_is_stale_below_the_drawing_renderer() -> None:
    asset_id = uuid.uuid4()

    def draft(edits):
        return SlidePostDraft(
            platform_profile="tiktok_photo",
            slides=[SlideRef(id="a", asset_id=asset_id, kind="image", edits=edits)],
            version=3,
            rendered_version=3,
        )

    def current(d, stamp):
        variant = {
            **_variant(stamp),
            "slides": [{"asset_id": str(asset_id), "asset_gcs_path": "private/a.jpg"}],
        }
        return plan_items._slide_post_export_is_current(d, variant)

    drawn, plain = draft(SlideEdits(drawing=DRAWING)), draft(SlideEdits())
    assert SLIDES_DRAWING_RENDERER_VERSION == 4 > SLIDES_SIZES_RENDERER_VERSION
    for stale in (None, 2, SLIDES_SIZES_RENDERER_VERSION):
        assert current(drawn, stale) is False
    assert current(drawn, SLIDES_DRAWING_RENDERER_VERSION) is True
    # A draft without ink is current at any version, exactly as before.
    for stamp in (None, 2, SLIDES_SIZES_RENDERER_VERSION, SLIDES_DRAWING_RENDERER_VERSION):
        assert current(plain, stamp) is True


# ── chat-edit backfill (the route restores keys an older editor draft omitted) ──


def test_chat_edit_backfill_restores_an_omitted_drawing_and_respects_an_explicit_null() -> None:
    asset_id = uuid.uuid4()
    stored = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[
            SlideRef(id="a", asset_id=asset_id, kind="image", edits=SlideEdits(drawing=DRAWING)),
            SlideRef(
                id="b", asset_id=uuid.uuid4(), kind="image", edits=SlideEdits(drawing=DRAWING)
            ),
        ],
        version=2,
    )
    sent = SlidePostDraft.model_validate(
        {
            "platform_profile": "tiktok_photo",
            "version": 2,
            "slides": [
                {"id": "a", "asset_id": str(asset_id), "kind": "image"},
                {
                    "id": "b",
                    "asset_id": str(stored.slides[1].asset_id),
                    "kind": "image",
                    "edits": {"drawing": None},
                },
            ],
        }
    )
    out = plan_items._backfill_slide_post_sizes(sent, stored)
    assert out.slides[0].edits is not None and out.slides[0].edits.drawing is not None
    assert out.slides[1].edits is not None and out.slides[1].edits.drawing is None


# ── R5: the chat copilot rebuilds slides ─────────────────────────────────────


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
def test_a_chat_change_to_a_drawn_slide_keeps_its_ink(op) -> None:
    h = _chat_helpers()
    assets = [h._asset(), h._asset()]
    draft = h._draft(assets, edits={0: SlideEdits(drawing=DRAWING)})
    compiled, _ = h._edit(draft, assets, [op])
    edits = compiled.draft.slides[0].edits
    assert edits is not None
    # The slide really was rebuilt (that is the path that used to drop fields)...
    assert edits.texts or edits.look_preset != "none"
    # ...and the ink came through untouched.
    assert edits.drawing == SlideDrawing.model_validate(DRAWING)
    assert compiled.draft.slides[1].edits is None


def test_a_reorder_keeps_ink_with_its_slide(monkeypatch) -> None:
    h = _chat_helpers()
    h._facts_on(monkeypatch)
    assets = [h._asset(time="2024-07-03T10:00:00Z"), h._asset(time="2024-07-01T10:00:00Z")]
    draft = h._draft(assets, edits={0: SlideEdits(drawing=DRAWING)})
    compiled, _ = h._edit(draft, assets, [h.CHRONO])
    by_id = {s.id: s for s in compiled.draft.slides}
    assert by_id["s0"].edits is not None and by_id["s0"].edits.drawing is not None
    assert [s.id for s in compiled.draft.slides] == ["s1", "s0"]


def test_the_snapshot_flags_drawn_slides_and_is_unchanged_without_drawings() -> None:
    from app.services.slide_post_chat_edit import build_slide_post_snapshot

    h = _chat_helpers()
    assets = [h._asset(), h._asset()]
    drawn = build_slide_post_snapshot(
        h._draft(assets, edits={1: SlideEdits(drawing=DRAWING)}), h._by_id(assets)
    )
    assert [s.get("has_drawing") for s in drawn["slots"]] == [None, True]
    plain = build_slide_post_snapshot(h._draft(assets), h._by_id(assets))
    assert all("has_drawing" not in slot for slot in plain["slots"])


# ── R6: propose keeps manual edits ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_proposal_keeps_a_drawn_slide(monkeypatch) -> None:
    from app.services.slide_post_compose import propose_slide_post_draft

    def _raise(*_a, **_k):
        raise RuntimeError("model client unavailable in tests")

    monkeypatch.setattr("app.agents._model_client.default_client", _raise)
    assets = [
        SimpleNamespace(id=uuid.uuid4(), kind="image", analysis=None, user_context=None)
        for _ in range(2)
    ]
    current = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[
            SlideRef(
                id="mine", asset_id=assets[0].id, kind="image", edits=SlideEdits(drawing=DRAWING)
            ),
            SlideRef(id="b", asset_id=assets[1].id, kind="image"),
        ],
        version=2,
    )
    result = await propose_slide_post_draft(
        item=SimpleNamespace(id=uuid.uuid4(), theme="", idea="", notes=""),
        assets=assets,
        platform_profile="tiktok_photo",
        previous_version=2,
        current_draft=current,
    )
    drawn = next(s for s in result.draft.slides if s.id == "mine")
    assert drawn.edits is not None and drawn.edits.drawing is not None
