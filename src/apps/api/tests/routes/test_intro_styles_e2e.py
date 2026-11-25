"""KRI-1633 end to end: "Try intro styles" through the real app, real PostgreSQL and real Skia.

The failure-mode list F1-F26 and each guard are in `app/pipeline/intro_styles.py`; the F-numbers
here name the ones this file proves. Nothing is mocked in the chain: the real routes (owner
check, gates, rate limits), the real offer (compile, placement, dry-run Save, previews), the real
editor-commit Save (validators, render-contract rebind, phone recipe compile) and the pinned
recipe's text layers, per route (Talking, narrated, montage) in EN and TR. Only auth is
overridden (the dev-login stand-in) and the phone render itself does not exist here.

1. GET the editor variant: `editor_capabilities.intro_styles` is true (F1)
2. POST intro-styles, pick `options[0]`, POST editor-commit with the replaced lane: 200,
   recipe revision + 1, `awaiting_device`
3. the pinned recipe's text layers read as her words (`stacked_title_matches`, F12)
4. her colour edit, then Save: the layer colour is hers (F13)
5. an unrelated Save: the title layers are unchanged (F14)
6. a chat op rewrites the title: the stack collapses to one row (F13)
7. offer again, "No intro style", Save: one plain title row in her words (F15)
Plus: 409 stale base (F16), 426 at protocol 56 with no key (F1), 404 flag off (F1), no provider
call with the model switch on (F21), unavailable reasons for voiceover and speech montage (F20).
"""

from __future__ import annotations

import copy
import uuid
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
from app.models import ContentPlan, Job, Persona, PlanItem, User
from app.pipeline.phone_title_look import stacked_title_matches, title_family_of
from app.routes import generative_jobs as gj
from app.services.device_render import device_status
from app.text_fold import fold_text

_database_name = make_url(settings.database_url).database or ""
if not _database_name.endswith("_test"):
    pytest.skip(f"refusing to write fixtures to {_database_name!r}", allow_module_level=True)
try:
    with sync_engine.connect() as _probe:
        _probe.execute(text("SELECT 1"))
except (OperationalError, OSError) as exc:
    pytest.skip(f"test PostgreSQL unavailable: {exc!r}", allow_module_level=True)

ROUTES = fx.ROUTES
TITLES = {"en": fx.EN_TITLE, "tr": fx.TR_TITLE}
CAPS = {"X-Kria-Client-Protocol": "57"}


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    for key, value in fx.flag_overrides().items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(settings, "intro_styles_min_client_protocol", 57)
    monkeypatch.setattr(settings, "intro_scene_model_enabled", False)
    # the one storage boundary: the approved montage footage still has the generation it was
    # approved at (`_proposal_media_is_current`); no object store exists in the test
    from app import storage

    monkeypatch.setattr(
        storage, "object_metadata", lambda _path: SimpleNamespace(generation="1", size=1)
    )


