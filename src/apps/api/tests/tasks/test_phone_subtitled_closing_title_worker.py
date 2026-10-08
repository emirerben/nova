"""KRI-514: `_run_phone_subtitled_job` draws the creator's confirmed closing text.

"End on the toast photo with a 'MY PICK' badge" used to stop the whole phone
Talking edit with "This kind of edit can't show your own text on each shot or
at the end yet". The worker now reads ``creator_strategy.closing_title``,
builds a tag row that appears with the closing photo and sits on it (or holds
the last 3 s near the top without one), compiles it under the captions to the
very end of the recipe, and persists it as an editable text row.
``PHONE_SUBTITLED_CLOSING_TITLE_ENABLED=false`` is byte-identical to the job
without closing text.
"""

from __future__ import annotations

import copy

import pytest

import app.pipeline.phone_subtitled_title as title_mod
import app.services.phone_overlay_grounding as phone_overlay_grounding_mod
import app.services.phone_reaction_grounding as phone_reaction_grounding_mod
import app.services.phone_visuals as phone_visuals_mod
from app.pipeline.phone_subtitled_lanes import (
    CAPTION_BAND_TOP_FRAC,
    PHONE_SUBTITLED_LANES_FIELD,
    SubtitledOverlayCard,
)
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services.creator_render_contract import (
    CreatorRenderContract,
    TextRequirement,
    verify_phone_recipe,
)
from app.services.device_render import device_status
from app.services.phone_reaction_grounding import GroundedReactionBeats
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _basic_beat_receipt,
    _beat_grounding_mock,
    _beats_features,
    _closing_dict,
    _enable_beats,
    _ending_clip_dict,
    _grounding_mock,
    _lane_request,
    _lanes_features,
    _make_fake_bind,
    _setup_subtitled,
)

_PICK = "MY PICK"
_PHOTO_ASPECT = 1080 / 1920
# The toast photo as grounding placed it: off the face, top right.
_PHOTO = {"x_frac": 0.74, "y_frac": 0.3, "scale": 0.36}


def _faces(monkeypatch, regions=()):
    calls: list = []

    def sample(path, anchors, **kwargs):
        calls.append((path, list(anchors)))
        return list(regions), {"attempted": len(anchors), "decoded": len(anchors)}

    monkeypatch.setattr(title_mod, "sample_face_regions", sample)
    return calls


def _closing_layers(job):
    recipe = device_status(job, "subtitled").request.recipe
    return [
        layer
        for layer in recipe.text_layers
        if layer.id.startswith("text-") and "".join(run.text for run in layer.runs) == _PICK
    ]


def _closes_with_pick(job) -> list:
    """The device-pin check the approved contract runs on this recipe."""
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role="closing", text=_PICK),)
    )
    return verify_phone_recipe(contract, device_status(job, "subtitled").request.recipe)


def _toast_photo_job(monkeypatch, *, strategy_extra: dict | None = None):
    """The Greek-yogurt chat: a toast photo held from 7 s to the end of a 10 s
    take, with "MY PICK" on it."""
    job, snapshot, session, binding = _setup_subtitled(monkeypatch)
    _enable_beats(monkeypatch, extra_features=("authoredText",))
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))
    photo = SubtitledOverlayCard(
        id="closing-photo",
        media_id="toast",
        gcs_path="users/u1/plan/item1/pool/toast.jpg",
        generation="1",
        start_s=7.0,
        end_s=10.0,
        fade=True,
        **_PHOTO,
    )
    grounding = _beat_grounding_mock(
        [photo],
        [],
        _basic_beat_receipt(
            closing={"status": "placed", "from_s": 7.0, "visual_label": "toast", "badge": "none"}
        ),
    )
    grounding.return_value = GroundedReactionBeats(
        cards=[photo],
        receipt=grounding.return_value.receipt,
        closing_card_id="closing-photo",
        closing_card_aspect=_PHOTO_ASPECT,
    )
    monkeypatch.setattr(phone_reaction_grounding_mod, "ground_phone_reaction_beats", grounding)
    monkeypatch.setattr(
        phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", _grounding_mock([])
    )
    job.all_candidates["creator_strategy"] = {
        "closing_media": _closing_dict("toast"),
        "closing_title": _PICK,
        **(strategy_extra or {}),
    }
    return job, snapshot, session, binding


