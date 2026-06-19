"""Route tests for plan-item per-variant editing (swap-song / retext / change-style).

Mock-DB style, mirroring test_plan_item_generation.py. These endpoints add only
ownership enforcement + job resolution on top of the shared validate-and-dispatch
helpers in routes/generative_jobs.py, so the happy paths assert the right
`regenerate_generative_variant.delay(...)` kwargs and the error paths assert the
shared validation rules fire identically to the generative surface.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.pipeline.style_sets import style_set_ids
from app.routes.generative_jobs import (
    dispatch_retext,
    require_editable_variant,
)

REGEN = "app.tasks.generative_build.regenerate_generative_variant"


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
    item.edit_format = "montage"
    plan = MagicMock()
    plan.user_id = user_id
    return item, plan


def _db(execute_results: list, plan) -> AsyncMock:
    """db.execute() yields the given scalar_one_or_none values in order; db.get()
    (the ContentPlan ownership check + reload) always returns `plan`."""
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


SONG_VARIANT = {
    "variant_id": "song_text",
    "music_track_id": "track-123",
    "render_status": "ready",
    "rank": 2,
    "style_set_id": "default",
}
ORIGINAL_VARIANT = {
    "variant_id": "original_text",
    "music_track_id": None,
    "render_status": "ready",
    "rank": 3,
}


def _override(user, db) -> None:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


# ── swap-song ────────────────────────────────────────────────────────────────


def test_swap_song_happy_path(client: TestClient) -> None:
    user = _user()
    job = _job([dict(SONG_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    track = MagicMock(analysis_status="ready", audio_gcs_path="music/x.mp3")
    # execute order: load item → load track → reload item (for the response).
    db = _db([item, track, item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/swap-song",
            json={"new_track_id": "track-123"},
        )
    assert resp.status_code == 200
    regen.delay.assert_called_once_with(str(job.id), "song_text", new_track_id="track-123")


def test_swap_song_rejects_original_variant(client: TestClient) -> None:
    user = _user()
    job = _job([dict(ORIGINAL_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)  # rejected before any track lookup
    _override(user, db)
    resp = client.post(
        f"/plan-items/{item.id}/variants/original_text/swap-song",
        json={"new_track_id": "track-123"},
    )
    assert resp.status_code == 422


def test_swap_song_rejects_unready_track(client: TestClient) -> None:
    user = _user()
    job = _job([dict(SONG_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    track = MagicMock(analysis_status="analyzing", audio_gcs_path=None)
    db = _db([item, track], plan)
    _override(user, db)
    resp = client.post(
        f"/plan-items/{item.id}/variants/song_text/swap-song",
        json={"new_track_id": "track-123"},
    )
    assert resp.status_code == 422


# ── retext ───────────────────────────────────────────────────────────────────


def test_retext_happy_path(client: TestClient) -> None:
    user = _user()
    job = _job([dict(SONG_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/retext",
            json={"text": "  new hook  "},
        )
    assert resp.status_code == 200
    regen.delay.assert_called_once_with(
        str(job.id), "song_text", override_text="new hook", remove_text=False
    )


def test_retext_remove_happy_path(client: TestClient) -> None:
    user = _user()
    job = _job([dict(SONG_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/retext",
            json={"remove": True},
        )
    assert resp.status_code == 200
    regen.delay.assert_called_once_with(
        str(job.id), "song_text", override_text=None, remove_text=True
    )


def test_retext_requires_text_or_remove(client: TestClient) -> None:
    user = _user()
    job = _job([dict(SONG_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)
    resp = client.post(f"/plan-items/{item.id}/variants/song_text/retext", json={})
    assert resp.status_code == 422


# ── change-style ───────────────────────────────────────────────────────────────


def test_change_style_happy_path(client: TestClient) -> None:
    user = _user()
    job = _job([dict(SONG_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    valid_style = style_set_ids(applies_to="generative")[0]
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/change-style",
            json={"style_set_id": valid_style},
        )
    assert resp.status_code == 200
    regen.delay.assert_called_once_with(str(job.id), "song_text", style_set_id=valid_style)


def test_change_style_rejects_unknown_style(client: TestClient) -> None:
    user = _user()
    job = _job([dict(SONG_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)
    resp = client.post(
        f"/plan-items/{item.id}/variants/song_text/change-style",
        json={"style_set_id": "definitely-not-a-style"},
    )
    assert resp.status_code == 422


# ── intro-size ───────────────────────────────────────────────────────────────

# The size nudge only applies to the AI-intro text variant; SONG_VARIANT alone
# lacks text_mode, so these add it explicitly.
_AGENT_TEXT_VARIANT = {**SONG_VARIANT, "text_mode": "agent_text"}


def test_intro_size_happy_path(client: TestClient) -> None:
    user = _user()
    job = _job([dict(_AGENT_TEXT_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/intro-size",
            json={"text_size_px": 72},
        )
    assert resp.status_code == 200
    regen.delay.assert_called_once_with(str(job.id), "song_text", size_override_px=72)


def test_intro_size_clamps_to_envelope(client: TestClient) -> None:
    user = _user()
    job = _job([dict(_AGENT_TEXT_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/intro-size",
            json={"text_size_px": 9999},
        )
    assert resp.status_code == 200
    # over the ceiling → clamped server-side to MAX_INTRO_PX (80)
    assert regen.delay.call_args.kwargs["size_override_px"] == 80


def test_intro_size_rejects_non_agent_text_variant(client: TestClient) -> None:
    # A lyrics variant has no resizable AI intro → 422, no re-render queued.
    user = _user()
    job = _job([{**SONG_VARIANT, "text_mode": "lyrics"}])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/intro-size",
            json={"text_size_px": 72},
        )
    assert resp.status_code == 422
    regen.delay.assert_not_called()


# ── ownership + state guards ─────────────────────────────────────────────────


def test_edit_404_when_not_owner(client: TestClient) -> None:
    user = _user()
    job = _job([dict(SONG_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    plan.user_id = uuid.uuid4()  # different user owns the plan
    db = _db([item], plan)
    _override(user, db)
    resp = client.post(f"/plan-items/{item.id}/variants/song_text/retext", json={"text": "x"})
    assert resp.status_code == 404


def test_edit_404_when_no_render_job(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id, job=None)
    db = _db([item], plan)
    _override(user, db)
    resp = client.post(f"/plan-items/{item.id}/variants/song_text/retext", json={"text": "x"})
    assert resp.status_code == 404


def test_edit_404_when_variant_unknown(client: TestClient) -> None:
    user = _user()
    job = _job([dict(SONG_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)
    resp = client.post(f"/plan-items/{item.id}/variants/no-such-variant/retext", json={"text": "x"})
    assert resp.status_code == 404


def test_edit_409_when_variant_rendering(client: TestClient) -> None:
    user = _user()
    rendering = {**SONG_VARIANT, "render_status": "rendering"}
    job = _job([rendering])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)
    resp = client.post(f"/plan-items/{item.id}/variants/song_text/retext", json={"text": "x"})
    assert resp.status_code == 409


# ── shared-helper unit guards (single-sourced validation) ─────────────────────


def test_require_editable_variant_raises_404_unknown() -> None:
    job = _job([dict(SONG_VARIANT)])
    with pytest.raises(HTTPException) as exc:
        require_editable_variant(job, "nope")
    assert exc.value.status_code == 404


def test_require_editable_variant_raises_409_rendering() -> None:
    job = _job([{**SONG_VARIANT, "render_status": "rendering"}])
    with pytest.raises(HTTPException) as exc:
        require_editable_variant(job, "song_text")
    assert exc.value.status_code == 409


def test_dispatch_retext_requires_text_or_remove() -> None:
    job = _job([dict(SONG_VARIANT)])
    with pytest.raises(HTTPException) as exc:
        dispatch_retext(job, "song_text", text=None, remove=False)
    assert exc.value.status_code == 422


# ── combined /edit (intro layout pick) ────────────────────────────────────────


TEXT_VARIANT = {
    "variant_id": "song_text",
    "music_track_id": "track-123",
    "render_status": "ready",
    "rank": 2,
    "style_set_id": "default",
    "text_mode": "agent_text",
    "intro_text": "what's your favorite place?",
}


def test_edit_intro_layout_happy_path(client: TestClient) -> None:
    user = _user()
    job = _job([dict(TEXT_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/edit",
            json={"intro_layout": "cluster"},
        )
    assert resp.status_code == 200
    regen.delay.assert_called_once_with(
        str(job.id),
        "song_text",
        override_text=None,
        remove_text=False,
        style_set_id=None,
        size_override_px=None,
        layout_override="cluster",
        font_family_override=None,
        effect_override=None,
        text_color_override=None,
        cluster_hero_font_override=None,
        cluster_body_font_override=None,
        cluster_accent_font_override=None,
        cluster_hero_size_px_override=None,
        cluster_body_size_px_override=None,
        cluster_accent_size_px_override=None,
    )


def test_edit_accepts_full_batch_payload(client: TestClient) -> None:
    """W6: the plan /edit route accepts the SAME batch fields as the generative
    /edit (text + style_set_id + text_size_px in one request) so the instant
    editor commits the whole session as one re-render. Asserts every field
    threads through to regenerate_generative_variant.delay(...)."""
    user = _user()
    job = _job([dict(TEXT_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    valid_style = style_set_ids(applies_to="generative")[0]
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/edit",
            json={
                "text": "  brand new hook  ",
                "style_set_id": valid_style,
                "text_size_px": 64,
            },
        )
    assert resp.status_code == 200
    regen.delay.assert_called_once_with(
        str(job.id),
        "song_text",
        override_text="brand new hook",
        remove_text=False,
        style_set_id=valid_style,
        size_override_px=64,
        layout_override=None,
        font_family_override=None,
        effect_override=None,
        text_color_override=None,
        cluster_hero_font_override=None,
        cluster_body_font_override=None,
        cluster_accent_font_override=None,
        cluster_hero_size_px_override=None,
        cluster_body_size_px_override=None,
        cluster_accent_size_px_override=None,
    )


def test_edit_accepts_remove_text_batch(client: TestClient) -> None:
    """W6: remove_text is honored on the plan /edit route (mutually exclusive
    with text — the instant editor sends one or the other)."""
    user = _user()
    job = _job([dict(TEXT_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/edit",
            json={"remove_text": True},
        )
    assert resp.status_code == 200
    assert regen.delay.call_args.kwargs["remove_text"] is True
    assert regen.delay.call_args.kwargs["override_text"] is None


def test_edit_intro_layout_rejects_wordy_hook(client: TestClient) -> None:
    user = _user()
    variant = dict(TEXT_VARIANT, intro_text="when they don't even listen to your feelings")
    job = _job([variant])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/edit",
            json={"intro_layout": "cluster"},
        )
    assert resp.status_code == 422
    assert "3-6 word" in resp.json()["detail"]
    regen.delay.assert_not_called()


def test_edit_requires_at_least_one_field(client: TestClient) -> None:
    user = _user()
    job = _job([dict(TEXT_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)
    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        resp = client.post(f"/plan-items/{item.id}/variants/song_text/edit", json={})
    assert resp.status_code == 422
    regen.delay.assert_not_called()
