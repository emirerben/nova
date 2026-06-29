"""Tests for scene-timing override endpoint (PR-D).

Covers:
1. PATCH scene-timing on a sequence variant → 200, overrides persisted, no render enqueued.
2. PATCH on a non-sequence variant → 422.
3. _apply_scene_timing_overrides unit tests (normal, out-of-bounds, empty).
4. scene_timings appears in GET job status response for sequence variants.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.tasks.generative_build import _apply_scene_timing_overrides

REGEN = "app.tasks.generative_build.regenerate_generative_variant"

# ── shared helpers ────────────────────────────────────────────────────────────


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


# ── variant fixtures ──────────────────────────────────────────────────────────

_SEQUENCE_VARIANT = {
    "variant_id": "original_text",
    "music_track_id": None,
    "render_status": "ready",
    "rank": 3,
    "text_mode": "agent_text",
    "intro_mode": "sequence",
    "scenes": [
        {"text": "Hello world", "start_s": 0.0, "end_s": 2.5},
        {"text": "Second scene", "start_s": 2.5, "end_s": 5.0},
    ],
}

_NON_SEQUENCE_VARIANT = {
    "variant_id": "song_text",
    "music_track_id": "track-123",
    "render_status": "ready",
    "rank": 1,
    "text_mode": "agent_text",
    "intro_mode": "cluster",
}

# ── route tests ───────────────────────────────────────────────────────────────


def test_patch_scene_timing_happy_path(client: TestClient) -> None:
    """PATCH scene-timing on sequence variant → 200, overrides persisted, NO render task."""
    user = _user()
    job = _job([dict(_SEQUENCE_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)

    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.patch(
            f"/plan-items/{item.id}/variants/original_text/scene-timing",
            json={
                "overrides": [
                    {"scene_index": 0, "start_s": 0.5, "end_s": 2.0},
                    {"scene_index": 1, "start_s": 2.2, "end_s": 4.8},
                ]
            },
        )

    assert resp.status_code == 200
    # No re-render should be enqueued — overrides are apply-on-request.
    regen.delay.assert_not_called()


def test_patch_scene_timing_persists_overrides(client: TestClient) -> None:
    """Overrides are written into scene_timing_overrides on the variant dict."""
    user = _user()
    job = _job([dict(_SEQUENCE_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)

    overrides = [
        {"scene_index": 0, "start_s": 0.3, "end_s": 2.1},
    ]
    with patch(REGEN):
        client.patch(
            f"/plan-items/{item.id}/variants/original_text/scene-timing",
            json={"overrides": overrides},
        )

    variants = job.assembly_plan["variants"]
    target = next(v for v in variants if v["variant_id"] == "original_text")
    assert target["scene_timing_overrides"] == overrides


def test_patch_scene_timing_rejects_non_sequence_variant(client: TestClient) -> None:
    """PATCH on a cluster/linear variant → 422 (no scene timing to edit)."""
    user = _user()
    job = _job([dict(_NON_SEQUENCE_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)

    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.patch(
            f"/plan-items/{item.id}/variants/song_text/scene-timing",
            json={"overrides": [{"scene_index": 0, "start_s": 0.0, "end_s": 2.0}]},
        )

    assert resp.status_code == 422
    regen.delay.assert_not_called()


# ── _apply_scene_timing_overrides unit tests ──────────────────────────────────


def test_apply_scene_timing_overrides_normal() -> None:
    """Overrides replace start_s / end_s on the targeted scenes."""
    scenes = [
        {"text": "A", "start_s": 0.0, "end_s": 3.0},
        {"text": "B", "start_s": 3.0, "end_s": 6.0},
    ]
    overrides = [
        {"scene_index": 0, "start_s": 0.5, "end_s": 2.8},
        {"scene_index": 1, "start_s": 3.1, "end_s": 5.9},
    ]
    result = _apply_scene_timing_overrides(scenes, overrides)
    assert result[0]["start_s"] == 0.5
    assert result[0]["end_s"] == 2.8
    assert result[1]["start_s"] == 3.1
    assert result[1]["end_s"] == 5.9
    # Other fields preserved
    assert result[0]["text"] == "A"


def test_apply_scene_timing_overrides_out_of_bounds() -> None:
    """Out-of-bounds scene_index is silently dropped; valid indices still applied."""
    scenes = [{"text": "A", "start_s": 0.0, "end_s": 3.0}]
    overrides = [
        {"scene_index": 0, "start_s": 0.5, "end_s": 2.5},
        {"scene_index": 99, "start_s": 10.0, "end_s": 20.0},  # out of bounds
    ]
    result = _apply_scene_timing_overrides(scenes, overrides)
    assert len(result) == 1
    assert result[0]["start_s"] == 0.5
    assert result[0]["end_s"] == 2.5


def test_apply_scene_timing_overrides_empty() -> None:
    """Empty overrides list returns scenes unchanged."""
    scenes = [{"text": "A", "start_s": 0.0, "end_s": 3.0}]
    result = _apply_scene_timing_overrides(scenes, [])
    assert result == [{"text": "A", "start_s": 0.0, "end_s": 3.0}]


def test_apply_scene_timing_overrides_does_not_mutate_input() -> None:
    """Original scenes list is not mutated."""
    original = [{"text": "A", "start_s": 0.0, "end_s": 3.0}]
    scenes = [dict(s) for s in original]
    _apply_scene_timing_overrides(scenes, [{"scene_index": 0, "start_s": 1.0, "end_s": 2.0}])
    assert scenes[0]["start_s"] == 0.0  # original unchanged


# ── scene_timings in GET status ───────────────────────────────────────────────


def test_scene_timings_exposed_in_variants_for_response() -> None:
    """scene_timings is populated from scenes on sequence variants."""
    from app.routes.generative_jobs import _variants_for_response

    job = MagicMock()
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": "original_text",
                "render_status": "ready",
                "output_url": None,
                "text_mode": "agent_text",
                "rank": 1,
                "intro_mode": "sequence",
                "scenes": [
                    {"text": "Hello", "start_s": 0.0, "end_s": 2.5},
                    {"text": "World", "start_s": 2.5, "end_s": 5.0},
                ],
            }
        ]
    }

    with (
        patch("app.routes.generative_jobs.signed_get_url", side_effect=Exception("no gcs")),
        patch("app.routes.generative_jobs._TEXT_ELEMENTS_ENABLED", False),
    ):
        result = _variants_for_response(job)

    assert len(result) == 1
    v = result[0]
    # scenes key must be stripped from the payload
    assert "scenes" not in v
    # scene_timings must be populated
    timings = v.get("scene_timings")
    assert timings is not None
    assert len(timings) == 2
    assert timings[0] == {"text": "Hello", "start_s": 0.0, "end_s": 2.5}
    assert timings[1] == {"text": "World", "start_s": 2.5, "end_s": 5.0}


def test_scene_timings_empty_for_non_sequence_variants() -> None:
    """Non-sequence variants get an empty scene_timings list (no scenes key)."""
    from app.routes.generative_jobs import _variants_for_response

    job = MagicMock()
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": "song_text",
                "render_status": "ready",
                "output_url": None,
                "text_mode": "agent_text",
                "rank": 1,
                "intro_mode": "cluster",
                # No scenes key
            }
        ]
    }

    with (
        patch("app.routes.generative_jobs.signed_get_url", side_effect=Exception("no gcs")),
        patch("app.routes.generative_jobs._TEXT_ELEMENTS_ENABLED", False),
    ):
        result = _variants_for_response(job)

    v = result[0]
    assert "scenes" not in v
    # scene_timings should be an empty list (no scenes → no timings)
    assert v.get("scene_timings") == []