class Seeded:
    def __init__(self, route: str, lang: str):
        job, self.variant_id, self.rows = fx.BUILDERS[route](TITLES[lang])
        self.route, self.lang = route, lang
        self.owner, plan_id, persona_id, self.item_id = (uuid.uuid4() for _ in range(4))
        self.job_id = job.id
        self.plan_id, self.persona_id = plan_id, persona_id
        with sync_session() as db:
            db.add(User(id=self.owner, email=f"{self.owner}@intro-styles.test"))
            db.flush()
            db.add(Persona(id=persona_id, user_id=self.owner, persona_status="ready", persona={}))
            db.flush()
            db.add(ContentPlan(id=plan_id, user_id=self.owner, persona_id=persona_id))
            db.flush()
            db.add(
                PlanItem(
                    id=self.item_id,
                    content_plan_id=plan_id,
                    position=1,
                    idea="intro styles",
                    item_status="ready",
                    # the approved montage's own footage, as `_proposal_media_is_current` reads it
                    clip_assignments=[
                        {"media_id": "source", "gcs_path": "user/analysis-proxy-source.mp4"}
                    ],
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
            item = db.get(PlanItem, self.item_id)
            item.current_job_id = job.id
            db.commit()
        monkey_ids = [str(self.owner)]
        self.user_ids = monkey_ids

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

    def layers(self):
        recipe = device_status(self.job(), self.variant_id).request.recipe
        return [layer for layer in recipe.text_layers if layer.id.startswith(("title-", "text-"))]

    def revision(self) -> int:
        return device_status(self.job(), self.variant_id).request.identity.recipe_revision

    def evidence(self) -> list[dict]:
        return [
            {
                "element_id": layer.id,
                "text": " ".join(run.text for run in layer.runs),
                "start_s": layer.start,
                "end_s": layer.end,
                "anchor_y": layer.anchor_y,
            }
            for layer in self.layers()
        ]

    def cleanup(self) -> None:
        with sync_session() as db:
            db.execute(
                text("UPDATE plan_items SET current_job_id = NULL WHERE id = :i"),
                {"i": self.item_id},
            )
            db.execute(delete(Job).where(Job.id == self.job_id))
            db.execute(delete(PlanItem).where(PlanItem.id == self.item_id))
            db.execute(delete(ContentPlan).where(ContentPlan.id == self.plan_id))
            db.execute(delete(Persona).where(Persona.id == self.persona_id))
            db.execute(delete(User).where(User.id == self.owner))
            db.commit()


@pytest.fixture
def make(monkeypatch):
    import contextlib

    made: list[Seeded] = []
    stack = contextlib.ExitStack()

    def build(route: str, lang: str = "en") -> tuple[Seeded, TestClient]:
        seeded = Seeded(route, lang)
        made.append(seeded)
        monkeypatch.setattr(settings, "intro_scene_user_ids", [str(seeded.owner)])
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=seeded.owner, email="x@intro-styles.test"
        )
        # one client (one event loop) for every request of the test: the async engine's pooled
        # connections belong to the loop that opened them
        client = stack.enter_context(TestClient(app, raise_server_exceptions=True))
        return seeded, client

    yield build
    stack.close()
    app.dependency_overrides.clear()
    for seeded in made:
        seeded.cleanup()


_UNSET = object()


def offer(client, seeded, rows=_UNSET, *, base=None, headers=CAPS):
    return client.post(
        f"{seeded.url}/intro-styles",
        json={
            "version": 1,
            "base_generation": base if base is not None else seeded.baseline(),
            "title_rows": seeded.rows if rows is _UNSET else rows,
        },
        headers=headers,
    )


def commit(client, seeded, **sections):
    """The editor's Save: a guided/montage Save carries the revision token it loaded."""
    revision = (
        gj._guided_v2_revision(seeded.job(), seeded.variant())
        if seeded.route == "montage"
        else None
    )
    token = {"guided_revision_number": revision["revision_number"]} if revision else {}
    return client.post(
        f"{seeded.url}/editor-commit",
        json={"base_generation": seeded.baseline(), **token, **sections},
        headers=CAPS,
    )


def save(client, seeded, rows, **extra):
    return commit(client, seeded, text_elements=rows, **extra)


@pytest.mark.parametrize("lang", ["en", "tr"])
@pytest.mark.parametrize("route", ROUTES)
def test_pick_save_edit_and_override_through_the_real_app(make, route, lang):
    seeded, client = make(route, lang)
    words = TITLES[lang]
    # 1. the entry exists for this build and account
    from app.services.client_protocol import _client_protocol

    token = _client_protocol.set(57)
    try:
        variants = gj._variants_for_response(seeded.job())
    finally:
        _client_protocol.reset(token)
    assert (
        next(v for v in variants if v["variant_id"] == seeded.variant_id)["editor_capabilities"][
            "intro_styles"
        ]
        is True
    )

    # 2. the offer, the pick, the Save
    response = offer(client, seeded)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] and body["route"] == route and len(body["options"]) >= 4
    pick = body["options"][0]
    before = seeded.revision()
    saved = save(client, seeded, pick["rows"])
    assert saved.status_code == 200, saved.text
    assert seeded.revision() == before + 1
    assert seeded.variant()["render_status"] == "awaiting_device"

    # 3. the pinned recipe's text layers read as her words
    assert stacked_title_matches(seeded.evidence(), words, tolerance_s=0.05)

    # 4. her colour edit wins and persists through an unrelated Save (5) too
    edited = [{**row, "color": "#12AB34"} for row in pick["rows"]]
    assert save(client, seeded, edited).status_code == 200
    colours = {
        (round(run.fill.red * 255), round(run.fill.green * 255), round(run.fill.blue * 255))
        for layer in seeded.layers()
        for run in layer.runs
    }
    assert colours == {(0x12, 0xAB, 0x34)}
    layers_before = seeded.layers()
    other = {"landscape_fit": "fit"} if route == "montage" else {"caption_meta": {"enabled": True}}
    unrelated = commit(client, seeded, **other)
    assert unrelated.status_code == 200, unrelated.text
    assert [layer.runs for layer in seeded.layers()] == [layer.runs for layer in layers_before]

    # 7. offer again over the saved stack: "No intro style" is one plain row in her words
    again = offer(client, seeded, edited)
    assert again.status_code == 200 and again.json()["available"]
    none = again.json()["options"][-1]
    assert none["key"] == "none" and len(none["rows"]) == 1
    assert fold_text(none["rows"][0]["text"]) == fold_text(words)
    assert save(client, seeded, none["rows"]).status_code == 200
    assert len(seeded.layers()) == 1


