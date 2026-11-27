"""KRI-1615 stage B end to end: her own photos as cards in "Try intro styles", from the offer to
the pinned recipe, through the real app, real PostgreSQL and real Skia.

The failure modes M31-M45 and each guard are in `app/pipeline/intro_styles.py`; the M-numbers
here name the ones this file proves. Nothing in the chain is mocked: the real routes (owner check,
gates), the real offer (layout, dry-run Save, previews), the real Save pin (`intro_card_pool` ->
`bind_phone_visual_assets`, which rechecks owner, item, path and generation) and the real phone
recompile. Only auth (the dev-login stand-in) and the storage download of her photo's bytes are
replaced. The card a test posts is built from the documented wire (docs/pipelines/intro-scene.md,
stage B), the way the iPhone will build it, not by the server's own helper.
"""

from __future__ import annotations

import copy
import hashlib
import io
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.auth import get_current_user
from app.cli import intro_styles_fixtures as fx
from app.config import settings
from app.database import sync_engine, sync_session
from app.main import app
from app.models import ContentPlan, Job, Persona, PlanItem, PlanItemAsset, User
from app.pipeline.phone_title_look import stacked_title_matches
from app.routes import generative_jobs as gj
from app.services.client_protocol import INTRO_CARDS_PROTOCOL
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_VISUALS_FIELD

_database_name = make_url(settings.database_url).database or ""
if not _database_name.endswith("_test"):
    pytest.skip(f"refusing to write fixtures to {_database_name!r}", allow_module_level=True)
try:
    with sync_engine.connect() as _probe:
        _probe.execute(text("SELECT 1"))
except (OperationalError, OSError) as exc:
    pytest.skip(f"test PostgreSQL unavailable: {exc!r}", allow_module_level=True)

pytest.importorskip("skia")

TITLES = {"en": fx.EN_TITLE, "tr": "şehir rehberi: lizbon, ilk gün"}
CARDS = {"X-Kria-Client-Protocol": str(INTRO_CARDS_PROTOCOL)}
OLD = {"X-Kria-Client-Protocol": str(INTRO_CARDS_PROTOCOL - 1)}
# A speaker framed with room above her head and under her chin (refs 3 and 5 on the taste board).
ROOMY_FACE = (0.32, 0.44, 0.68, 0.66)
PORTRAIT_PHOTO = 0.75


def _jpeg() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (60, 80), (200, 120, 40)).save(out, "JPEG")
    return out.getvalue()


JPEG = _jpeg()


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
        Path(local).write_bytes(JPEG)

    monkeypatch.setattr(storage, "download_generation_to_file", download)
    limiter.reset()
    yield downloads
    limiter.reset()


