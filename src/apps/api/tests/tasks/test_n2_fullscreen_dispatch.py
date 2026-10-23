"""Task-level coverage for a Voiceover photo followed by its own source clip."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import app.services.phone_reaction_grounding as grounding
import app.services.phone_visuals as phone_visuals
import app.tasks.generative_build as gb
from app.pipeline.narrated_photo_sequence import PhotoSequenceUnavailable
from app.services.device_render import device_status
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _beat_dict,
    _enable_beats,
    _make_fake_bind,
    _pool_asset,
    _setup_narrated,
)

_PHOTO_ID = "n2-river-photo"


def _setup_sequence(monkeypatch, *, fullscreen_enabled: bool):
    job, snapshot, _session, _bindings = _setup_narrated(monkeypatch)
    _enable_beats(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_voiceover_editor_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_voiceover_reaction_beats_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_subtitled_fullscreen_overlays_enabled",
        fullscreen_enabled,
        raising=False,
    )
    monkeypatch.setattr(
        grounding,
        "_load_ready_pool_assets",
        lambda *args, **kwargs: [_pool_asset(_PHOTO_ID, "river.jpg")],
    )
    monkeypatch.setattr(grounding, "_load_sfx_entries", lambda *args, **kwargs: [])
    bind_calls: list[dict] = []
    monkeypatch.setattr(phone_visuals, "bind_phone_visual_assets", _make_fake_bind(bind_calls))
    job.all_candidates["creator_strategy"] = {
        "reaction_beats": [
            _beat_dict(
                "river",
                "Second",
                visual_id=f"asset-{_PHOTO_ID}",
                visual_role="photo",
                display_mode="fullscreen",
                following_clip_id="c1",
            )
        ]
    }
    return job, snapshot, bind_calls


def test_voiceover_task_realizes_fullscreen_photo_then_own_video_and_records_evidence(monkeypatch):
    job, snapshot, bind_calls = _setup_sequence(monkeypatch, fullscreen_enabled=True)

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert job.status == "awaiting_device"
    recipe = device_status(job, "narrated").request.recipe
    main = next(track for track in recipe.tracks if track.id == "narrated")
    fullscreen = next(track for track in recipe.tracks if track.id == "subtitled-fullscreen")
    [photo] = fullscreen.clips
    visible = next(clip for clip in main.clips if "-after-photo-" in clip.id)
    assert visible.source_asset_id == "c1"
    assert visible.timeline_start == pytest.approx(photo.visual_placement.window_end)
    assert visible.source_start == 0

    [variant] = job.assembly_plan["variants"]
    [placed] = variant["phone_beat_receipt"]["placed"]
    assert placed["sequence_photo_id"] == _PHOTO_ID
    assert placed["following_clip_id"] == "c1"
    assert placed["following_at_s"] == pytest.approx(photo.visual_placement.window_end)
    assert placed["following_source_start_s"] == 0
    assert placed["sequence_realized"] is True
    assert bind_calls == [{_PHOTO_ID: ("image", f"users/u1/plan/item1/pool/{_PHOTO_ID}.jpg", "7")}]


def test_voiceover_task_recovers_when_the_requested_fullscreen_photo_is_dropped(monkeypatch):
    job, snapshot, _bind_calls = _setup_sequence(monkeypatch, fullscreen_enabled=False)
    recovery = Mock(return_value=True)
    monkeypatch.setattr(gb, "_save_request_recovery", recovery)

    with pytest.raises(PhotoSequenceUnavailable, match="full-screen photo followed by its video"):
        gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert job.status != "awaiting_device"
    assert "_device_render_v1" not in job.assembly_plan
    assert recovery.call_count == 1
    assert "full-screen photo followed by its video" in recovery.call_args.kwargs["message"]