@pytest.mark.parametrize("route", ["talking", "montage"])
def test_a_chat_rewrite_of_a_picked_title_collapses_the_stack(make, route):
    from app.kria.plan_review import parse_ops_without_model
    from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops

    seeded, client = make(route)
    pick = offer(client, seeded).json()["options"][0]
    assert save(client, seeded, pick["rows"]).status_code == 200
    job, variant = seeded.job(), seeded.variant()
    snapshot = build_editor_snapshot(job, variant)
    ops, rejection = parse_ops_without_model(
        [{"op": "rewrite_text", "selector": {"group": "title"}, "text": "my morning ritual"}],
        snapshot,
        "edit the title",
    )
    assert ops, rejection
    compiled = compile_editor_ops(job, variant, ops)
    rows = compiled.payload.model_dump(mode="json", exclude_none=True)["text_elements"]
    title = [r for r in rows if title_family_of(r["id"])]
    assert len(title) == 1 and title[0]["text"] == "my morning ritual"


def test_a_stale_base_is_a_409_with_the_editors_own_body(make):
    seeded, client = make("talking")
    response = offer(client, seeded, base="not-the-baseline")
    assert response.status_code == 409
    problem = response.json()["problem"]
    assert problem["code"] == "baseline_conflict" and problem["recovery"] == "reload"
    assert problem["current_base_generation"] == seeded.baseline()


def test_a_protocol_56_build_gets_426_and_no_capability_key(make):
    seeded, client = make("talking")
    response = offer(client, seeded, headers={"X-Kria-Client-Protocol": "56"})
    assert response.status_code == 426
    assert response.json()["detail"] == {
        "code": "client_update_required",
        "feature": "intro_styles",
        "minimum_client_protocol": settings.intro_styles_min_client_protocol,
    }
    from app.services.client_protocol import _client_protocol

    token = _client_protocol.set(56)
    try:
        caps = gj._editor_capabilities(seeded.job(), seeded.variant())
    finally:
        _client_protocol.reset(token)
    assert "intro_styles" not in caps


def test_flag_off_is_a_404_and_no_key(make, monkeypatch):
    seeded, client = make("talking")
    monkeypatch.setattr(settings, "intro_scene_enabled", False)
    response = offer(client, seeded)
    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "intro_styles_disabled"}


def test_rows_that_are_not_her_title_are_a_422(make):
    seeded, client = make("talking")
    for rows in (None, [], [{"id": "caption-1", "text": "x y", "start_s": 0, "end_s": 1}]):
        response = offer(client, seeded, rows=rows)
        assert response.status_code == 422, rows
        assert response.json()["detail"] == {"code": "intro_styles_title_rows_invalid"}


def test_someone_elses_item_is_a_404(make):
    seeded, client = make("talking")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=uuid.uuid4(), email="y@intro-styles.test"
    )
    monkey = settings.intro_scene_user_ids
    settings.intro_scene_user_ids = ["*"]
    try:
        assert offer(client, seeded).status_code == 404
    finally:
        settings.intro_scene_user_ids = monkey


@pytest.mark.parametrize("archetype", ["voiceover", "speech_montage"])
def test_routes_that_cannot_save_a_title_have_a_reason_not_an_offer(make, archetype):
    seeded, client = make("talking")
    with sync_session() as db:
        job = db.get(Job, seeded.job_id)
        plan = copy.deepcopy(job.assembly_plan)
        plan["variants"][0]["resolved_archetype"] = archetype
        job.assembly_plan = plan
        db.commit()
    body = offer(client, seeded).json()
    assert (body["available"], body["reason"], body["options"]) == (False, "route_unsupported", [])


def test_the_model_is_not_called_even_with_its_switch_on(make, monkeypatch):
    import app.agents._model_client as model_client
    import app.services.phone_text_styling as styling

    def boom(*_a, **_k):
        raise AssertionError("a provider call")

    monkeypatch.setattr(settings, "intro_scene_model_enabled", True)
    monkeypatch.setattr(model_client, "default_client", boom)
    monkeypatch.setattr(styling, "select_fonts", boom)
    seeded, client = make("narrated")
    assert offer(client, seeded).json()["available"] is True


def test_the_offer_stores_nothing(make):
    seeded, client = make("talking")
    before = copy.deepcopy(seeded.job().assembly_plan)
    assert offer(client, seeded).status_code == 200
    assert seeded.job().assembly_plan == before