class Seeded:
    """A phone Talking edit as the worker leaves it (the real builders), its plan item and her
    Visuals pool: ``photos`` ready photos plus any ``extra`` rows, oldest first."""

    def __init__(self, lang: str, *, job_id: uuid.UUID, face=ROOMY_FACE, photos=4, extra=()):
        # The job id seeds which looks are tried, so each test names its own (a failure repeats).
        job, self.variant_id, self.rows = fx.talking_job(TITLES[lang], face=face, job_id=job_id)
        self.lang = lang
        self.owner, self.plan_id, self.persona_id, self.item_id = (uuid.uuid4() for _ in range(4))
        self.job_id = job.id
        self.photo_ids: list[str] = []
        self.users = [self.owner]
        with sync_session() as db:
            db.execute(
                text("UPDATE plan_items SET current_job_id = NULL WHERE current_job_id = :j"),
                {"j": job.id},
            )
            db.execute(delete(Job).where(Job.id == job.id))
            db.commit()
        with sync_session() as db:
            db.add(User(id=self.owner, email=f"{self.owner}@intro-cards.test"))
            db.flush()
            db.add(
                Persona(id=self.persona_id, user_id=self.owner, persona_status="ready", persona={})
            )
            db.flush()
            db.add(ContentPlan(id=self.plan_id, user_id=self.owner, persona_id=self.persona_id))
            db.flush()
            db.add(
                PlanItem(
                    id=self.item_id,
                    content_plan_id=self.plan_id,
                    position=1,
                    idea="intro cards",
                    item_status="ready",
                )
            )
            db.flush()
            db.add(
                Job(
                    id=job.id,
                    user_id=self.owner,
                    status="awaiting_device",
                    raw_storage_path="test-only",
                    content_plan_item_id=self.item_id,
                    content_plan_ownership_epoch=0,
                    assembly_plan=copy.deepcopy(job.assembly_plan),
                    all_candidates={},
                )
            )
            db.flush()
            db.get(PlanItem, self.item_id).current_job_id = job.id
            db.commit()
        start = datetime(2026, 11, 1, tzinfo=UTC)
        for index in range(photos):
            self.photo_ids.append(self.add_photo(created_at=start + timedelta(minutes=index)))
        for index, overrides in enumerate(extra):
            self.add_photo(created_at=start - timedelta(minutes=index + 1), **overrides)

    def add_photo(self, *, created_at, item_id=None, user_id=None, **overrides) -> str:
        asset_id = uuid.uuid4()
        owner = user_id or self.owner
        item = item_id or self.item_id
        row = {
            "id": asset_id,
            "user_id": owner,
            "plan_item_id": item,
            "kind": "image",
            "gcs_path": f"users/{owner}/plan/{item}/pool/{asset_id}.jpg",
            "gcs_generation": "7",
            "status": "ready",
            "media_status": "ready",
            "aspect": PORTRAIT_PHOTO,
            "analysis": {"dims_upright": True, "has_alpha": False, "width": 600, "height": 800},
            "created_at": created_at,
            **overrides,
        }
        with sync_session() as db:
            db.add(PlanItemAsset(**row))
            db.commit()
        return str(asset_id)

    def path(self, media_id: str) -> str:
        with sync_session() as db:
            return db.get(PlanItemAsset, uuid.UUID(media_id)).gcs_path

    @property
    def url(self) -> str:
        return f"/plan-items/{self.item_id}/variants/{self.variant_id}"

    def job(self):
        with sync_session() as db:
            job = db.get(Job, self.job_id)
            db.expunge(job)
            return job

    def variant(self) -> dict:
        return next(
            v for v in self.job().assembly_plan["variants"] if v["variant_id"] == self.variant_id
        )

    def baseline(self) -> str:
        return gj.variant_render_baseline(self.variant())

    def recipe(self):
        return device_status(self.job(), self.variant_id).request.recipe

    def drawn_cards(self) -> dict[str, tuple[float, float, float]]:
        """``{card id: (x, y, width)}`` the pinned recipe draws, canvas fractions."""
        out = {}
        for track in self.recipe().tracks:
            for clip in track.clips:
                placement = clip.visual_placement
                if clip.id.startswith("subtitled-overlay-") and placement is not None:
                    out[clip.id.removeprefix("subtitled-overlay-")] = (
                        placement.x_fraction,
                        placement.y_fraction,
                        placement.width_fraction,
                    )
        return out

    def title_evidence(self) -> list[dict]:
        return [
            {
                "element_id": layer.id,
                "text": " ".join(run.text for run in layer.runs),
                "start_s": layer.start,
                "end_s": layer.end,
                "anchor_y": layer.anchor_y,
            }
            for layer in self.recipe().text_layers
            if layer.id.startswith(("title-", "text-"))
        ]

    def cleanup(self) -> None:
        with sync_session() as db:
            db.execute(
                text("UPDATE plan_items SET current_job_id = NULL WHERE id = :i"),
                {"i": self.item_id},
            )
            db.execute(delete(Job).where(Job.id == self.job_id))
            db.execute(delete(PlanItemAsset).where(PlanItemAsset.user_id.in_(self.users)))
            db.execute(delete(PlanItem).where(PlanItem.content_plan_id == self.plan_id))
            db.execute(delete(ContentPlan).where(ContentPlan.id == self.plan_id))
            db.execute(delete(Persona).where(Persona.id == self.persona_id))
            db.execute(delete(User).where(User.id.in_(self.users)))
            db.commit()


