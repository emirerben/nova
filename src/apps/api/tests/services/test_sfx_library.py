"""KRI-1613: a library slug to its `SoundEffect` row, on real Postgres.

Ways it could fail, a case each: a sibling slug that starts with the same letters ("sparkle" and
"sparkle-short", "camera-shutter" and "camera-shutter-click") wins; an archived, unpublished or
unready row is served; a row the seed script does not own (an admin upload named "<slug>.m4a",
a tagged row under another file name) is served; the newest of two live rows loses; the app's
copy of the version tag or the intro slugs drift from the seed catalog.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import sync_session
from app.models import SoundEffect
from app.pipeline.intro_scene import SILENT, SOUNDS
from app.services.sfx_library import LIBRARY_VERSION, library_effect_id
from scripts.sfx_library import catalog
from tests.postgres_required import postgres_required


def test_the_tag_and_every_intro_slug_are_the_seed_catalogs() -> None:
    assert LIBRARY_VERSION == catalog.LIBRARY_VERSION
    slugs = {effect.slug for effect in catalog.LIBRARY}
    assert {slug for slug in SOUNDS if slug != SILENT} <= slugs


@pytest.fixture
def db():
    if not (make_url(settings.database_url).database or "").endswith("_test"):
        pytest.skip("requires a *_test database")
    try:
        session = sync_session()
        session.execute(text("select 1"))
    except OperationalError:
        if postgres_required():
            pytest.fail("NOVA_REQUIRE_POSTGRES_TESTS=1 but the *_test Postgres is not reachable")
        pytest.skip("*_test Postgres not reachable")
    try:
        yield session
    finally:
        session.rollback()
        session.close()


_NOW = datetime(2026, 11, 27, tzinfo=UTC)


def _row(db, slug: str, *, age_s: int = 0, tag: str | None = None, **over) -> str:
    effect_id = f"t-{uuid.uuid4().hex[:12]}"
    fields = {
        "name": f"{slug} {effect_id}",
        "audio_gcs_path": f"sound-effects/{effect_id}/{slug}.m4a",
        "duration_s": 0.4,
        "status": "ready",
        "source_filename": f"{slug}.m4a",
        "provenance": f"{LIBRARY_VERSION}:{tag or slug} ElevenLabs; licence",
        "published_at": _NOW,
        "archived_at": None,
        "created_at": _NOW - timedelta(seconds=age_s),
        **over,
    }
    db.add(SoundEffect(id=effect_id, **fields))
    db.flush()
    return effect_id


def test_a_sibling_slug_never_answers(db) -> None:
    sparkle = _row(db, "sparkle", age_s=10)
    _row(db, "sparkle-short", age_s=0)
    shutter = _row(db, "camera-shutter", age_s=10)
    _row(db, "camera-shutter-click", age_s=0)
    assert library_effect_id(db, "sparkle") == sparkle
    assert library_effect_id(db, "camera-shutter") == shutter


def test_only_a_live_row_is_served_and_the_newest_wins(db) -> None:
    older = _row(db, "accent-pop", age_s=30)
    newer = _row(db, "accent-pop", age_s=20)
    _row(db, "accent-pop", age_s=10, archived_at=_NOW)
    _row(db, "accent-pop", age_s=5, published_at=None)
    _row(db, "accent-pop", age_s=1, status="pending")
    assert library_effect_id(db, "accent-pop") == newer != older


def test_a_row_the_seed_does_not_own_is_never_served(db) -> None:
    _row(db, "click", provenance=None)  # an admin upload that happens to be named click.m4a
    _row(db, "click", source_filename="my-click.m4a")  # tagged, but not the seed's file
    _row(db, "whoosh-fast", tag="whoosh-fast-long")
    assert library_effect_id(db, "click") is None
    assert library_effect_id(db, "whoosh-fast") is None
    assert library_effect_id(db, "soft-pop") is None
