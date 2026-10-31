"""KRI-1117: the phone Talking worker compiles a requested speaker background.

Drives `_run_generative_job` on the same Cafe T2 harness the ending-clip tests use, with the
background carried by the persisted lane request.
"""

from __future__ import annotations

import pytest

import app.services.phone_visuals as phone_visuals_mod
from app.config import settings
from app.pipeline.phone_subtitled_lanes import PHONE_SUBTITLED_LANES_FIELD
from app.services.client_protocol import clear_request_context
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    BITE,
    COTOPAXI,
    _cafe_job,
    _offline,  # noqa: F401 - autouse fixture the harness needs
    _pool_rows,
    _recipe,
)
from tests.tasks.test_phone_subtitled_narrated_dispatch import _lane_request


def _background(media_id: str, kind: str) -> dict:
    row = _pool_rows([media_id])[0]
    return {
        "media_id": media_id,
        "gcs_path": row["gcs_path"],
        "generation": "7",
        "kind": kind,
        **({"source_start_s": 1.0} if kind == "video" else {}),
    }


def _job(monkeypatch, *, enabled=True, verified=True, media_id=COTOPAXI, kind="image"):
    clear_request_context()
    monkeypatch.setattr(settings, "speaker_background_enabled", enabled)
    job = _cafe_job(
        monkeypatch,
        beats=False,
        overlay_display=None,
        lane_request={**_lane_request(), "background": _background(media_id, kind)},
    )
    features = [f for f in settings.phone_render_verified_features if f != "speakerBackground"]
    if verified:
        features.append("speakerBackground")
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", features)
    return job


def test_an_open_gate_pins_the_background_in_the_recipe_and_the_receipts(monkeypatch):
    job = _job(monkeypatch)
    gb._run_generative_job(str(job.id))

    recipe = _recipe(job)
    layer = recipe.speaker_background
    assert layer is not None and layer.asset_id == f"visual-{COTOPAXI}"
    assert "speakerBackground" in recipe.required_capabilities
    variant = job.assembly_plan["variants"][0]
    assert "background" in variant["phone_lane_receipt"]["applied"]
    assert variant["phone_lane_receipt"]["dropped"] == []
    pinned = [row["media_id"] for row in job.assembly_plan["_phone_visuals_v1"]]
    assert pinned == [COTOPAXI]


def test_a_video_background_keeps_its_start(monkeypatch):
    job = _job(monkeypatch, media_id=BITE, kind="video")
    gb._run_generative_job(str(job.id))
    layer = _recipe(job).speaker_background
    assert (layer.media_kind, layer.source_start) == ("video", 1.0)


@pytest.mark.parametrize("closed", ["flag", "token"])
def test_a_closed_gate_renders_exactly_what_a_request_that_never_asked_renders(monkeypatch, closed):
    asked = _job(monkeypatch, enabled=closed != "flag", verified=closed != "token")
    gb._run_generative_job(str(asked.id))
    never_asked = _cafe_job(monkeypatch, beats=False, overlay_display=None)
    gb._run_generative_job(str(never_asked.id))

    assert _recipe(asked).speaker_background is None
    assert _recipe(asked).model_dump_json() == _recipe(never_asked).model_dump_json()
    assert "_phone_visuals_v1" not in asked.assembly_plan
    assert asked.assembly_plan["variants"][0]["phone_lane_receipt"]["applied"] == []


def test_an_unbindable_background_drops_its_lane_and_the_edit_still_renders(monkeypatch):
    job = _job(monkeypatch)

    def _unreadable(_open, *, job_id, pins):  # noqa: ARG001
        raise ValueError("approved photo bytes are unavailable")

    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _unreadable)
    gb._run_generative_job(str(job.id))

    assert _recipe(job).speaker_background is None
    receipt = job.assembly_plan["variants"][0]["phone_lane_receipt"]
    assert receipt["dropped"] == [
        {"lane": "background", "reason": "approved photo bytes are unavailable"}
    ]


def test_a_background_kind_the_device_has_not_verified_is_dropped(monkeypatch):
    job = _job(monkeypatch, media_id=BITE, kind="video")
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [f for f in gb.settings.phone_render_verified_features if f != "visualVideos"],
    )
    gb._run_generative_job(str(job.id))
    assert _recipe(job).speaker_background is None
    dropped = job.assembly_plan["variants"][0]["phone_lane_receipt"]["dropped"]
    assert [row["lane"] for row in dropped] == ["background"]


def test_the_lane_field_name_is_unchanged():
    assert PHONE_SUBTITLED_LANES_FIELD == "_phone_subtitled_lanes_v1"
