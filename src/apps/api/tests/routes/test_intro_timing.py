"""Route tests for intro-timing endpoint on plan-items.

Mirrors the intro-size tests in test_plan_item_variant_edit.py — mock-DB style,
asserting the dispatch helper fires `regenerate_generative_variant.delay` with the
correct kwargs and that guard conditions return the right HTTP status codes.
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

# ── fixtures ──────────────────────────────────────────────────────────────────


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


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _override(user, db) -> None:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


# ── variant fixtures ───────────────────────────────────────────────────────────

_AGENT_TEXT_VARIANT = {
    "variant_id": "song_text",
    "music_track_id": "track-123",
    "render_status": "ready",
    "rank": 1,
    "text_mode": "agent_text",
    "base_video_path": "generative-jobs/x/base.mp4",
}

_LYRICS_VARIANT = {
    "variant_id": "song_lyrics",
    "music_track_id": "track-123",
    "render_status": "ready",
    "rank": 2,
    "text_mode": "lyrics",
}

# ── tests ─────────────────────────────────────────────────────────────────────


def test_intro_timing_happy_path(client: TestClient) -> None:
    """POST intro-timing → 200, render_status set to rendering, task enqueued."""
    user = _user()
    job = _job([dict(_AGENT_TEXT_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/intro-timing",
            json={"start_s": 0.5, "end_s": 4.0},
        )
    assert resp.status_code == 200
    regen.delay.assert_called_once_with(
        str(job.id),
        "song_text",
        intro_start_s_override=0.5,
        intro_end_s_override=4.0,
    )


def test_intro_timing_persists_on_variant(client: TestClient) -> None:
    """dispatch_set_intro_timing writes intro_start_s / intro_end_s into the variant dict."""
    user = _user()
    job = _job([dict(_AGENT_TEXT_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        client.post(
            f"/plan-items/{item.id}/variants/song_text/intro-timing",
            json={"start_s": 1.0, "end_s": 5.0},
        )
    # After dispatch the variant dict in assembly_plan should carry the new timing fields.
    variants = job.assembly_plan["variants"]
    target = next(v for v in variants if v["variant_id"] == "song_text")
    assert target["intro_start_s"] == 1.0
    assert target["intro_end_s"] == 5.0
    assert target["render_status"] == "rendering"


def test_intro_timing_rejects_non_agent_text_variant(client: TestClient) -> None:
    """A lyrics variant has no resizable AI intro overlay → 422, no task enqueued."""
    user = _user()
    job = _job([dict(_LYRICS_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_lyrics/intro-timing",
            json={"start_s": 0.0, "end_s": 3.0},
        )
    assert resp.status_code == 422
    regen.delay.assert_not_called()


def test_intro_timing_rejects_end_le_start(client: TestClient) -> None:
    """end_s <= start_s → 422, no task enqueued."""
    user = _user()
    job = _job([dict(_AGENT_TEXT_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/intro-timing",
            json={"start_s": 3.0, "end_s": 1.0},
        )
    assert resp.status_code == 422
    regen.delay.assert_not_called()


def test_intro_timing_rejects_end_equal_start(client: TestClient) -> None:
    """end_s == start_s → 422."""
    user = _user()
    job = _job([dict(_AGENT_TEXT_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/intro-timing",
            json={"start_s": 2.0, "end_s": 2.0},
        )
    assert resp.status_code == 422
    regen.delay.assert_not_called()
