"""KRI-1616 end to end: her transparent pictures as intro stickers, and cards hanging past the
frame edge, from "Try intro styles" to the pinned phone recipe, through the real app, real
PostgreSQL and real Skia (stage B's harness: only auth and the storage download are replaced).

Failure modes this file proves (M60-M66 are in `services/intro_props_layout.py`):
- S1 a sticker look, or bleed, reaches a build that cannot show it (63 stickers, 64 bleed), and an
  older build's offer is not today's minus the new looks;
- S2 a sticker is offered where the phone would flatten it (``alphaOverlay`` not verified);
- S3 a picture with no transparent pixel, or a pool with no transparent picture, gets a sticker
  look; a sticker is used as a photo card;
- S4 a sticker lands on the title ink, the captions, the watermark or another sticker, sits under
  a photo card, or hangs past the frame further than the offer's 35% (or at all without bleed);
- S5 the Save moves, retimes or flattens a sticker (the pinned bytes are the PNG's, the overlay
  keeps its alpha) or refuses one of her own;
- S6 her own drag further off, resize, retime or delete of a sticker is refused or clamped by an
  offer preference;
- S7 another owner's transparent picture becomes her sticker (the wrong-content control);
- S8 a Turkish title.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.cli import intro_styles_fixtures as fx
from app.config import settings
from app.main import app
from app.pipeline.render_geometry import NormalizedBox
from app.services.client_protocol import (
    INTRO_CARDS_PROTOCOL,
    INTRO_EDGE_BLEED_PROTOCOL,
    INTRO_STICKERS_PROTOCOL,
)
from app.services.intro_card_pool import card_box
from app.services.intro_props_layout import BLEED_MAX, STICKER_Z
from app.services.phone_card_layout import EDGE_INSET, WATERMARK_BOX, boxes_intersect
from app.services.phone_sources import PHONE_VISUALS_FIELD
from tests.routes.test_intro_cards_e2e import (
    CLOSE_FACE,
    ROOMY_FACE,
    Seeded,
    as_overlays,
    card_options,
)

STICKER_ASPECT = 1.0


def _png() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(image).polygon(
        [
            (100, 10),
            (125, 75),
            (190, 80),
            (138, 120),
            (155, 190),
            (100, 150),
            (45, 190),
            (62, 120),
            (10, 80),
            (75, 75),
        ],
        fill=(240, 196, 40, 255),
    )
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


PNG = _png()
JPEG = __import__("tests.routes.test_intro_cards_e2e", fromlist=["JPEG"]).JPEG


def headers(protocol: int) -> dict[str, str]:
    return {"X-Kria-Client-Protocol": str(protocol)}


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    from app import storage
    from app.limiter import limiter

    for key, value in fx.flag_overrides().items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(settings, "intro_scene_model_enabled", False)
    monkeypatch.setattr(settings, "intro_cards_enabled", True)
    monkeypatch.setattr(
        storage, "object_metadata", lambda _path: SimpleNamespace(generation="7", size=1)
    )
    downloads: list[str] = []

    def download(path, local, *, generation):
        downloads.append(path)
        assert generation == "7"
        Path(local).write_bytes(PNG if path.endswith(".png") else JPEG)

    monkeypatch.setattr(storage, "download_generation_to_file", download)
    limiter.reset()
    yield downloads
    limiter.reset()


class StickerSeeded(Seeded):
    def add_sticker(self, *, created_at, user_id=None, has_alpha=True, **overrides) -> str:
        owner = user_id or self.owner
        asset_id = uuid.uuid4()
        self.add_photo(
            created_at=created_at,
            user_id=owner,
            id=asset_id,
            gcs_path=f"users/{owner}/plan/{self.item_id}/pool/{asset_id}.png",
            upload_content_type="image/png",
            aspect=STICKER_ASPECT,
            analysis={"dims_upright": True, "has_alpha": has_alpha, "width": 200, "height": 200},
            **overrides,
        )
        return str(asset_id)


@pytest.fixture
def make(monkeypatch, request):
    made: list[StickerSeeded] = []
    stack = contextlib.ExitStack()

    def build(lang="en", *, stickers=2, has_alpha=True, **kwargs):
        job_id = uuid.uuid5(uuid.NAMESPACE_URL, f"kri1616/{request.node.nodeid}/{len(made)}")
        seeded = StickerSeeded(lang, job_id=job_id, **{"photos": 0, **kwargs})
        start = datetime(2026, 11, 2, tzinfo=UTC)
        seeded.sticker_ids = [
            seeded.add_sticker(created_at=start + timedelta(minutes=n), has_alpha=has_alpha)
            for n in range(stickers)
        ]
        made.append(seeded)
        monkeypatch.setattr(settings, "intro_scene_user_ids", [str(seeded.owner)])
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=seeded.owner, email="x@intro-stickers.test"
        )
        client = stack.enter_context(TestClient(app, raise_server_exceptions=True))
        return seeded, client

    yield build
    stack.close()
    app.dependency_overrides.clear()
    for seeded in made:
        seeded.cleanup()


def offer(client, seeded, rows=None, *, protocol=INTRO_EDGE_BLEED_PROTOCOL) -> dict:
    response = client.post(
        f"{seeded.url}/intro-styles",
        json={
            "version": 1,
            "base_generation": seeded.baseline(),
            "title_rows": seeded.rows if rows is None else rows,
        },
        headers=headers(protocol),
    )
    assert response.status_code == 200, response.text
    return response.json()


def save(client, seeded, rows, overlays, *, protocol=INTRO_EDGE_BLEED_PROTOCOL):
    return client.post(
        f"{seeded.url}/editor-commit",
        json={
            "base_generation": seeded.baseline(),
            "text_elements": rows,
            "media_overlays": overlays,
        },
        headers=headers(protocol),
    )


def sticker_looks(body: dict) -> list[dict]:
    return [o for o in card_options(body) if o["cards"][0]["slot"].startswith("sticker_")]


def photo_looks(body: dict) -> list[dict]:
    return [o for o in card_options(body) if o["cards"][0]["slot"].startswith("prop_")]


def boxes(cards: list[dict]) -> list[NormalizedBox]:
    return [card_box(c["x_frac"], c["y_frac"], c["scale"], STICKER_ASPECT) for c in cards]


def title_boxes(seeded, rows) -> list[NormalizedBox]:
    from app.pipeline.canvas import PORTRAIT
    from app.pipeline.phone_subtitled_title import _measure_title_box

    return [_measure_title_box(dict(row), canvas=PORTRAIT) for row in rows]


@pytest.mark.parametrize("lang", ["en", "tr"])
def test_s1_s4_s5_s8_a_sticker_look_is_offered_saved_and_drawn_where_it_was_offered(
    make, _flags, lang
):
    seeded, client = make(lang, face=ROOMY_FACE)
    body = offer(client, seeded)
    looks = sticker_looks(body)
    assert len(looks) == 1, body.get("options")
    look = looks[0]
    cards = look["cards"]
    assert look["key"].endswith("+stickers_edge")
    assert [c["id"] for c in cards] == ["intro-card-sticker-1", "intro-card-sticker-2"]
    assert [c["slot"] for c in cards] == ["sticker_1", "sticker_2"]
    assert [c["media_id"] for c in cards] == seeded.sticker_ids
    assert all(c["z"] >= STICKER_Z for c in cards)
    placed = boxes(cards)
    for box in placed:
        assert box.top >= EDGE_INSET - 1e-6 and box.bottom <= 1 - EDGE_INSET + 1e-6
        past = max(-box.left, box.right - 1.0, 0.0)
        assert past <= BLEED_MAX * box.width + 1e-6
        assert not boxes_intersect(box, WATERMARK_BOX)
        assert not any(boxes_intersect(box, line) for line in title_boxes(seeded, look["rows"]))
    assert any(box.left < 0 or box.right > 1 for box in placed), "a sticker hangs past the edge"
    assert not boxes_intersect(placed[0], placed[1])
    if lang == "tr":
        assert {r.get("text_language") for r in look["rows"]} == {"tr"}

    saved = save(client, seeded, look["rows"], as_overlays(cards))
    assert saved.status_code == 200, saved.text
    receipts = {r["media_id"]: r for r in seeded.job().assembly_plan[PHONE_VISUALS_FIELD]}
    for card in cards:
        assert receipts[card["media_id"]]["sha256"] == hashlib.sha256(PNG).hexdigest()
        assert receipts[card["media_id"]]["gcs_path"].endswith(".png")
    drawn, windows = seeded.drawn_cards(), seeded.drawn_windows()
    for card in cards:
        assert drawn[card["id"]] == pytest.approx((card["x_frac"], card["y_frac"], card["scale"]))
        assert windows[card["id"]] == pytest.approx((card["start_s"], card["end_s"]))
    clips = [
        clip
        for track in seeded.recipe().tracks
        for clip in track.clips
        if clip.id.startswith("subtitled-overlay-intro-card-sticker-")
    ]
    assert clips and all(clip.overlay_preserve_alpha is not False for clip in clips)
    # saved as phone-lane image cards, which the editor's source pool serves with `preserve_alpha`
    # (tests/routes/test_edge_bleed.py::test_b10_*)
    saved_rows = {row["id"]: row for row in seeded.variant()["media_overlays"]}
    assert all(
        (saved_rows[c["id"]]["kind"], saved_rows[c["id"]]["source"]) == ("image", "phone_lane")
        for c in cards
    )
    reoffer = offer(client, seeded, look["rows"])
    assert [o["key"] for o in reoffer["options"] if o["current"]] == [look["key"]]


def test_s1_a_build_before_stickers_or_bleed_gets_neither(make):
    seeded, client = make(face=ROOMY_FACE)
    assert not sticker_looks(offer(client, seeded, protocol=INTRO_CARDS_PROTOCOL))
    no_bleed = sticker_looks(offer(client, seeded, protocol=INTRO_STICKERS_PROTOCOL))
    assert len(no_bleed) == 1
    for box in boxes(no_bleed[0]["cards"]):
        assert box.left >= EDGE_INSET - 1e-6 and box.right <= 1 - EDGE_INSET + 1e-6


def test_s1_s3_photo_looks_ignore_stickers_and_a_new_build_adds_only_its_looks(make):
    seeded, client = make(face=ROOMY_FACE, photos=4)
    old = offer(client, seeded, protocol=INTRO_CARDS_PROTOCOL)
    new = offer(client, seeded, protocol=INTRO_STICKERS_PROTOCOL)
    assert all(c["slot"].startswith("prop_") for o in photo_looks(new) for c in o["cards"])
    stripped = [o for o in new["options"] if o not in sticker_looks(new)]
    assert stripped == old["options"]


def test_s2_no_sticker_look_where_the_phone_would_flatten_it(make, monkeypatch):
    seeded, client = make(face=ROOMY_FACE)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [f for f in fx.VERIFIED_FEATURES if f != "alphaOverlay"],
    )
    assert not sticker_looks(offer(client, seeded))


@pytest.mark.parametrize("has_alpha", [False, None], ids=["opaque RGBA", "never measured"])
def test_s3_a_picture_with_no_measured_transparency_gets_no_sticker_look(make, has_alpha):
    seeded, client = make(face=ROOMY_FACE, has_alpha=has_alpha)
    assert not sticker_looks(offer(client, seeded))


def test_s4_on_a_close_selfie_stickers_pop_briefly_and_never_cover_the_title(make):
    seeded, client = make(face=CLOSE_FACE)
    looks = sticker_looks(offer(client, seeded))
    assert looks, "a sticker look on a close framing"
    look = looks[0]
    title_end = max(r["end_s"] for r in look["rows"])
    for card, box in zip(look["cards"], boxes(look["cards"]), strict=True):
        assert not any(boxes_intersect(box, line) for line in title_boxes(seeded, look["rows"]))
        if boxes_intersect(box, NormalizedBox(*CLOSE_FACE)):
            assert card["end_s"] - card["start_s"] <= 1.5 + 1e-9
        assert card["end_s"] <= title_end + 1e-9
    saved = save(client, seeded, look["rows"], as_overlays(look["cards"]))
    assert saved.status_code == 200, saved.text


def test_s6_her_own_edits_of_a_sticker_save_as_she_set_them(make):
    seeded, client = make(face=ROOMY_FACE)
    look = sticker_looks(offer(client, seeded))[0]
    assert save(client, seeded, look["rows"], as_overlays(look["cards"])).status_code == 200
    first, second = as_overlays(look["cards"])
    far_off = {**first, "x_frac": -0.06, "scale": 0.5, "start_s": 0.4, "end_s": 6.0}
    response = save(client, seeded, look["rows"], [far_off, second])
    assert response.status_code == 200, response.text
    drawn, windows = seeded.drawn_cards(), seeded.drawn_windows()
    assert drawn[first["id"]] == pytest.approx((-0.06, first["y_frac"], 0.5))
    assert windows[first["id"]][0] == pytest.approx(0.4)
    response = save(client, seeded, look["rows"], [second])
    assert response.status_code == 200, response.text
    assert set(seeded.drawn_cards()) == {second["id"]}
    assert seeded.drawn_cards()[second["id"]] == drawn[second["id"]]


def test_s7_another_owners_transparent_picture_is_refused(make):
    seeded, client = make(face=ROOMY_FACE)
    look = sticker_looks(offer(client, seeded))[0]
    stranger = uuid.uuid4()
    from app.database import sync_session
    from app.models import User

    with sync_session() as db:
        db.add(User(id=stranger, email=f"{stranger}@intro-stickers.test"))
        db.commit()
    seeded.users.append(stranger)
    theirs = seeded.add_sticker(created_at=datetime(2026, 10, 1, tzinfo=UTC), user_id=stranger)
    posted = as_overlays(look["cards"])
    posted[0]["src_gcs_path"] = seeded.path(theirs)
    response = save(client, seeded, look["rows"], posted)
    assert response.status_code == 422, response.text
    assert seeded.drawn_cards() == {}
