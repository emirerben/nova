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
    item.voiceover_gcs_path = None
    item.voiceover_bed_level = None
    item.voiceover_caption_style = None
    item.edit_format = None
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


# ── synchronous render_status="rendering" mutation ───────────────────────────
# These guard the backend fix: dispatch_swap_song and dispatch_retext must
# set render_status="rendering" on the variant synchronously (before .delay()),
# mirroring dispatch_set_media_overlays.  Without this fix the frontend's
# pending-edits pin is the only barrier and the 409 lock has a race window.


def test_swap_song_sets_rendering_synchronously(client: TestClient) -> None:
    """After POST swap-song, the variant in the reload response is rendering."""
    user = _user()
    job = _job([dict(SONG_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    track = MagicMock(analysis_status="ready", audio_gcs_path="music/x.mp3")
    db = _db([item, track, item], plan)
    _override(user, db)

    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        client.post(
            f"/plan-items/{item.id}/variants/song_text/swap-song",
            json={"new_track_id": "track-123"},
        )

    # The job's assembly_plan must reflect render_status="rendering" before .delay().
    variants = job.assembly_plan["variants"]
    match = next((v for v in variants if v["variant_id"] == "song_text"), None)
    assert match is not None, "variant not found in assembly_plan"
    assert match["render_status"] == "rendering", (
        "dispatch_swap_song must set render_status='rendering' synchronously "
        f"(got {match['render_status']!r})"
    )
    # And the commit was called (persists to DB before worker fires).
    db.commit.assert_awaited()


def test_retext_sets_rendering_synchronously(client: TestClient) -> None:
    """After POST retext, the variant in the reload response is rendering."""
    user = _user()
    job = _job([dict(SONG_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)

    with patch(REGEN) as regen:
        regen.delay = MagicMock()
        client.post(
            f"/plan-items/{item.id}/variants/song_text/retext",
            json={"text": "new hook"},
        )

    variants = job.assembly_plan["variants"]
    match = next((v for v in variants if v["variant_id"] == "song_text"), None)
    assert match is not None, "variant not found in assembly_plan"
    assert match["render_status"] == "rendering", (
        "dispatch_retext must set render_status='rendering' synchronously "
        f"(got {match['render_status']!r})"
    )
    db.commit.assert_awaited()


def test_second_swap_song_returns_409(client: TestClient) -> None:
    """A swap-song on an already-rendering variant returns 409 immediately."""
    user = _user()
    rendering_variant = {**SONG_VARIANT, "render_status": "rendering"}
    job = _job([rendering_variant])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)

    resp = client.post(
        f"/plan-items/{item.id}/variants/song_text/swap-song",
        json={"new_track_id": "track-456"},
    )
    assert resp.status_code == 409, (
        "Second swap-song on a rendering variant must return 409"
    )


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


# ── captions (on-video caption editor) ───────────────────────────────────────

NARRATED_VARIANT = {
    "variant_id": "narrated",
    "render_status": "ready",
    "rank": 1,
    "resolved_archetype": "narrated",
    "base_video_path": "generative-jobs/j/variant_1_narrated_base.mp4",
    "music_track_id": None,
}
REBURN = "app.tasks.generative_build.reburn_narrated_captions"


def test_is_editable_caption_variant_guard() -> None:
    from app.routes.generative_jobs import (
        _CAPTION_EDIT_ARCHETYPES,
        _is_editable_caption_variant,
    )

    assert _is_editable_caption_variant(NARRATED_VARIANT) is True
    # subtitled single-clip is also an editable caption variant.
    assert _is_editable_caption_variant({**NARRATED_VARIANT, "resolved_archetype": "subtitled"})
    # montage agent_text base also has base_video_path — must NOT be editable.
    assert (
        _is_editable_caption_variant({**NARRATED_VARIANT, "resolved_archetype": "montage"}) is False
    )
    # narrated but no base → not editable yet
    assert _is_editable_caption_variant({"resolved_archetype": "narrated"}) is False
    # subtitled but no base → not editable yet
    assert _is_editable_caption_variant({"resolved_archetype": "subtitled"}) is False
    # Route gate must stay in lockstep with the worker's reburn guard.
    assert _CAPTION_EDIT_ARCHETYPES == frozenset({"narrated", "subtitled"})


def test_caption_cue_rejects_infinity() -> None:
    from pydantic import ValidationError

    from app.routes.generative_jobs import CaptionsRequest

    with pytest.raises(ValidationError):
        CaptionsRequest.model_validate_json(
            '{"cues":[{"text":"x","start_s":1.0,"end_s":Infinity}]}'
        )


def test_captions_request_caps_cue_count() -> None:
    from pydantic import ValidationError

    from app.routes.generative_jobs import CaptionsRequest

    many = [{"text": "x", "start_s": float(i), "end_s": float(i) + 1} for i in range(301)]
    with pytest.raises(ValidationError):
        CaptionsRequest(cues=many)
    # 300 is allowed
    CaptionsRequest(cues=many[:300])


RETX = "app.tasks.generative_build.retranscribe_subtitled_captions"
SUBTITLED_VARIANT = {
    **NARRATED_VARIANT,
    "variant_id": "subtitled",
    "resolved_archetype": "subtitled",
    "caption_language": "en",
}


def test_caption_language_happy_enqueues_retranscribe(client: TestClient) -> None:
    user = _user()
    job = _job([dict(SUBTITLED_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    with patch(RETX) as retx:
        retx.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/subtitled/caption-language",
            json={"language": "tr"},
        )
    assert resp.status_code == 200
    retx.delay.assert_called_once_with(str(job.id), "subtitled", "tr")


def test_caption_language_marks_rendering_synchronously(client: TestClient) -> None:
    """The 409 gate must close AT DISPATCH — otherwise two calls in the enqueue window
    both pass and race to a last-writer-wins variant state."""
    user = _user()
    job = _job([dict(SUBTITLED_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    with patch(RETX) as retx:
        retx.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/subtitled/caption-language",
            json={"language": "tr"},
        )
    assert resp.status_code == 200
    assert job.assembly_plan["variants"][0]["render_status"] == "rendering"
    # The mutation must be COMMITTED — get_db rolls back on exit, so without an
    # explicit commit the gate-close silently evaporates (Red Team finding).
    assert db.commit.await_count >= 1


def test_apply_captions_marks_rendering_synchronously(client: TestClient) -> None:
    user = _user()
    job = _job([dict(NARRATED_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)
    _override(user, db)
    with patch(REBURN) as reburn:
        reburn.delay = MagicMock()
        resp = client.post(f"/plan-items/{item.id}/variants/narrated/captions/apply")
    assert resp.status_code == 200
    assert job.assembly_plan["variants"][0]["render_status"] == "rendering"
    assert db.commit.await_count >= 1  # persisted, not rolled back


def test_caption_cue_caps_text_length() -> None:
    from pydantic import ValidationError

    from app.routes.generative_jobs import CaptionCue

    with pytest.raises(ValidationError):
        CaptionCue(text="x" * 601, start_s=0.0, end_s=1.0)


def test_caption_language_rejects_non_subtitled(client: TestClient) -> None:
    user = _user()
    job = _job([dict(NARRATED_VARIANT)])  # narrated: no language override
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)
    resp = client.post(
        f"/plan-items/{item.id}/variants/narrated/caption-language",
        json={"language": "tr"},
    )
    assert resp.status_code == 422


def test_caption_language_rejects_missing_base(client: TestClient) -> None:
    user = _user()
    no_base = {**SUBTITLED_VARIANT, "base_video_path": None}
    job = _job([no_base])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)
    resp = client.post(
        f"/plan-items/{item.id}/variants/subtitled/caption-language",
        json={"language": "tr"},
    )
    assert resp.status_code == 422


def test_caption_language_rejects_unknown_language(client: TestClient) -> None:
    user = _user()
    job = _job([dict(SUBTITLED_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)
    _override(user, db)
    resp = client.post(
        f"/plan-items/{item.id}/variants/subtitled/caption-language",
        json={"language": "de"},  # outside the en/tr Literal
    )
    assert resp.status_code == 422


def test_apply_captions_happy(client: TestClient) -> None:
    user = _user()
    job = _job([dict(NARRATED_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, item], plan)  # load item → reload for response
    _override(user, db)
    with patch(REBURN) as reburn:
        reburn.delay = MagicMock()
        resp = client.post(f"/plan-items/{item.id}/variants/narrated/captions/apply")
    assert resp.status_code == 200
    reburn.delay.assert_called_once_with(str(job.id), "narrated")


def test_apply_captions_rejects_non_narrated_variant(client: TestClient) -> None:
    user = _user()
    # montage variant with a base_video_path — must be refused (422), never reburned.
    montage = {**NARRATED_VARIANT, "variant_id": "original_text", "resolved_archetype": "montage"}
    job = _job([montage])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)  # rejected in dispatch, before any reload
    _override(user, db)
    with patch(REBURN) as reburn:
        reburn.delay = MagicMock()
        resp = client.post(f"/plan-items/{item.id}/variants/original_text/captions/apply")
    assert resp.status_code == 422
    reburn.delay.assert_not_called()


def test_edit_captions_persists_cues(client: TestClient) -> None:
    user = _user()
    job = _job([dict(NARRATED_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    # load item → persist's FOR-UPDATE select returns the job → reload item
    db = _db([item, job, item], plan)
    _override(user, db)
    resp = client.patch(
        f"/plan-items/{item.id}/variants/narrated/captions",
        json={"cues": [{"text": "could", "start_s": 0.0, "end_s": 1.0}]},
    )
    assert resp.status_code == 200
    assert job.assembly_plan["variants"][0]["caption_cues"] == [
        {"text": "could", "start_s": 0.0, "end_s": 1.0}
    ]


# ── caption font (editor font picker for narrated captions) ───────────────────

_VALID_CAPTION_FONT = "TikTok Sans Bold"  # a known non-deprecated registry font


def test_set_caption_font_persists(client: TestClient) -> None:
    user = _user()
    job = _job([dict(NARRATED_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, job, item], plan)  # load → FOR-UPDATE job → reload
    _override(user, db)
    resp = client.patch(
        f"/plan-items/{item.id}/variants/narrated/caption-font",
        json={"caption_font": _VALID_CAPTION_FONT},
    )
    assert resp.status_code == 200
    assert job.assembly_plan["variants"][0]["voiceover_caption_font"] == _VALID_CAPTION_FONT


def test_set_caption_font_null_resets(client: TestClient) -> None:
    user = _user()
    job = _job([{**NARRATED_VARIANT, "voiceover_caption_font": "Montserrat"}])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, job, item], plan)
    _override(user, db)
    resp = client.patch(
        f"/plan-items/{item.id}/variants/narrated/caption-font",
        json={"caption_font": None},
    )
    assert resp.status_code == 200
    assert job.assembly_plan["variants"][0]["voiceover_caption_font"] is None


def test_set_caption_font_rejects_unknown(client: TestClient) -> None:
    user = _user()
    job = _job([dict(NARRATED_VARIANT)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan)  # validation 422s before the FOR-UPDATE select
    _override(user, db)
    resp = client.patch(
        f"/plan-items/{item.id}/variants/narrated/caption-font",
        json={"caption_font": "Not A Real Font"},
    )
    assert resp.status_code == 422
    assert "voiceover_caption_font" not in job.assembly_plan["variants"][0]


def test_set_caption_font_rejects_non_narrated(client: TestClient) -> None:
    user = _user()
    montage = {**NARRATED_VARIANT, "variant_id": "original_text", "resolved_archetype": "montage"}
    job = _job([montage])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, job], plan)  # font valid → FOR-UPDATE → narrated-gate 422
    _override(user, db)
    resp = client.patch(
        f"/plan-items/{item.id}/variants/original_text/caption-font",
        json={"caption_font": _VALID_CAPTION_FONT},
    )
    assert resp.status_code == 422


def test_set_caption_font_409_while_rendering(client: TestClient) -> None:
    # A font edit must NOT race an in-flight reburn — the shared lock+guard ladder
    # 409s while render_status=='rendering'.
    user = _user()
    job = _job([{**NARRATED_VARIANT, "render_status": "rendering"}])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, job], plan)  # load item → FOR-UPDATE job → 409 (rendering)
    _override(user, db)
    resp = client.patch(
        f"/plan-items/{item.id}/variants/narrated/caption-font",
        json={"caption_font": _VALID_CAPTION_FONT},
    )
    assert resp.status_code == 409


def test_edit_captions_409_while_rendering(client: TestClient) -> None:
    user = _user()
    job = _job([{**NARRATED_VARIANT, "render_status": "rendering"}])
    item, plan = _owned_item(user.id, job=job)
    db = _db([item, job], plan)
    _override(user, db)
    resp = client.patch(
        f"/plan-items/{item.id}/variants/narrated/captions",
        json={"cues": [{"text": "x", "start_s": 0.0, "end_s": 1.0}]},
    )
    assert resp.status_code == 409
