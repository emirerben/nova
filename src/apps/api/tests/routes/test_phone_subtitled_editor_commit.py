import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.kria.device_render import make_device_request
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.routes import generative_jobs as gj
from app.services.device_render import device_status, pin_device_request
from app.services.phone_editor import prepare_phone_editor_commit
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD
from app.services.phone_subtitled_editor import PHONE_SUBTITLED_EDITOR_LANES_FIELD
from tests.pipeline.test_phone_subtitled_plan import (
    _CUES,
    PHOTO_PATH,
    _binding,
    _overlay_card,
    _photo_visual,
    _resolved_sfx,
)

_VERIFIED_FEATURES = [
    "basicComposition",
    "local1080Export",
    "positionedText",
    "animatedText",
    "audioMix",
    "soundEffects",
    "visualBlocks",
    "alphaOverlay",
    "stillImages",
    "visualVideos",
]


def _enable_subtitled_editor(monkeypatch, *, editor_flag: bool = True) -> None:
    # `phone_rendering_enabled` + `sound_effects_enabled`/`media_overlays_enabled`
    # are prerequisites this feature doesn't own -- always on here so a test
    # that only flips `editor_flag` off exercises THAT flag specifically,
    # not an unrelated one.
    monkeypatch.setattr(gj.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(gj.settings, "sound_effects_enabled", True)
    monkeypatch.setattr(gj.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_subtitled_editor_lanes_enabled", editor_flag)
    monkeypatch.setattr(gj.settings, "phone_subtitled_media_lanes_enabled", editor_flag)
    monkeypatch.setattr(gj.settings, "phone_render_verified_features", list(_VERIFIED_FEATURES))


def phone_job(monkeypatch, *, enable=True):
    """A phone-rendered `subtitled` variant with one pinned overlay card and
    one pinned sound effect, mirroring `tests.routes.test_phone_editor_commit
    .phone_job`'s pattern for the guided_story archetype."""
    _enable_subtitled_editor(monkeypatch, editor_flag=enable)
    bindings = (_binding(duration_s=10.0),)
    photo = _photo_visual()
    card = _overlay_card(id="card-1")
    sfx = _resolved_sfx()
    lanes = PhoneSubtitledLanes(overlays=[card], sound_effects=[sfx])
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=_CUES, visuals=(photo,), lanes=lanes
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        assembly_plan={
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            PHONE_VISUALS_FIELD: [photo.model_dump(mode="json")],
            "variants": [
                {
                    "variant_id": "subtitled",
                    "resolved_archetype": "subtitled",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": recipe.duration,
                    "caption_cues": _CUES,
                    "voiceover_caption_style": "sentence",
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe),
        base_generation="first",
    )
    return job


def _sfx_payload(*, effect_id="sfx-1", catalog_id="pop", at_s=1.0, gain=1.0, **extra):
    return {
        "id": effect_id,
        "sound_effect_id": catalog_id,
        "src_gcs_path": f"sound-effects/{catalog_id}/{catalog_id}.m4a",
        "at_s": at_s,
        "gain": gain,
        "duration_s": 1.0,
        **extra,
    }


def _overlay_payload(*, card_id="card-1", src_gcs_path=PHOTO_PATH, x_frac=0.5, **extra):
    return {
        "id": card_id,
        "kind": "image",
        "src_gcs_path": src_gcs_path,
        "display_mode": "pip",
        "x_frac": x_frac,
        "y_frac": 0.4,
        "scale": 0.35,
        "start_s": 1.0,
        "end_s": 3.0,
        **extra,
    }


def save(job, **section_overrides):
    return gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(base_generation="first", **section_overrides),
        user_id="owner",
        plan_item_id="item",
    )


# --- flag gating ----------------------------------------------------------------


def test_flag_off_still_422s_unsupported_phone_edit(monkeypatch):
    job = phone_job(monkeypatch, enable=False)
    with pytest.raises(HTTPException) as error:
        save(job, sound_effects=[_sfx_payload(at_s=4.0)])
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"


# --- happy path -------------------------------------------------------------------


def test_flag_on_save_pins_revision_two_with_no_cloud_task(monkeypatch):
    job = phone_job(monkeypatch)
    old = device_status(job, "subtitled").request
    prep = save(job, sound_effects=[_sfx_payload(at_s=4.0)])

    assert prep["render_destination"] == "device"
    assert prep["render_task_id"] is None
    new = device_status(job, "subtitled").request
    assert new.identity.recipe_revision == old.identity.recipe_revision + 1

    variant = job.assembly_plan["variants"][0]
    assert variant["render_status"] == "awaiting_device"
    assert PHONE_SUBTITLED_EDITOR_LANES_FIELD in variant


def test_media_overlay_move_round_trips_into_the_recipe_overlay_clip(monkeypatch):
    job = phone_job(monkeypatch)
    moved = _overlay_payload(x_frac=0.9)
    save(job, media_overlays=[moved])

    request = device_status(job, "subtitled").request
    overlay_track = next(t for t in request.recipe.tracks if t.id == "subtitled-overlays")
    assert len(overlay_track.clips) == 1
    assert overlay_track.clips[0].visual_placement.x_fraction == pytest.approx(0.9)

    variant = job.assembly_plan["variants"][0]
    assert variant["media_overlays"][0]["x_frac"] == pytest.approx(0.9)


def test_sound_delete_removes_the_sfx_clip(monkeypatch):
    job = phone_job(monkeypatch)
    save(job, sound_effects=[])

    request = device_status(job, "subtitled").request
    assert not any(t.id == "sfx" for t in request.recipe.tracks)
    variant = job.assembly_plan["variants"][0]
    assert variant["sound_effects"] is None


def test_added_catalog_effect_lands_on_the_sfx_track(monkeypatch):
    job = phone_job(monkeypatch)
    new_effect = _sfx_payload(effect_id="sfx-new", catalog_id="ding", at_s=6.0)

    from app.services import phone_subtitled_editor as pse

    def fake_inspect(path, *, asset_id, catalog, catalog_id):
        from tests.pipeline.test_phone_subtitled_plan import _sfx_asset

        return _sfx_asset(catalog_id, generation="42")

    monkeypatch.setattr(pse, "inspect_library_asset", fake_inspect)

    save(job, sound_effects=[_sfx_payload(at_s=1.0), new_effect])

    request = device_status(job, "subtitled").request
    sfx_track = next(t for t in request.recipe.tracks if t.id == "sfx")
    assert len(sfx_track.clips) == 2
    assert {c.id for c in sfx_track.clips} == {"sfx-sfx-1", "sfx-sfx-new"}


def test_untouched_section_carries_forward_when_only_the_other_is_committed(monkeypatch):
    job = phone_job(monkeypatch)
    save(job, sound_effects=[_sfx_payload(at_s=4.0)])

    request = device_status(job, "subtitled").request
    overlay_track = next(t for t in request.recipe.tracks if t.id == "subtitled-overlays")
    assert len(overlay_track.clips) == 1  # the pinned card carried forward untouched


# --- named-reason 422s -------------------------------------------------------------


def test_text_elements_section_is_a_named_422_reason(monkeypatch):
    """Mirrors `test_phone_editor_commit.test_unsupported_phone_edit_names_
    its_reason` -- a hand-built `prep` dict marking `text_elements` active
    bypasses `_prepare_editor_commit`'s own (differently-shaped) generic
    caption-archetype rejection, isolating just this branch's own
    unsupported-section check and its exact reason text."""
    job = phone_job(monkeypatch)
    with pytest.raises(HTTPException) as error:
        prepare_phone_editor_commit(
            job,
            "subtitled",
            prepare=lambda staged: {
                "has_render_section": True,
                "sections": {"text_elements": True},
                "sfx_override": None,
                "media_overlays_override": None,
                "caption_cues_override": None,
                "generation": "second",
            },
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"
    assert "text_elements" in error.value.detail["reason"]


def test_unpinned_overlay_path_is_a_named_422_reason(monkeypatch):
    job = phone_job(monkeypatch)
    bad = _overlay_payload(card_id="card-bad", src_gcs_path="users/owner/plan/item/pool/nope.jpg")
    with pytest.raises(HTTPException) as error:
        save(job, media_overlays=[bad])
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"
    assert "isn't a photo Kria pinned" in error.value.detail["reason"]


def test_failed_compile_leaves_the_baseline_untouched(monkeypatch):
    job = phone_job(monkeypatch)
    baseline = device_status(job, "subtitled").request
    baseline_variant = dict(job.assembly_plan["variants"][0])

    with pytest.raises(HTTPException):
        save(
            job,
            media_overlays=[
                _overlay_payload(
                    card_id="card-bad", src_gcs_path="users/owner/plan/item/pool/nope.jpg"
                )
            ],
        )

    assert device_status(job, "subtitled").request == baseline
    assert job.assembly_plan["variants"][0] == baseline_variant


def test_unsupported_phone_edit_names_its_reason_via_synthetic_prep(monkeypatch):
    """Mirrors `test_phone_editor_commit.test_unsupported_phone_edit_names_
    its_reason` -- construct a `prep` dict by hand rather than routing
    through the full `_prepare_editor_commit`."""
    job = phone_job(monkeypatch, enable=False)
    with pytest.raises(HTTPException) as error:
        prepare_phone_editor_commit(
            job,
            "subtitled",
            prepare=lambda staged: {
                "has_render_section": True,
                "sections": {"sound_effects": True},
                "sfx_override": [_sfx_payload()],
                "media_overlays_override": None,
                "caption_cues_override": None,
                "generation": "second",
            },
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"
    assert error.value.detail["reason"] == "ValueError: phone Talking edits aren't editable yet"
