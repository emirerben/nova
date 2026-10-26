"""Exact-timed small cards through the phone guided writer (failure modes 1, 4, 9, 10, 11).

The real dispatcher (`_run_generative_job`) hands the confirmed strategy to
`_run_phone_guided_job`, which grounds the cards against the compiled story clock, binds the
photo from the job's ready pool and pins a recipe whose `subtitled-overlays` lane carries the
card. Flag off: the pinned recipe is byte-identical to a run with no cards at all.
"""

from __future__ import annotations

from app.kria.device_render import recipe_digest
from app.pipeline.phone_timed_cards import TIMED_CARD_RECEIPT_FIELD, TIMED_CARDS_FIELD
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_VISUALS_FIELD
from app.tasks import generative_build as gb
from tests.services import test_phone_visuals as photos
from tests.tasks.test_phone_guided_dispatch import setup

_FEATURES = [
    "basicComposition",
    "local1080Export",
    "audioMix",
    "stillImages",
    "visualBlocks",
    "alphaOverlay",
]


def _card(**overrides):
    row = {
        "card_id": "menu",
        "visual_id": f"asset-{photos.PHOTO_ID}",
        "label": "menu photo",
        "at_s": 1.0,
        "hold_s": 1.5,
    }
    row.update(overrides)
    return row


def _timed_setup(monkeypatch, *, flag=True, strategy=None):
    job, snapshot, session, _planner, cloud = setup(monkeypatch)
    job.user_id = photos.USER_ID
    job.content_plan_item_id = photos.ITEM_ID
    job.all_candidates = {"creator_strategy": strategy if strategy is not None else {}}
    session.get.return_value = job
    session.execute.return_value.scalars.return_value.all.return_value = [
        photos.photo_row(source_filename="menu.jpg", duration_s=None, aspect=None, analysis={})
    ]
    photos.fake_storage(
        monkeypatch, {(photos.PHOTO_PATH, photos.PHOTO_GENERATION): photos.PHOTO_BYTES}
    )
    monkeypatch.setattr(gb.settings, "phone_timed_cards_enabled", flag, raising=False)
    monkeypatch.setattr(gb.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", list(_FEATURES))
    return job, snapshot, cloud


def _overlay_clips(job):
    recipe = device_status(job, "guided_story").request.recipe
    track = next((t for t in recipe.tracks if t.id == "subtitled-overlays"), None)
    return recipe, (list(track.clips) if track is not None else [])


def test_dispatcher_grounds_the_card_binds_the_photo_and_pins_the_lane(monkeypatch):
    job, _snapshot, cloud = _timed_setup(monkeypatch, strategy={"timed_cards": [_card()]})
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    recipe, clips = _overlay_clips(job)
    (clip,) = clips
    assert (clip.timeline_start, clip.visual_placement.window_end) == (1.0, 2.5)
    assert clip.volume == 0 and clip.source_asset_id == f"visual-{photos.PHOTO_ID}"
    assert {"visualBlocks", "alphaOverlay", "audioMix", "stillImages"} <= set(
        recipe.required_capabilities
    )
    variant = job.assembly_plan["variants"][0]
    receipt = variant[TIMED_CARD_RECEIPT_FIELD]
    assert receipt["matcher"] == "timed" and receipt["unplaced"] == []
    (placed,) = receipt["placed"]
    assert (placed["card_id"], placed["at_s"], placed["end_s"], placed["kind"]) == (
        "menu",
        1.0,
        2.5,
        "image",
    )
    assert variant[TIMED_CARDS_FIELD][0]["media_id"] == photos.PHOTO_ID
    assert job.assembly_plan[PHONE_VISUALS_FIELD][0]["media_id"] == photos.PHOTO_ID
    cloud.assert_not_called()


def test_clip_anchor_lands_on_the_compiled_clip_and_a_late_card_is_reported(monkeypatch):
    """1 / 4: "during the clip" = that clip's compiled start + offset; past the end = receipt."""
    cards = [
        _card(card_id="during", at_s=None, clip_id="source", offset_s=0.5, hold_s=1.0),
        _card(card_id="late", at_s=2.9, hold_s=1.0),
    ]
    job, _snapshot, _cloud = _timed_setup(monkeypatch, strategy={"timed_cards": cards})
    gb._run_generative_job(str(job.id))
    _recipe, clips = _overlay_clips(job)
    (clip,) = clips
    assert (clip.timeline_start, clip.visual_placement.window_end) == (0.5, 1.5)
    receipt = job.assembly_plan["variants"][0][TIMED_CARD_RECEIPT_FIELD]
    assert [p["card_id"] for p in receipt["placed"]] == ["during"]
    (late,) = receipt["unplaced"]
    assert (late["card_id"], late["reason"], late["timeline_end_s"]) == ("late", "past_end", 3.0)


def test_flag_off_pins_a_byte_identical_recipe_and_an_honest_receipt(monkeypatch):
    """9: off, the lane never binds or compiles; the recipe equals a run without cards."""
    baseline_job, _s, _c = _timed_setup(monkeypatch, flag=False)
    gb._run_generative_job(str(baseline_job.id))
    baseline = device_status(baseline_job, "guided_story").request.recipe

    job, _snapshot, _cloud = _timed_setup(
        monkeypatch, flag=False, strategy={"timed_cards": [_card()]}
    )
    gb._run_generative_job(str(job.id))
    recipe, clips = _overlay_clips(job)
    assert clips == [] and recipe_digest(recipe) == recipe_digest(baseline)
    variant = job.assembly_plan["variants"][0]
    assert variant[TIMED_CARD_RECEIPT_FIELD]["matcher"] == "disabled"
    assert variant[TIMED_CARD_RECEIPT_FIELD]["unplaced"][0]["reason"] == "lane_disabled"
    assert TIMED_CARDS_FIELD not in variant
    assert PHONE_VISUALS_FIELD not in job.assembly_plan
    # And a strategy with no cards at all leaves the variant exactly as before.
    assert TIMED_CARD_RECEIPT_FIELD not in baseline_job.assembly_plan["variants"][0]
