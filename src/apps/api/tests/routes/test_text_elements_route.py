"""Route tests for PUT /plan-items/{id}/variants/{vid}/text-elements (T4).

Tests guard conditions (lyrics variant, too many elements, no base) and the
happy-path write + enqueue contract.  Uses the same mock-DB pattern as
test_plan_item_variant_edit.py so no real DB / Celery worker is needed.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app

REGEN = "app.tasks.generative_build.regenerate_generative_variant"


# ── fixtures / helpers ────────────────────────────────────────────────────────


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


def _result(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    return r


def _job(variants: list[dict]) -> MagicMock:
    job = MagicMock()
    job.id = uuid.uuid4()
    job.status = "variants_ready"
    job.assembly_plan = {"variants": variants}
    return job


def _owned_item(user_id: uuid.UUID, *, job=None):
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = ["users/x/plan/y/a.mp4"]
    item.day_index = 1
    item.theme = "t"
    item.idea = "i"
    item.filming_suggestion = None
    item.rationale = None
    item.current_job = job
    item.current_job_id = job.id if job else None
    item.item_status = "idea"
    item.user_edited = False
    item.conformance = None
    item.filming_guide = []
    item.clip_assignments = []
    item.position = 1
    item.scheduled_date = None
    item.notes = None
    item.scenes = []
    item.source_idea_seed_id = None
    item.source_idea_seed_text = None
    item.voiceover_gcs_path = None
    item.voiceover_bed_level = None
    item.voiceover_caption_style = None
    item.edit_format = None
    plan = MagicMock()
    plan.user_id = user_id
    return item, plan


def _db(execute_results: list, plan) -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(v) for v in execute_results])
    db.get = AsyncMock(return_value=plan)
    return db


def _override(user, db) -> None:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


# Minimal valid TextElement dict (only required fields)
_VALID_ELEMENT = {
    "id": "abc123",
    "text": "Hello world",
    "start_s": 0.0,
    "end_s": 2.0,
    "role": "generative_intro",
    "position": "middle",
}

AGENT_TEXT_VARIANT = {
    "variant_id": "song_text",
    "text_mode": "agent_text",
    "render_status": "ready",
    "rank": 1,
    "style_set_id": "default",
    "base_video_path": "generative-jobs/abc/base.mp4",
}

LYRICS_VARIANT = {
    "variant_id": "song_lyrics",
    "text_mode": "lyrics",
    "render_status": "ready",
    "rank": 2,
    "style_set_id": "default",
    "base_video_path": None,
}

NO_BASE_VARIANT = {
    "variant_id": "original_text",
    "text_mode": "agent_text",
    "render_status": "ready",
    "rank": 3,
    "style_set_id": "default",
    # No base_video_path
}


# ── guard: lyrics variant → 422 (A16) ────────────────────────────────────────


def test_lyrics_variant_rejected(client: TestClient) -> None:
    """PUT on a lyrics variant must return 422 (lyric lines are beat-synced)."""
    user = _user()
    job = _job([dict(LYRICS_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    resp = client.put(
        f"/plan-items/{item.id}/variants/song_lyrics/text-elements",
        json={"elements": [_VALID_ELEMENT], "render": True},
    )
    assert resp.status_code == 422
    assert "lyrics" in resp.json()["detail"].lower()


# ── guard: too many elements → 422 ───────────────────────────────────────────


def test_too_many_elements_rejected(client: TestClient) -> None:
    """More than 50 elements must return 422."""
    user = _user()
    job = _job([dict(AGENT_TEXT_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    big_list = [
        {**_VALID_ELEMENT, "id": f"elem{i}", "start_s": float(i), "end_s": float(i) + 1}
        for i in range(51)
    ]
    resp = client.put(
        f"/plan-items/{item.id}/variants/song_text/text-elements",
        json={"elements": big_list, "render": True},
    )
    assert resp.status_code == 422
    assert "50" in resp.json()["detail"]


# ── guard: render=True + no base_video_path → 422 ────────────────────────────


def test_render_without_base_rejected(client: TestClient) -> None:
    """render=True on a variant with no base_video_path must return 422."""
    user = _user()
    job = _job([dict(NO_BASE_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    resp = client.put(
        f"/plan-items/{item.id}/variants/original_text/text-elements",
        json={"elements": [_VALID_ELEMENT], "render": True},
    )
    assert resp.status_code == 422
    assert "base" in resp.json()["detail"].lower()


# ── happy path: valid elements, render=True ───────────────────────────────────


def test_set_text_elements_happy_path(client: TestClient) -> None:
    """Valid PUT with render=True sets render_status=rendering and enqueues a task."""
    user = _user()
    job = _job([dict(AGENT_TEXT_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    # execute calls: load item → reload item (for response)
    db = _db([item, item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.apply_async = MagicMock()
        resp = client.put(
            f"/plan-items/{item.id}/variants/song_text/text-elements",
            json={"elements": [_VALID_ELEMENT], "render": True},
        )
    assert resp.status_code == 200
    # The variant's render_status should have been flipped to "rendering"
    variant = job.assembly_plan["variants"][0]
    assert variant["render_status"] == "rendering"
    assert variant["text_elements_user_edited"] is True
    assert "render_generation_id" in variant
    # Task must have been enqueued to the overlay-jobs queue
    regen.apply_async.assert_called_once()
    call_kwargs = regen.apply_async.call_args
    assert call_kwargs.kwargs["queue"] == "overlay-jobs"
    assert "render_gen_id" in call_kwargs.kwargs.get("kwargs", {})


# ── happy path: render=False → no task enqueued ──────────────────────────────


def test_set_text_elements_render_false(client: TestClient) -> None:
    """render=False persists elements without enqueuing a Celery task."""
    user = _user()
    job = _job([dict(AGENT_TEXT_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.apply_async = MagicMock()
        resp = client.put(
            f"/plan-items/{item.id}/variants/song_text/text-elements",
            json={"elements": [_VALID_ELEMENT], "render": False},
        )
    assert resp.status_code == 200
    variant = job.assembly_plan["variants"][0]
    assert variant["text_elements_user_edited"] is True
    # render_status must NOT have changed when render=False
    assert variant["render_status"] == "ready"
    regen.apply_async.assert_not_called()


# ── T8: sequence lock removed from dispatch_retext ───────────────────────────

SEQUENCE_VARIANT = {
    "variant_id": "original_text",
    "text_mode": "agent_text",
    "intro_mode": "sequence",
    "render_status": "ready",
    "rank": 3,
    "style_set_id": "default",
    "base_video_path": "generative-jobs/abc/base.mp4",
    "intro_text": "A hook sentence",
    "scenes": [{"text": "word", "start_s": 0.0, "end_s": 1.0}],
}


def test_retext_sequence_variant_now_succeeds(client: TestClient) -> None:
    """T8: dispatch_retext on a sequence variant must NO LONGER return 422."""
    user = _user()
    job = _job([dict(SEQUENCE_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/original_text/retext",
            json={"text": "new hook"},
        )
    assert resp.status_code == 200, f"Expected 200; got {resp.status_code}: {resp.text}"
    regen.delay.assert_called_once()


# ── T8: dispatch_edit_variant sequence lock STILL enforced ───────────────────


def test_edit_variant_sequence_still_blocked(client: TestClient) -> None:
    """T8: dispatch_edit_variant with text on a sequence variant must still return 422."""
    user = _user()
    job = _job([dict(SEQUENCE_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)
    resp = client.post(
        f"/plan-items/{item.id}/variants/original_text/edit",
        json={"text": "overwrite attempt"},
    )
    assert resp.status_code == 422
    assert "synced" in resp.json()["detail"].lower() or "editorial" in resp.json()["detail"].lower()


# ── T8: sequence materialization on first PUT /text-elements ─────────────────


def test_sequence_materialization_on_first_put(client: TestClient) -> None:
    """T8: first PUT on a sequence variant (text_elements_user_edited=False) materializes
    geometry_materialized_at_version on the variant dict."""
    user = _user()
    variant = {**SEQUENCE_VARIANT, "text_elements_user_edited": False}
    job = _job([dict(variant)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.apply_async = MagicMock()
        resp = client.put(
            f"/plan-items/{item.id}/variants/original_text/text-elements",
            json={"elements": [_VALID_ELEMENT], "render": False},
        )
    assert resp.status_code == 200
    v = job.assembly_plan["variants"][0]
    assert v["text_elements_user_edited"] is True
    assert v.get("geometry_materialized_at_version") == "1"
    assert v.get("text_elements_materialized_from") == "sequence"
