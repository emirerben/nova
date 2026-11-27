"""KRI-1616: a phone Talking card hanging past the side of the frame, from her Save to the recipe.

Failure modes, one test each:
- B1 Python and Swift disagree on the bound (both read tests/fixtures/edge-bleed-bound/v1.json);
- B2 a placement past the bound reaches a recipe (the placement and the lane card refuse it);
- B3 her drag past the frame on a bleed build is pulled back (it reaches the recipe and reads back);
- B4 her drag past the bound fails the Save (it is kept at the bound: what she sees is the card
  stopping with a quarter of it on the frame);
- B5 an older build's Save of a bled edit pulls her card back onto the frame (426 instead, nothing
  stored changes), while its Save of an edit with no bleed still works;
- B6 an older build polls a bled recipe and fails it as `invalidTimeline` (426 `edge_bleed`);
- B7 the shared card schema changes for agents, the web and cloud renders (still [0, 1]);
- B8 a Save outside a request (the copilot edit, the approval task) drops a stored bled centre;
- B9 a full-screen card takes a bled centre.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.agents._schemas.media_overlay import MediaOverlay
from app.kria.portable_visual import (
    EDGE_BLEED_KEEP_ON_CANVAS,
    VisualMediaPlacement,
    clamp_to_edge_bleed,
    within_edge_bleed,
)
from app.pipeline.phone_subtitled_lanes import SubtitledOverlayCard
from app.routes.device_render import _client_update_detail
from app.services.client_protocol import (
    INTRO_EDGE_BLEED_PROTOCOL,
    clear_request_context,
    set_client_protocol,
)
from app.services.device_render import device_status
from app.services.phone_subtitled_editor import project_phone_subtitled_editor_sections
from tests.routes.test_phone_subtitled_editor_commit import (
    _overlay_payload,
    _resave,
    phone_job,
    save,
)

FIXTURE = Path(__file__).resolve().parents[5] / "tests/fixtures/edge-bleed-bound/v1.json"
UPDATE = {
    "code": "client_update_required",
    "feature": "edge_bleed",
    "minimum_client_protocol": INTRO_EDGE_BLEED_PROTOCOL,
}


@pytest.fixture(autouse=True)
def _request_context():
    clear_request_context()
    yield
    clear_request_context()


def _from_build(protocol: int | None) -> None:
    set_client_protocol(protocol)


def _cases(kind: str) -> list[dict]:
    return json.loads(FIXTURE.read_text())[kind]


def _card_clip(job):
    track = next(
        t
        for t in device_status(job, "subtitled").request.recipe.tracks
        if t.id == "subtitled-overlays"
    )
    return track.clips[0]


def _row(job) -> dict:
    return job.assembly_plan["variants"][0]["media_overlays"][0]


def test_b1_the_bound_is_the_fixture_both_renderers_read():
    assert json.loads(FIXTURE.read_text())["keep_on_canvas"] == EDGE_BLEED_KEEP_ON_CANVAS
    for case in _cases("placements"):
        assert within_edge_bleed(case["x"], case["width"]) is case["valid"], case["name"]
    for case in _cases("drags"):
        assert clamp_to_edge_bleed(case["x"], case["width"]) == pytest.approx(case["kept"]), case


@pytest.mark.parametrize("case", _cases("placements"), ids=lambda c: c["name"])
def test_b2_a_placement_past_the_bound_never_reaches_a_recipe(case):
    placement = {"order": 1, "window_start": 0, "window_end": 1, "x_fraction": case["x"]}
    if case["width"] is not None:
        placement["width_fraction"] = case["width"]
        card = {
            "id": "c",
            "media_id": "m",
            "gcs_path": "p",
            "generation": "1",
            "start_s": 0,
            "end_s": 1,
            "x_frac": case["x"],
            "scale": case["width"],
        }
        if case["valid"]:
            SubtitledOverlayCard(**card)
        else:
            with pytest.raises(ValueError):
                SubtitledOverlayCard(**card)
    if case["valid"]:
        VisualMediaPlacement(**placement)
    else:
        with pytest.raises(ValueError):
            VisualMediaPlacement(**placement)


def test_b3_her_drag_past_the_frame_reaches_the_recipe_and_reads_back(monkeypatch):
    job = phone_job(monkeypatch)
    _from_build(INTRO_EDGE_BLEED_PROTOCOL)

    save(job, media_overlays=[_overlay_payload(x_frac=-0.05)])

    assert _row(job)["x_frac"] == -0.05
    assert _card_clip(job).visual_placement.x_fraction == -0.05
    sections = project_phone_subtitled_editor_sections(
        job.assembly_plan, job.assembly_plan["variants"][0]
    )
    assert sections["media_overlays"][0]["x_frac"] == -0.05


def test_b4_her_drag_past_the_bound_is_kept_at_the_bound_not_refused(monkeypatch):
    job = phone_job(monkeypatch)
    _from_build(INTRO_EDGE_BLEED_PROTOCOL)

    save(job, media_overlays=[_overlay_payload(x_frac=1.9)])

    kept = 1 + (0.5 - EDGE_BLEED_KEEP_ON_CANVAS) * 0.35
    assert _row(job)["x_frac"] == pytest.approx(kept)
    assert _card_clip(job).visual_placement.x_fraction == pytest.approx(kept)


def test_b5_an_older_build_cannot_pull_a_bled_card_back_but_saves_other_edits(monkeypatch):
    job = phone_job(monkeypatch)
    _from_build(INTRO_EDGE_BLEED_PROTOCOL)
    save(job, media_overlays=[_overlay_payload(x_frac=-0.05)])
    before = device_status(job, "subtitled").request
    stored = json.dumps(_row(job), sort_keys=True)

    _from_build(INTRO_EDGE_BLEED_PROTOCOL - 1)
    with pytest.raises(HTTPException) as refused:
        save(job, media_overlays=[_overlay_payload(x_frac=-0.05)])
    assert (refused.value.status_code, refused.value.detail) == (426, UPDATE)
    assert device_status(job, "subtitled").request == before
    assert json.dumps(_row(job), sort_keys=True) == stored

    fresh = phone_job(monkeypatch)
    save(fresh, media_overlays=[_overlay_payload(x_frac=0.2)])
    assert _card_clip(fresh).visual_placement.x_fraction == 0.2


def test_b6_an_older_build_polling_a_bled_recipe_is_told_to_update(monkeypatch):
    job = phone_job(monkeypatch)
    _from_build(INTRO_EDGE_BLEED_PROTOCOL)
    save(job, media_overlays=[_overlay_payload(x_frac=1.04)])
    assert _client_update_detail(job, {}, device_status(job, "subtitled")) is None

    _from_build(INTRO_EDGE_BLEED_PROTOCOL - 1)
    assert _client_update_detail(job, {}, device_status(job, "subtitled")) == UPDATE

    inside = phone_job(monkeypatch)
    save(inside, media_overlays=[_overlay_payload(x_frac=0.0)])
    assert _client_update_detail(inside, {}, device_status(inside, "subtitled")) is None


def test_b7_the_shared_card_schema_still_clamps_to_the_frame():
    card = MediaOverlay.model_validate(
        {"id": "c", "kind": "image", "src_gcs_path": "users/u/a.png", "x_frac": -0.05}
    )
    assert card.x_frac == 0.0


def test_b8_a_save_outside_a_request_keeps_a_stored_bled_centre(monkeypatch):
    job = phone_job(monkeypatch)
    _from_build(INTRO_EDGE_BLEED_PROTOCOL)
    save(job, media_overlays=[_overlay_payload(x_frac=-0.05)])
    clear_request_context()

    _resave(job, media_overlays=[_overlay_payload(x_frac=-0.05, end_s=2.5)])

    assert _row(job)["x_frac"] == -0.05
    assert _card_clip(job).visual_placement.x_fraction == -0.05


def test_b9_a_full_screen_card_takes_no_bled_centre(monkeypatch):
    monkeypatch.setattr("app.config.settings.phone_subtitled_fullscreen_overlays_enabled", True)
    job = phone_job(monkeypatch, fullscreen=True)
    _from_build(INTRO_EDGE_BLEED_PROTOCOL)

    save(job, media_overlays=[_overlay_payload(x_frac=-0.05, display_mode="fullscreen")])

    assert _row(job)["x_frac"] == 0.0
