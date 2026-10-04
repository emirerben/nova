"""KRI-306: the creator-selectable output shape (orientation + bars/crop)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.services import render_shape as rs


def _assignment(width: int, height: int, rotation: int = 0) -> dict:
    return {
        "gcs_path": f"users/u/{width}x{height}.mp4",
        "upload_contract": {
            "purpose": "analysis_proxy",
            "proxy": {
                "original": {
                    "width": width,
                    "height": height,
                    "orientation_degrees": rotation,
                }
            },
        },
    }


def _item(assignments=None, fit: str = "fit") -> SimpleNamespace:
    return SimpleNamespace(landscape_fit=fit, clip_assignments=assignments or [])


def _offer(edit_format: str = "montage", item=None, **kwargs) -> rs.RenderShapeOffer:
    kwargs.setdefault("landscape_enabled", True)
    return rs.creation_offer(item or _item(), CreativeStrategy(edit_format=edit_format), **kwargs)


# ── format table ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("edit_format", ["montage", "day_vlog", "single_hero"])
def test_montage_family_offers_landscape_and_bars(edit_format):
    offer = _offer(edit_format)
    assert offer.orientations == ("portrait", "landscape")
    assert offer.fit_choices == ("fit", "fill")


def test_subtitled_offers_bars_only():
    offer = _offer("subtitled")
    assert offer.orientations == ("portrait",)
    assert offer.fit_choices == ("fit", "fill")
    assert offer.has_choice is True


@pytest.mark.parametrize("edit_format", ["narrated", "narrated_planned", "narrated_ready"])
def test_narrated_family_is_closed_with_a_reason(edit_format):
    offer = _offer(edit_format)
    assert offer.has_choice is False
    assert offer.reason == rs.REASON_FORMAT_CLOSED
    assert offer.projection() is None


def test_flag_off_removes_landscape():
    offer = _offer("montage", landscape_enabled=False)
    assert offer.orientations == ("portrait",)
    assert offer.reason == rs.REASON_DISABLED
    assert offer.fit_choices == ("fit", "fill")


def test_mixed_media_timing_pins_portrait():
    strategy = CreativeStrategy(
        edit_format="montage",
        mixed_media_timing={"mode": "quick_photo_long_video", "photo_s": 1.0},
    )
    if strategy.mixed_media_timing is None:  # schema shape guard
        pytest.skip("mixed_media_timing did not validate")
    offer = rs.creation_offer(_item(), strategy, landscape_enabled=True)
    assert offer.orientations == ("portrait",)
    assert offer.reason == rs.REASON_MIXED_MEDIA


def test_cloud_account_is_not_offered_landscape():
    offer = _offer("montage", device_render=False)
    assert offer.orientations == ("portrait",)
    assert offer.reason == rs.REASON_NOT_ON_DEVICE


def test_env_flag_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv("LANDSCAPE_OUTPUT_ENABLED", "true")
    assert rs.creation_offer(_item(), CreativeStrategy()).orientations == (
        "portrait",
        "landscape",
    )
    monkeypatch.setenv("LANDSCAPE_OUTPUT_ENABLED", "false")
    assert rs.creation_offer(_item(), CreativeStrategy()).orientations == ("portrait",)


# ── defaults ─────────────────────────────────────────────────────────────────


def test_default_is_portrait_without_signals():
    offer = _offer()
    assert (offer.default_orientation, offer.default_fit) == ("portrait", "fit")


def test_fit_default_follows_the_item_preference():
    assert _offer(item=_item(fit="fill")).default_fit == "fill"


def test_vote_prefers_the_display_aspect_majority():
    landscape = [_assignment(1920, 1080), _assignment(1920, 1080), _assignment(1080, 1920)]
    assert _offer(item=_item(landscape)).default_orientation == "landscape"
    portrait = [_assignment(1080, 1920), _assignment(1080, 1920), _assignment(1920, 1080)]
    assert _offer(item=_item(portrait)).default_orientation == "portrait"


def test_vote_applies_rotation_and_ignores_near_square():
    # A 1080x1920 file flagged 90 degrees DISPLAYS as landscape.
    assert rs.vote_orientation([_assignment(1080, 1920, 90)]) == "landscape"
    assert rs.vote_orientation([_assignment(1000, 1000), _assignment(1040, 1000)]) is None
    assert rs.vote_orientation([{"gcs_path": "cloud/clip.mp4"}]) is None


def test_previous_ready_variant_beats_the_vote():
    landscape_clips = _item([_assignment(1920, 1080)] * 3)
    offer = _offer(item=landscape_clips, previous_orientation="portrait")
    assert offer.default_orientation == "portrait"
    offer = _offer(item=_item(), previous_orientation="landscape")
    assert offer.default_orientation == "landscape"


def test_previous_orientation_is_ignored_when_landscape_is_not_offered():
    offer = _offer(previous_orientation="landscape", landscape_enabled=False)
    assert offer.default_orientation == "portrait"


def test_previous_ready_orientation_reads_the_first_ready_variant():
    job = SimpleNamespace(
        assembly_plan={
            "variants": [
                {"render_status": "failed", "orientation": "landscape"},
                {"render_status": "ready", "orientation": "landscape"},
            ]
        }
    )
    assert rs.previous_ready_orientation(job) == "landscape"
    assert rs.previous_ready_orientation(SimpleNamespace(assembly_plan={"variants": []})) is None
    assert rs.previous_ready_orientation(None) is None
    legacy = SimpleNamespace(assembly_plan={"variants": [{"render_status": "ready"}]})
    assert rs.previous_ready_orientation(legacy) == "portrait"


def test_projection_shape():
    assert _offer().projection() == {
        "orientations": ["portrait", "landscape"],
        "fit_choices": ["fit", "fill"],
        "default": {"output_orientation": "portrait", "landscape_fit": "fit"},
    }


# ── choice validation ────────────────────────────────────────────────────────


def test_no_choice_resolves_to_none():
    assert rs.resolve_choice(_offer(), None, None) is None
    # ...even where there is nothing to choose: absence never errors.
    assert rs.resolve_choice(_offer("narrated"), None, None) is None


def test_partial_choice_fills_from_defaults():
    offer = _offer(item=_item(fit="fit"))
    assert rs.resolve_choice(offer, None, "fill") == {
        "output_orientation": "portrait",
        "landscape_fit": "fill",
    }
    assert rs.resolve_choice(offer, "portrait", None) == {
        "output_orientation": "portrait",
        "landscape_fit": "fit",
    }


def test_landscape_choice_never_carries_bars():
    assert rs.resolve_choice(_offer(), "landscape", "fit") == {
        "output_orientation": "landscape",
        "landscape_fit": "fill",
    }


def test_unavailable_choice_is_unsupported():
    with pytest.raises(rs.RenderShapeError) as exc:
        rs.resolve_choice(_offer("subtitled"), "landscape", None)
    assert exc.value.code == "render_shape_unsupported"
    with pytest.raises(rs.RenderShapeError) as exc:
        rs.resolve_choice(_offer(landscape_enabled=False), "landscape", None)
    assert exc.value.code == "render_shape_unsupported"


def test_choice_on_a_closed_format_is_not_applicable():
    with pytest.raises(rs.RenderShapeError) as exc:
        rs.resolve_choice(_offer("narrated"), None, "fill")
    assert exc.value.code == "render_shape_not_applicable"


def test_shape_from_all_candidates_is_strict():
    ok = {"creator_render_shape": {"output_orientation": "landscape", "landscape_fit": "fill"}}
    assert rs.shape_from_all_candidates(ok) == {
        "output_orientation": "landscape",
        "landscape_fit": "fill",
    }
    assert rs.shape_from_all_candidates({}) is None
    assert (
        rs.shape_from_all_candidates({"creator_render_shape": {"output_orientation": "x"}}) is None
    )


# ── device editor offer ──────────────────────────────────────────────────────


def _device_variant(archetype: str, **extra) -> dict:
    return {"resolved_archetype": archetype, "render_destination": "device", **extra}


def test_editor_offer_subtitled_needs_its_lane_rollout():
    open_offer = rs.device_editor_offer(_device_variant("subtitled"), {}, subtitled_lanes=True)
    assert open_offer.landscape_fit.editable is True
    assert open_offer.orientation is None  # base map already closes it
    closed = rs.device_editor_offer(_device_variant("subtitled"), {}, subtitled_lanes=False)
    assert closed.landscape_fit.editable is False


def test_editor_offer_voiceover_montage_fit_only():
    offer = rs.device_editor_offer(_device_variant("voiceover"), {}, voiceover_lanes=True)
    assert offer.landscape_fit.editable is True
    # Save cannot re-canvas a voiceover montage: advertise only what Save accepts.
    assert offer.orientation is not None and offer.orientation.editable is False


@pytest.mark.parametrize("archetype", ["narrated", "speech_montage", "talking_head"])
def test_editor_offer_closes_the_rest(archetype):
    offer = rs.device_editor_offer(_device_variant(archetype), {}, voiceover_lanes=True)
    assert offer.landscape_fit.editable is False
    assert offer.orientation is not None and offer.orientation.editable is False


def test_editor_offer_landscape_output_has_no_bars():
    offer = rs.device_editor_offer(
        _device_variant("voiceover", orientation="landscape"), {}, voiceover_lanes=True
    )
    assert offer.landscape_fit.editable is False
    assert offer.landscape_fit.reason == "landscape_output"


def test_editor_offer_guided_needs_a_revision():
    variant = _device_variant("guided_story")
    assert rs.device_editor_offer(variant, {}, guided_revision=True).landscape_fit.editable
    gated = rs.device_editor_offer(variant, {}, guided_revision=False)
    assert gated.landscape_fit.editable is False
    assert gated.orientation is None  # the guided orientation gate is its own


def test_editor_offer_value_prefers_the_persisted_variant_value():
    variant = _device_variant("voiceover", landscape_fit="fill")
    offer = rs.device_editor_offer(
        variant, {}, voiceover_lanes=True, all_candidates={"landscape_fit": "fit"}
    )
    assert offer.landscape_fit.value == "fill"
    unset = rs.device_editor_offer(
        _device_variant("voiceover"),
        {},
        voiceover_lanes=True,
        all_candidates={"landscape_fit": "fit"},
    )
    assert unset.landscape_fit.value == "fit"


def test_cloud_axis_is_always_closed():
    axis = rs.cloud_fit_axis({"landscape_fit": "fit"})
    assert axis == {"editable": False, "value": "fit", "reason": "cloud_unsupported"}
    assert rs.cloud_fit_axis({})["value"] == "fill"


# ── Job carries the explicit choice only ─────────────────────────────────────


def _build(**extra):
    import uuid

    from app.services.generative_jobs import build_generative_job

    return build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        mode="content_plan",
        content_plan_item_id=uuid.uuid4(),
        content_plan_ownership_epoch=0,
        **extra,
    )


def test_job_carries_the_shape_only_when_the_creator_chose():
    plain = _build()
    assert "creator_render_shape" not in plain.all_candidates
    chosen = _build(
        creator_render_shape={"output_orientation": "landscape", "landscape_fit": "fill"}
    )
    assert chosen.all_candidates["creator_render_shape"] == {
        "output_orientation": "landscape",
        "landscape_fit": "fill",
    }
    # Everything else about the job is unchanged by the choice.
    rest = {k: v for k, v in chosen.all_candidates.items() if k != "creator_render_shape"}
    assert rest == plain.all_candidates


def test_job_drops_a_malformed_shape():
    job = _build(creator_render_shape={"output_orientation": "square", "landscape_fit": "fit"})
    assert "creator_render_shape" not in job.all_candidates
