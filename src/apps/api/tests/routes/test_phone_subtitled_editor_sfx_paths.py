"""Phone Talking (subtitled) editor lanes keep REAL catalog sound-effect paths.

A pinned device recipe carries no storage paths, so `sections_from_lanes`
writes the synthetic `sound-effects/{id}/{id}` placeholder for any effect
whose catalog audio object it doesn't know. iOS only commits CHANGED
sections, so a card-only first Save reaches `_compile_subtitled_editor_commit`
with no sound-effects section: the carried-over effect used to be persisted
with that placeholder, `_phone_subtitled_sfx_paths` stopped resolving once
`sound_effects` was a persisted list, and `_native_editor_assets` signed a
path that 404s -- the whole native preview fell back to the MP4.

Both ends are covered here: the commit fills the real
`SoundEffect.audio_gcs_path` for every carried-over effect, and the read path
heals rows that were already persisted with the placeholder.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models import PlanItemAsset, SoundEffect
from app.routes import generative_jobs as gj
from app.services.device_render import device_status
from app.services.phone_editor import prepare_phone_editor_commit
from app.services.phone_sources import PHONE_VISUALS_FIELD
from app.services.phone_subtitled_editor import PHONE_SUBTITLED_EDITOR_LANES_FIELD
from tests.routes.test_editor_commit import _db, _owned_item, _result, _user
from tests.routes.test_phone_subtitled_editor_commit import (
    _overlay_payload,
    _sfx_payload,
    phone_job,
    save,
)

CATALOG_PATH = "sound-effects/pop/audio.m4a"
PLACEHOLDER_PATH = "sound-effects/pop/pop"


def _ready_phone_job(monkeypatch):
    """`phone_job` after the phone uploaded its render (the editor only opens
    a ready variant, and the sfx/overlay capabilities need a video)."""
    job = phone_job(monkeypatch)
    job.assembly_plan["variants"][0]["video_path"] = "jobs/phone/subtitled.mp4"
    return job


class _CatalogDB:
    """Async session double answering the one `SoundEffect` catalog query."""

    def __init__(self, effects):
        self.effects = effects
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        result = SimpleNamespace(all=lambda: list(self.effects))
        return SimpleNamespace(scalars=lambda: result)


def _pop_effect():
    return SimpleNamespace(id="pop", audio_gcs_path=CATALOG_PATH)


def _variant(job) -> dict:
    return job.assembly_plan["variants"][0]


# --- commit time --------------------------------------------------------------


def test_card_only_first_save_persists_the_catalog_path_for_the_carried_effect(monkeypatch):
    job = _ready_phone_job(monkeypatch)

    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation="first", media_overlays=[_overlay_payload(x_frac=0.9)]
        ),
        user_id="owner",
        plan_item_id="item",
        phone_sfx_catalog_paths={"pop": CATALOG_PATH},
    )

    variant = _variant(job)
    assert [row["src_gcs_path"] for row in variant["sound_effects"]] == [CATALOG_PATH]
    assert variant[PHONE_SUBTITLED_EDITOR_LANES_FIELD]["paths"] == {"pop": CATALOG_PATH}


def test_a_placeholder_path_in_a_committed_section_never_replaces_a_real_one(monkeypatch):
    """Chat edits compile their sound-effects section from the variant rows
    without the route's catalog resolve, so a legacy row's placeholder can
    arrive as the committed path; the catalog path wins."""
    job = _ready_phone_job(monkeypatch)
    prepare_phone_editor_commit(
        job,
        "subtitled",
        prepare=lambda staged: {
            "has_render_section": True,
            "sections": {"sound_effects": True},
            "sfx_override": [_sfx_payload(at_s=4.0, src_gcs_path=PLACEHOLDER_PATH)],
            "media_overlays_override": None,
            "caption_cues_override": None,
            "generation": "second",
        },
        sfx_catalog_paths={"pop": CATALOG_PATH},
    )

    variant = _variant(job)
    assert variant["sound_effects"][0]["at_s"] == pytest.approx(4.0)
    assert variant["sound_effects"][0]["src_gcs_path"] == CATALOG_PATH
    assert variant[PHONE_SUBTITLED_EDITOR_LANES_FIELD]["paths"] == {"pop": CATALOG_PATH}


def test_commit_without_a_catalog_row_keeps_the_placeholder_out_of_the_paths_bag(monkeypatch):
    job = _ready_phone_job(monkeypatch)
    save(job, media_overlays=[_overlay_payload(x_frac=0.9)])

    variant = _variant(job)
    # The row still needs a prefix-valid path; the persisted bag only ever
    # holds real catalog objects, so a later read or Save can still heal it.
    assert variant["sound_effects"][0]["src_gcs_path"] == PLACEHOLDER_PATH
    assert variant[PHONE_SUBTITLED_EDITOR_LANES_FIELD]["paths"] == {}


def test_editor_commit_route_resolves_catalog_paths_for_carried_effects(monkeypatch):
    """End to end through the iOS Save route: a card-only first Save."""
    job = _ready_phone_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_render_user_ids", [], raising=False)
    user = _user()
    item, plan = _owned_item(user.id, job=job)
    # The route only accepts cards from the item's own pool; recipes pin a
    # visual by media id + generation, so re-homing the binding's path keeps
    # the pinned recipe valid.
    photo_path = f"users/{user.id}/plan/{item.id}/pool/photo-1.jpg"
    job.assembly_plan[PHONE_VISUALS_FIELD][0]["gcs_path"] = photo_path
    pool_row = SimpleNamespace(
        gcs_path=photo_path, gcs_generation="77", kind="image", upload_content_type="image/jpeg"
    )
    db = _db([item], plan, job)
    route_execute = db.execute.side_effect
    catalog_queries: list = []

    async def execute(statement):
        descriptions = getattr(statement, "column_descriptions", None) or []
        entity = descriptions[0].get("entity") if descriptions else None
        if entity is SoundEffect:
            catalog_queries.append(statement)
            return _result([_pop_effect()])
        if entity is PlanItemAsset:
            return _result([pool_row])
        return await route_execute(statement)

    db.execute.side_effect = execute
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        response = TestClient(app, raise_server_exceptions=False).post(
            f"/plan-items/{item.id}/variants/subtitled/editor-commit",
            json={
                "base_generation": "first",
                "media_overlays": [_overlay_payload(src_gcs_path=photo_path, x_frac=0.9)],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert device_status(job, "subtitled").request.identity.recipe_revision == 2
    variant = _variant(job)
    assert [row["src_gcs_path"] for row in variant["sound_effects"]] == [CATALOG_PATH]
    assert variant[PHONE_SUBTITLED_EDITOR_LANES_FIELD]["paths"] == {"pop": CATALOG_PATH}
    assert len(catalog_queries) == 1


# --- read time ----------------------------------------------------------------


async def test_rows_persisted_with_the_placeholder_heal_on_read(monkeypatch):
    """A variant Saved before the fix (placeholder persisted) signs the real
    catalog object without needing another Save."""
    job = _ready_phone_job(monkeypatch)
    save(job, media_overlays=[_overlay_payload(x_frac=0.9)])
    assert _variant(job)["sound_effects"][0]["src_gcs_path"] == PLACEHOLDER_PATH

    db = _CatalogDB([_pop_effect()])
    paths = await gj._phone_subtitled_sfx_paths(db, job, _variant(job))
    assert paths == {"pop": CATALOG_PATH}
    assert len(db.statements) == 1

    assets = gj._native_editor_assets(
        job, "subtitled", sign_url=lambda path, ttl: f"https://signed/{path}", sfx_paths=paths
    )
    sound = [asset for asset in assets if asset["kind"] == "sound_effect"]
    assert [asset["source_url"] for asset in sound] == [f"https://signed/{CATALOG_PATH}"]


def test_sync_catalog_read_matches_the_async_one(monkeypatch):
    """The Kria runtime worker's editor approvals read the catalog with a sync
    session before the same Save."""
    job = _ready_phone_job(monkeypatch)
    save(job, media_overlays=[_overlay_payload(x_frac=0.9)])
    statements: list = []

    class _SyncDB:
        def execute(self, statement):
            statements.append(statement)
            result = SimpleNamespace(all=lambda: [_pop_effect()])
            return SimpleNamespace(scalars=lambda: result)

    assert gj.phone_subtitled_sfx_paths_sync(_SyncDB(), job, _variant(job)) == {"pop": CATALOG_PATH}
    assert len(statements) == 1
    monkeypatch.setattr(gj.settings, "phone_subtitled_editor_lanes_enabled", False)
    assert gj.phone_subtitled_sfx_paths_sync(_SyncDB(), job, _variant(job)) == {}
    assert len(statements) == 1


async def test_rows_persisted_with_real_paths_need_no_catalog_lookup(monkeypatch):
    job = _ready_phone_job(monkeypatch)
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation="first", media_overlays=[_overlay_payload(x_frac=0.9)]
        ),
        user_id="owner",
        plan_item_id="item",
        phone_sfx_catalog_paths={"pop": CATALOG_PATH},
    )

    db = _CatalogDB([_pop_effect()])
    assert await gj._phone_subtitled_sfx_paths(db, job, _variant(job)) == {}
    assert db.statements == []