@pytest.fixture
def make(monkeypatch, request):
    import contextlib

    made: list[Seeded] = []
    stack = contextlib.ExitStack()

    def build(lang: str = "en", **kwargs) -> tuple[Seeded, TestClient]:
        job_id = uuid.uuid5(uuid.NAMESPACE_URL, f"kri1615b/{request.node.nodeid}/{len(made)}")
        seeded = Seeded(lang, job_id=job_id, **kwargs)
        made.append(seeded)
        monkeypatch.setattr(settings, "intro_scene_user_ids", [str(seeded.owner)])
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=seeded.owner, email="x@intro-cards.test"
        )
        client = stack.enter_context(TestClient(app, raise_server_exceptions=True))
        return seeded, client

    yield build
    stack.close()
    app.dependency_overrides.clear()
    for seeded in made:
        seeded.cleanup()


def offer(client, seeded, rows=None, *, headers=CARDS) -> dict:
    response = client.post(
        f"{seeded.url}/intro-styles",
        json={
            "version": 1,
            "base_generation": seeded.baseline(),
            "title_rows": seeded.rows if rows is None else rows,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def as_overlays(cards: list[dict]) -> list[dict]:
    """What the editor posts for an option's ``cards`` (the documented stage C mapping)."""
    return [
        {
            "id": card["id"],
            "kind": "image",
            "src_gcs_path": card["gcs_path"],
            "display_mode": "pip",
            "position": "custom",
            "x_frac": card["x_frac"],
            "y_frac": card["y_frac"],
            "scale": card["scale"],
            "start_s": card["start_s"],
            "end_s": card["end_s"],
            "z": card["z"],
        }
        for card in cards
    ]


def save(client, seeded, rows, overlays):
    return client.post(
        f"{seeded.url}/editor-commit",
        json={
            "base_generation": seeded.baseline(),
            "text_elements": rows,
            "media_overlays": overlays,
        },
        headers=CARDS,
    )


def card_options(body: dict) -> list[dict]:
    return [option for option in body["options"] if option.get("cards")]


@pytest.mark.parametrize("lang", ["en", "tr"])
def test_a_card_look_is_offered_saved_and_drawn_exactly_where_it_was_offered(make, _flags, lang):
    seeded, client = make(lang)
    body = offer(client, seeded)
    looks = card_options(body)
    assert looks, body.get("reason")
    assert body["options"][-1]["key"] == "none" and "cards" not in body["options"][-1]
    look = looks[0]
    cards = look["cards"]
    # her photos, oldest first, one per slot, each with the generation the Save will bind (M34)
    assert [c["slot"] for c in cards] == [f"prop_{n}" for n in range(1, len(cards) + 1)]
    assert [c["media_id"] for c in cards] == seeded.photo_ids[: len(cards)]
    assert [c["id"] for c in cards] == [f"intro-card-{n}" for n in range(1, len(cards) + 1)]
    assert all(
        c["gcs_path"] == seeded.path(c["media_id"]) and c["generation"] == "7" for c in cards
    )
    title_start = min(r["start_s"] for r in look["rows"])
    title_end = max(r["end_s"] for r in look["rows"])
    assert all((c["start_s"], c["end_s"]) == (title_start, title_end) for c in cards)
    if lang == "tr":
        assert {r.get("text_language") for r in look["rows"]} == {"tr"}  # M40
    # the preview shows the same cards (M43)
    preview_cards = look["preview"]["cards"]
    assert [p["media_id"] for p in preview_cards] == [c["media_id"] for c in cards]
    for p, c in zip(preview_cards, cards, strict=True):
        box = p["box"]
        assert (box["left"] + box["right"]) / 2 == pytest.approx(c["x_frac"])
        assert (box["top"] + box["bottom"]) / 2 == pytest.approx(c["y_frac"])
        assert box["right"] - box["left"] == pytest.approx(c["scale"])

    saved = save(client, seeded, look["rows"], as_overlays(cards))
    assert saved.status_code == 200, saved.text
    # every photo was bound through the real receipt path, at its generation
    receipts = {r["media_id"]: r for r in seeded.job().assembly_plan[PHONE_VISUALS_FIELD]}
    for card in cards:
        assert receipts[card["media_id"]]["sha256"] == hashlib.sha256(JPEG).hexdigest()
        assert receipts[card["media_id"]]["generation"] == "7"
    assert sorted(_flags) == sorted(c["gcs_path"] for c in cards)
    # the phone draws each card where the offer put it: none lifted onto her face (M41)
    drawn = seeded.drawn_cards()
    for card in cards:
        x, y, width = drawn[card["id"]]
        assert (x, y, width) == pytest.approx((card["x_frac"], card["y_frac"], card["scale"]))
    assert any(c["y_frac"] > 0.62 for c in cards), "the look puts a card under her captions"
    assert stacked_title_matches(seeded.title_evidence(), TITLES[lang], tolerance_s=0.05)

    # the same look applied and saved again converges (M37), and the next offer marks it current
    lanes_before = seeded.variant()["media_overlays"]
    again = save(client, seeded, look["rows"], as_overlays(cards))
    assert again.status_code == 200, again.text
    assert seeded.variant()["media_overlays"] == lanes_before
    assert seeded.drawn_cards() == drawn
    reoffer = offer(client, seeded, look["rows"])
    current = [o["key"] for o in reoffer["options"] if o["current"]]
    assert current == [look["key"]]


def test_an_old_build_and_the_switch_off_get_todays_offer(make, monkeypatch):
    seeded, client = make()
    new = offer(client, seeded)
    old = offer(client, seeded, headers=OLD)
    monkeypatch.setattr(settings, "intro_cards_enabled", False)
    off = offer(client, seeded)
    assert card_options(new) and not card_options(old)
    assert old == off  # M31: byte for byte
    stripped = {**new, "options": [o for o in new["options"] if not o.get("cards")]}
    assert stripped == old


def test_a_close_speaker_gets_no_card_look(make):
    seeded, client = make(face=fx.FACE)  # M35: the title alone fills the room above her head
    body = offer(client, seeded)
    assert body["available"] and not card_options(body)


def test_photos_that_cannot_be_cards_are_never_offered(make):
    extra = [
        {"analysis": {"dims_upright": True, "has_alpha": True}},  # a transparent sticker
        {"status": "analyzing"},
        {"media_status": "pending"},
        {"gcs_generation": None},
        {"analysis": {"has_alpha": False}},  # shape never measured upright
        {"aspect": None},
    ]
    seeded, client = make(photos=1, extra=extra)  # M33, M34: one usable photo is not a look
    assert not card_options(offer(client, seeded))


def test_three_photos_offer_only_the_two_card_look(make):
    seeded, client = make(photos=3)
    looks = card_options(offer(client, seeded))
    assert looks and {len(o["cards"]) for o in looks} == {2}  # M34
    assert all(o["key"].endswith("+around_speaker") for o in looks)


def test_a_card_already_on_screen_with_the_title_keeps_card_looks_out(make):
    seeded, client = make()
    first = offer(client, seeded)
    look, plain = card_options(first)[0], first["options"][0]
    # a Save of the look pins her photo; a later Save keeps that photo as a card of her own
    # (not an intro card) on screen with the title, then puts the plain look back
    assert save(client, seeded, look["rows"], as_overlays(look["cards"])).status_code == 200
    hers = {**as_overlays(look["cards"])[0], "id": "her-card", "start_s": 0.5, "end_s": 2.0}
    response = save(client, seeded, plain["rows"], [hers])
    assert response.status_code == 200, response.text
    assert not card_options(offer(client, seeded, plain["rows"]))  # M36


def test_deleting_one_card_keeps_the_other_where_it_was(make):
    seeded, client = make(photos=2)
    look = card_options(offer(client, seeded))[0]
    assert save(client, seeded, look["rows"], as_overlays(look["cards"])).status_code == 200
    before = seeded.drawn_cards()
    kept = look["cards"][1:]
    response = save(client, seeded, look["rows"], as_overlays(kept))
    assert response.status_code == 200, response.text  # M38
    after = seeded.drawn_cards()
    assert set(after) == {c["id"] for c in kept}
    assert all(after[c["id"]] == before[c["id"]] for c in kept)


def test_none_over_a_card_look_takes_the_cards_away(make):
    seeded, client = make()
    look = card_options(offer(client, seeded))[0]
    assert save(client, seeded, look["rows"], as_overlays(look["cards"])).status_code == 200
    none = offer(client, seeded, look["rows"])["options"][-1]
    assert none["key"] == "none" and "cards" not in none
    response = save(client, seeded, none["rows"], [])  # M37: every intro-card-* goes
    assert response.status_code == 200, response.text
    assert seeded.drawn_cards() == {}


def test_a_card_on_a_photo_that_cannot_be_a_card_is_refused_as_today(make):
    seeded, client = make()
    sticker = seeded.add_photo(
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        analysis={"dims_upright": True, "has_alpha": True},
    )
    look = card_options(offer(client, seeded))[0]
    posted = as_overlays(look["cards"])
    posted[0]["src_gcs_path"] = seeded.path(sticker)
    response = save(client, seeded, look["rows"], posted)
    assert response.status_code == 422, response.text  # M32, M33: the wrong-content control
    assert "isn't a photo Kria pinned" in response.json()["detail"]["reason"]
    assert seeded.drawn_cards() == {}


def test_a_card_on_another_items_photo_is_refused(make):
    seeded, client = make()
    other_item = uuid.uuid4()
    with sync_session() as db:
        db.add(PlanItem(id=other_item, content_plan_id=seeded.plan_id, position=2, idea="other"))
        db.commit()
    foreign = seeded.add_photo(created_at=datetime(2026, 10, 1, tzinfo=UTC), item_id=other_item)
    look = card_options(offer(client, seeded))[0]
    posted = as_overlays(look["cards"])
    posted[0]["src_gcs_path"] = seeded.path(foreign)
    response = save(client, seeded, look["rows"], posted)
    assert response.status_code == 422, response.text  # M32
    assert seeded.drawn_cards() == {}


def test_the_switch_off_at_save_refuses_a_new_card_as_today(make, monkeypatch):
    seeded, client = make()
    look = card_options(offer(client, seeded))[0]
    monkeypatch.setattr(settings, "intro_cards_enabled", False)
    response = save(client, seeded, look["rows"], as_overlays(look["cards"]))
    assert response.status_code == 422, response.text  # M31
    assert "isn't a photo Kria pinned" in response.json()["detail"]["reason"]


def test_bytes_gone_at_save_is_an_honest_refusal(make, monkeypatch):
    from google.api_core.exceptions import NotFound

    from app import storage

    seeded, client = make()
    look = card_options(offer(client, seeded))[0]

    def gone(*_args, **_kwargs):
        raise NotFound("gone")

    monkeypatch.setattr(storage, "download_generation_to_file", gone)
    response = save(client, seeded, look["rows"], as_overlays(look["cards"]))
    assert response.status_code == 422  # M42: the dry run hashed nothing
    assert response.json()["detail"] == {"code": "intro_card_unavailable"}


def test_without_the_servers_mark_a_card_look_is_not_offered(make, monkeypatch):
    """M41 control: the Save's own `caption_clear` mark is what keeps a bottom card under her
    captions. Without it the phone lifts the card, the dry run sees it compiled somewhere else,
    and the look is dropped instead of offered."""
    from app.services import intro_card_pool

    seeded, client = make()
    assert card_options(offer(client, seeded))
    monkeypatch.setattr(
        intro_card_pool,
        "caption_clear_decisions",
        lambda *_args, **_kwargs: {},
    )
    assert not card_options(offer(client, seeded))