def test_my_pick_sits_on_the_toast_photo_from_its_entrance_to_the_end(monkeypatch):
    calls = _faces(monkeypatch)
    job, _snapshot, _session, _binding = _toast_photo_job(monkeypatch)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    [row] = variant["text_elements"]
    assert (row["id"], row["text"]) == ("closing-title", _PICK)
    assert (row["start_s"], row["end_s"]) == (7.0, 10.0)
    assert row["background_color"] == "#C5F82A"
    assert row["source_params"] == {"source": "closing_title"}
    assert variant["text_elements_user_edited"] is True
    assert variant["text_elements_materialized_from"] == "closing_title"
    # On the photo's lower edge: inside its box, below its centre, and clear of
    # the captions. The photo is already off the face, so no face sampling.
    photo_height = _PHOTO["scale"] * 1080 / 1920 / _PHOTO_ASPECT
    assert row["x_frac"] == _PHOTO["x_frac"]
    assert _PHOTO["y_frac"] < row["y_frac"] < _PHOTO["y_frac"] + photo_height / 2
    assert row["y_frac"] < CAPTION_BAND_TOP_FRAC
    assert variant["closing_title_placement"]["status"] == "on_closing_photo"
    assert calls == []
    [layer] = _closing_layers(job)
    assert layer.background is not None
    assert (layer.start, layer.end) == (7.0, 10.0)
    assert _closes_with_pick(job)


def test_with_a_hook_title_both_rows_render_in_order(monkeypatch):
    _faces(monkeypatch)
    job, _snapshot, _session, _binding = _toast_photo_job(
        monkeypatch,
        strategy_extra={"opening_title": "Breakfast ranking", "opening_title_duration_s": 2.0},
    )

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    assert [row["id"] for row in variant["text_elements"]] == ["opening-title", "closing-title"]
    assert variant["text_elements_materialized_from"] == "opening_title"
    assert variant["opening_title_placement"]["status"] == "preset"
    assert variant["closing_title_placement"]["status"] == "on_closing_photo"
    assert _closes_with_pick(job)


def test_without_a_closing_photo_it_holds_the_last_three_seconds_off_the_face(monkeypatch):
    calls = _faces(
        monkeypatch, [ProtectedRegion(0.0, 10.0, NormalizedBox(0.25, 0.04, 0.75, 0.42), "face")]
    )
    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(
        gb.settings, "phone_render_verified_features", _lanes_features("authoredText")
    )
    job.all_candidates["creator_strategy"] = {"closing_title": f"  {_PICK} "}

    gb._run_generative_job(str(job.id))

    [row] = job.assembly_plan["variants"][0]["text_elements"]
    assert (row["text"], row["start_s"], row["end_s"]) == (_PICK, 7.0, 10.0)
    assert row["y_frac"] > 0.42
    assert job.assembly_plan["variants"][0]["closing_title_placement"]["status"] == "moved"
    # Sampled over the closing window on the speaker's own proxy.
    [(path, anchors)] = calls
    assert path == "/tmp/c0.mp4"
    assert 7.0 < anchors[0] < anchors[-1] < 10.0
    assert _closes_with_pick(job)


def test_after_an_ending_clip_the_text_runs_to_the_very_end(monkeypatch):
    """A hand-authored lane request can append an ending clip after the
    speaker; the contract wants closing text on screen at the recipe's end."""
    _faces(monkeypatch)
    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        _lanes_features("stillImages", "visualVideos", "visualBlocks", "authoredText"),
    )
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))
    job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = _lane_request(
        ending_clip=_ending_clip_dict("video1")
    )
    job.all_candidates["creator_strategy"] = {"closing_title": _PICK}

    gb._run_generative_job(str(job.id))

    recipe = device_status(job, "subtitled").request.recipe
    assert recipe.duration == pytest.approx(15.0)
    [row] = job.assembly_plan["variants"][0]["text_elements"]
    assert (row["start_s"], row["end_s"]) == (7.0, pytest.approx(15.0))
    assert _closes_with_pick(job)


def test_switched_off_the_job_is_exactly_the_one_without_closing_text(monkeypatch):
    _faces(monkeypatch)
    plain, _s, _sess, _b = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(
        gb.settings, "phone_render_verified_features", _lanes_features("authoredText")
    )
    gb._run_generative_job(str(plain.id))
    expected_variant = copy.deepcopy(plain.assembly_plan["variants"][0])
    expected_recipe = device_status(plain, "subtitled").request.recipe

    monkeypatch.setattr(gb.settings, "phone_subtitled_closing_title_enabled", False)
    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(
        gb.settings, "phone_render_verified_features", _lanes_features("authoredText")
    )
    job.all_candidates["creator_strategy"] = {"closing_title": _PICK}
    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    assert "text_elements" not in variant
    assert variant == {**expected_variant, "render_generation_id": variant["render_generation_id"]}
    assert device_status(job, "subtitled").request.recipe == expected_recipe


def test_without_the_authored_text_feature_nothing_is_drawn(monkeypatch):
    """The tag's background needs `authoredText` on the device; the planner
    refuses up front in that case, and the worker never draws a half-styled
    row."""
    _faces(monkeypatch)
    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", _beats_features())
    job.all_candidates["creator_strategy"] = {"closing_title": _PICK}

    gb._run_generative_job(str(job.id))

    assert "text_elements" not in job.assembly_plan["variants"][0]
