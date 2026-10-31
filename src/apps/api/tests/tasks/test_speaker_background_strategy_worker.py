"""KRI-1117 (chat): the phone Talking worker resolves the strategy's ``background_media``.

Same Cafe T2 harness as the lane-request tests (`test_speaker_background_worker.py`), with the
background named by the approved creator strategy instead of a hand-written lane request.
Failure modes pinned here: the strategy's background never reaches the recipe; the same photo is
ALSO placed as a mid-talk card (reservation); a closed phone gate leaves a different recipe than
a strategy that never asked; an id outside this item's ready pool is bound anyway; a hand-written
lane request is overridden by the agent.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.client_protocol import clear_request_context
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    BITE,
    COTOPAXI,
    OBLEA,
    _assets_in_tracks,
    _cafe_job,
    _offline,  # noqa: F401 - autouse fixture the harness needs
    _recipe,
)
from tests.tasks.test_phone_subtitled_narrated_dispatch import _lane_request


def _job(monkeypatch, *, background, enabled=True, verified=True, **kwargs):
    clear_request_context()
    monkeypatch.setattr(settings, "speaker_background_enabled", enabled)
    kwargs.setdefault("beats", False)
    kwargs.setdefault("overlay_display", "fullscreen_images")
    job = _cafe_job(monkeypatch, **kwargs)
    features = [f for f in settings.phone_render_verified_features if f != "speakerBackground"]
    if verified:
        features.append("speakerBackground")
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", features)
    if background is not None:
        job.all_candidates["creator_strategy"]["background_media"] = background
    return job


def _card_media(job) -> set[str]:
    receipt = job.assembly_plan["variants"][0].get("phone_overlay_receipt") or {}
    return {entry["media_id"] for entry in receipt.get("placed", [])}


def test_the_strategys_background_reaches_the_recipe_and_the_receipts(monkeypatch):
    job = _job(monkeypatch, background={"media_id": f"asset-{COTOPAXI}"})
    gb._run_generative_job(str(job.id))

    layer = _recipe(job).speaker_background
    assert layer is not None and layer.asset_id == f"visual-{COTOPAXI}"
    receipt = job.assembly_plan["variants"][0]["phone_lane_receipt"]
    assert "background" in receipt["applied"]
    assert receipt["dropped"] == []


def test_a_video_background_keeps_the_start_the_strategy_named(monkeypatch):
    job = _job(monkeypatch, background={"media_id": f"asset-{BITE}", "source_start_s": 1.5})
    gb._run_generative_job(str(job.id))

    layer = _recipe(job).speaker_background
    assert (layer.media_kind, layer.source_start) == ("video", 1.5)


def test_the_background_photo_is_never_also_a_mid_talk_card(monkeypatch):
    baseline = _job(monkeypatch, background=None)
    gb._run_generative_job(str(baseline.id))
    assert COTOPAXI in _card_media(baseline), "the harness must offer the photo as a card first"

    job = _job(monkeypatch, background={"media_id": f"asset-{COTOPAXI}"})
    gb._run_generative_job(str(job.id))

    receipt = job.assembly_plan["variants"][0]["phone_overlay_receipt"]
    offered = {entry["media_id"] for entry in receipt["placed"] + receipt["unplaced"]}
    assert COTOPAXI not in offered
    recipe = _recipe(job)
    cards = {
        clip.source_asset_id
        for track in recipe.tracks
        if track.id != "subtitled"
        for clip in track.clips
    }
    assert f"visual-{COTOPAXI}" not in cards
    assert recipe.speaker_background is not None
    # The other pool photos still take their cards.
    assert OBLEA in _card_media(job) or f"visual-{OBLEA}" in _assets_in_tracks(recipe)


@pytest.mark.parametrize("closed", ["flag", "token"])
def test_a_closed_gate_renders_exactly_what_a_strategy_that_never_asked_renders(
    monkeypatch, closed
):
    asked = _job(
        monkeypatch,
        background={"media_id": f"asset-{OBLEA}"},
        enabled=closed != "flag",
        verified=closed != "token",
    )
    gb._run_generative_job(str(asked.id))
    never = _job(monkeypatch, background=None, enabled=closed != "flag", verified=closed != "token")
    gb._run_generative_job(str(never.id))

    assert _recipe(asked).speaker_background is None
    assert _recipe(asked).model_dump_json() == _recipe(never).model_dump_json()
    assert _card_media(asked) == _card_media(never)


def test_an_id_outside_this_items_ready_pool_is_dropped_with_a_receipt(monkeypatch):
    foreign = "asset-00000000-0000-4000-8000-0000000000ff"
    job = _job(monkeypatch, background={"media_id": foreign})
    gb._run_generative_job(str(job.id))

    assert _recipe(job).speaker_background is None
    receipt = job.assembly_plan["variants"][0]["phone_lane_receipt"]
    assert {"lane": "background", "reason": "not_in_ready_pool"} in receipt["dropped"]
    assert job.status == "awaiting_device"


def test_a_hand_written_lane_request_background_wins_over_the_strategys(monkeypatch):
    from tests.tasks.test_speaker_background_worker import _background

    job = _job(
        monkeypatch,
        background={"media_id": f"asset-{OBLEA}"},
        lane_request={**_lane_request(), "background": _background(COTOPAXI, "image")},
    )
    gb._run_generative_job(str(job.id))

    layer = _recipe(job).speaker_background
    assert layer is not None and layer.asset_id == f"visual-{COTOPAXI}"
    dropped = job.assembly_plan["variants"][0]["phone_lane_receipt"]["dropped"]
    assert {"lane": "background", "reason": "lane_request_background_wins"} in dropped
