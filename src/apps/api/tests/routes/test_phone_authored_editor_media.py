"""KRI-1142: a phone edit built from scratch (authored) or still empty can add a picture
from Visuals > Generate media, not only a narrated or voiceover edit."""

from __future__ import annotations

import pytest

import app.routes.generative_jobs as gj
from tests.routes.test_phone_voiceover_editor_media import (
    _enable,
    _media_job,
    _photo_block,
    _reset_protocol,  # noqa: F401 - autouse fixture
    save,
)

AUTHORED = {"editor_timeline_mode": "authored"}
EMPTY = {"editor_state": "empty"}


def _caps(monkeypatch, state, **settings):
    _enable(monkeypatch)
    for name, value in settings.items():
        monkeypatch.setattr(gj.settings, name, value)
    job, _vid = _media_job()
    variant = {**job.assembly_plan["variants"][0], **state}
    job.assembly_plan["variants"][0] = variant
    return job, variant, gj._editor_capabilities(job, variant)


@pytest.mark.parametrize("state", [AUTHORED, EMPTY], ids=["authored", "empty"])
def test_authored_and_empty_edits_offer_the_picture_lane(monkeypatch, state):
    _job, _variant, caps = _caps(monkeypatch, state)

    assert caps["phone_editor_media"]["source_registration"] is True
    assert caps["visual_blocks"] is True
    assert caps["visual_blocks_reason"] is None


@pytest.mark.parametrize("state", [AUTHORED, EMPTY], ids=["authored", "empty"])
def test_the_picture_lane_stays_closed_while_visual_blocks_are_switched_off(monkeypatch, state):
    _job, _variant, caps = _caps(monkeypatch, state, visual_blocks_enabled=False)

    assert caps["visual_blocks"] is False


@pytest.mark.parametrize("state", [AUTHORED, EMPTY], ids=["authored", "empty"])
def test_a_missing_verified_feature_keeps_the_lane_closed(monkeypatch, state):
    _job, _variant, caps = _caps(monkeypatch, state, phone_render_verified_features=["captions"])

    assert caps["visual_blocks"] is False


def test_the_save_check_no_longer_refuses_a_picture_for_having_no_clean_base(monkeypatch):
    """The generic visual-block validator used to reject authored edits ("Visual blocks require
    a non-lyrics variant with a clean base"). A Save that gets past it fails, if at all, later
    in the authored compile, which has its own coverage."""
    from fastapi import HTTPException

    _enable(monkeypatch)
    job, vid = _media_job()
    job.assembly_plan["variants"][0].update(AUTHORED)

    try:
        save(job, vid, editor_state_version=2, visual_blocks=[_photo_block()])
    except HTTPException as exc:
        assert "clean base" not in str(exc.detail)


def test_the_save_check_still_refuses_a_picture_on_a_variant_that_cannot_take_one(monkeypatch):
    from fastapi import HTTPException

    _enable(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_editor_media_enabled", False)
    job, vid = _media_job()
    job.assembly_plan["variants"][0].update(AUTHORED)

    with pytest.raises(HTTPException) as caught:
        save(job, vid, editor_state_version=2, visual_blocks=[_photo_block()])
    assert caught.value.status_code in (404, 422)
