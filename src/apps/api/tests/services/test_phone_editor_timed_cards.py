"""Failure mode 11: a guided text-only Save keeps the exact-timed small card the worker pinned.

The guided editor recompiles from the plan, which never carried the card; the Save re-attaches
the persisted `phone_timed_cards` rows while the lane is on, and drops the lane (never a 422)
when the flag is off.
"""

from __future__ import annotations

from app.config import settings
from app.pipeline.phone_subtitled_lanes import SubtitledOverlayCard
from app.pipeline.phone_timed_cards import TIMED_CARDS_FIELD, timed_card_rows
from app.services.device_render import device_status
from tests.routes.test_phone_editor_commit import phone_job, save
from tests.services import test_phone_visuals as photos


def _job_with_card(monkeypatch, *, flag: bool):
    job = phone_job(monkeypatch, photo=True)
    card = SubtitledOverlayCard(
        id="timed-menu",
        media_id=photos.PHOTO_ID,
        gcs_path=photos.PHOTO_PATH,
        generation=photos.PHOTO_GENERATION,
        start_s=0.5,
        end_s=2.0,
        x_frac=0.74,
        y_frac=0.2,
        scale=0.38,
        fade=True,
    )
    job.assembly_plan["variants"][0][TIMED_CARDS_FIELD] = timed_card_rows([card])
    monkeypatch.setattr(settings, "phone_timed_cards_enabled", flag, raising=False)
    monkeypatch.setattr(settings, "media_overlays_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "visualBlocks", "alphaOverlay"],
    )
    return job


def _overlay_clips(job):
    recipe = device_status(job, "guided_story").request.recipe
    track = next((t for t in recipe.tracks if t.id == "subtitled-overlays"), None)
    return recipe, (list(track.clips) if track is not None else [])


def test_guided_text_only_save_keeps_the_timed_card(monkeypatch):
    job = _job_with_card(monkeypatch, flag=True)
    prep = save(job)
    assert prep["render_destination"] == "device"
    recipe, clips = _overlay_clips(job)
    (clip,) = clips
    assert clip.id == "subtitled-overlay-timed-menu"
    assert (clip.timeline_start, clip.visual_placement.window_end) == (0.5, 2.0)
    assert {"visualBlocks", "alphaOverlay"} <= set(recipe.required_capabilities)
    assert device_status(job, "guided_story").request.identity.recipe_revision == 2
    # The rows stay on the variant for the next Save.
    assert job.assembly_plan["variants"][0][TIMED_CARDS_FIELD][0]["id"] == "timed-menu"


def test_guided_save_with_the_flag_off_drops_the_lane_without_refusing(monkeypatch):
    job = _job_with_card(monkeypatch, flag=False)
    prep = save(job)
    assert prep["render_destination"] == "device"
    _recipe, clips = _overlay_clips(job)
    assert clips == []
