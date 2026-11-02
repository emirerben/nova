"""KRI-1142: a phone-rendered Talking (`subtitled`) edit can add a picture from the iPhone
editor's Visuals panel (including Generate media), and the picture survives later Saves."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routes.generative_jobs as gj
from app.services.client_protocol import clear_request_context
from app.services.device_render import device_status
from app.services.phone_editor_sources import EDITOR_SOURCES_FIELD
from app.services.phone_sources import PHONE_SOURCES_FIELD
from tests.routes.test_phone_subtitled_editor_commit import _sfx_payload, phone_job
from tests.routes.test_phone_voiceover_editor_media import _ASSETS, _photo_block
from tests.services import test_phone_visuals as pool

MEDIA_TRACK = "editor-media"


@pytest.fixture(autouse=True)
def _reset_protocol():
    clear_request_context()
    yield
    clear_request_context()


def _admit(variant: dict, *visuals, pinned: int = 1) -> None:
    """What the source-admission worker records for an imported visual; it indexes after the
    pinned sources (one speaker clip here)."""
    variant[EDITOR_SOURCES_FIELD] = {
        "imports": {},
        "sources": [
            {
                "media_id": visual.media_id,
                "lane": "asset",
                "kind": visual.kind,
                "gcs_path": visual.gcs_path,
                "generation": visual.generation,
                "duration_s": visual.duration_s,
                "status": "ready",
                "source_index": pinned + index,
                "visual_binding": visual.model_dump(mode="json"),
            }
            for index, visual in enumerate(visuals)
        ],
    }


def _talking_job(monkeypatch, *, media: bool = True, **gates):
    job = phone_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_editor_media_enabled", True)
    monkeypatch.setattr(gj.settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_subtitled_editor_media_enabled", media)
    for name, value in gates.items():
        monkeypatch.setattr(gj.settings, name, value)
    verified = set(gj.settings.phone_render_verified_features) | {"stillImages", "visualVideos"}
    monkeypatch.setattr(gj.settings, "phone_render_verified_features", sorted(verified))
    variant = job.assembly_plan["variants"][0]
    job.all_candidates = {
        "clip_paths": [b["proxy_path"] for b in job.assembly_plan[PHONE_SOURCES_FIELD]]
    }
    _admit(variant, pool.photo_visual())
    return job, variant


def save(job, **sections):
    variant = job.assembly_plan["variants"][0]
    return gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(base_generation=variant["render_generation_id"], **sections),
        user_id="owner",
        plan_item_id="item",
        visual_assets=_ASSETS,
    )


def _recipe(job):
    return device_status(job, "subtitled").request.recipe


def _media(recipe):
    return next((t for t in recipe.tracks if t.id == MEDIA_TRACK), None)


# --- capability -------------------------------------------------------------------------


def test_talking_edit_offers_picture_import_when_every_gate_passes(monkeypatch):
    job, variant = _talking_job(monkeypatch)

    caps = gj._editor_capabilities(job, variant)

    assert caps["phone_editor_media"]["source_registration"] is True
    assert caps["visual_blocks"] is True and caps["visual_blocks_reason"] is None
    assert caps["visual_block_kinds"] == ["media"]


@pytest.mark.parametrize(
    "gate",
    [
        {"media": False},
        {"phone_editor_media_enabled": False},
        {"visual_blocks_enabled": False},
        {"phone_subtitled_editor_lanes_enabled": False},
        {"phone_render_verified_features": ["captions"]},
    ],
    ids=["own-switch", "media-flag", "blocks-flag", "lanes-flag", "verified-features"],
)
def test_talking_picture_import_stays_closed_unless_every_gate_passes(monkeypatch, gate):
    job, variant = (
        _talking_job(monkeypatch, **{k: v for k, v in gate.items() if k != "media"})
        if ("media" not in gate)
        else _talking_job(monkeypatch, media=False)
    )

    caps = gj._editor_capabilities(job, variant)

    assert "phone_editor_media" not in caps
    assert caps["visual_blocks"] is False


def test_talking_read_carries_the_registration_revision(monkeypatch):
    job, _variant = _talking_job(monkeypatch)

    (variant,) = gj._variants_for_response(job)

    assert variant["editor_revision_number"] == 1


# --- save ------------------------------------------------------------------------------


def test_saving_a_picture_compiles_it_into_the_pinned_recipe(monkeypatch):
    job, _variant = _talking_job(monkeypatch)

    save(job, visual_blocks=[_photo_block()])

    media = _media(_recipe(job))
    assert media is not None and len(media.clips) == 1


def test_a_later_caption_or_sound_save_keeps_the_picture(monkeypatch):
    job, _variant = _talking_job(monkeypatch)
    save(job, visual_blocks=[_photo_block()])
    assert _media(_recipe(job)) is not None

    save(job, sound_effects=[_sfx_payload(at_s=4.0)])

    media = _media(_recipe(job))
    assert media is not None and len(media.clips) == 1


def test_removing_the_last_picture_removes_the_layer(monkeypatch):
    job, _variant = _talking_job(monkeypatch)
    save(job, visual_blocks=[_photo_block()])

    save(job, visual_blocks=[])

    assert _media(_recipe(job)) is None


def test_a_talking_edit_without_pictures_compiles_exactly_as_before(monkeypatch):
    job, _variant = _talking_job(monkeypatch)
    before = [t.id for t in _recipe(job).tracks]

    save(job, sound_effects=[_sfx_payload(at_s=4.0)])

    assert [t.id for t in _recipe(job).tracks] == before


def test_turning_the_rollout_off_refuses_a_save_instead_of_dropping_the_picture(monkeypatch):
    job, _variant = _talking_job(monkeypatch)
    save(job, visual_blocks=[_photo_block()])
    monkeypatch.setattr(gj.settings, "phone_subtitled_editor_media_enabled", False)

    with pytest.raises(HTTPException) as caught:
        save(job, sound_effects=[_sfx_payload(at_s=4.0)])

    assert caught.value.status_code == 422
    assert _media(_recipe(job)) is not None


def test_a_picture_save_is_refused_while_the_rollout_is_off(monkeypatch):
    job, _variant = _talking_job(monkeypatch, media=False)

    with pytest.raises(HTTPException) as caught:
        save(job, visual_blocks=[_photo_block()])

    assert caught.value.status_code in (404, 422)
