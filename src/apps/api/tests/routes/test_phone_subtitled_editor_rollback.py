"""Rolling back the phone Talking editor lanes restores the MP4 fallback.

A phone-editor Save persists `media_overlays`/`sound_effects` onto the
variant. iOS hydrates the Talking source clip only while the `overlays`/`sfx`
capabilities are editable, so a response that still carries those lanes after
`PHONE_SUBTITLED_EDITOR_LANES_ENABLED` (or both SFX/overlay kill switches) is
turned off validates as a recipe with an overlay track and no video: a black
canvas. With the lanes closed, the status response and the /timeline native
assets must drop both lanes -- the response only, never the persisted state --
so the editor sees empty tracks and falls back to the MP4 as before the lanes
existed.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest

from app.routes import generative_jobs as gj
from app.services.phone_subtitled_editor import PHONE_SUBTITLED_EDITOR_LANES_FIELD
from tests.routes.test_phone_subtitled_editor_commit import _overlay_payload, phone_job, save

_LANES = ("media_overlays", "sound_effects")


@pytest.fixture(autouse=True)
def _signing(monkeypatch):
    monkeypatch.setattr(gj, "signed_get_url", lambda path, ttl=None: f"https://signed/{path}")
    monkeypatch.setattr(
        gj.storage,
        "signed_download_url",
        lambda path, name, expiration_minutes=None: f"https://download/{path}",
    )


def _saved_phone_job(monkeypatch):
    """A ready phone Talking variant Saved once through the editor while the
    lanes were on: both lanes are persisted on the variant."""
    job = phone_job(monkeypatch)
    job.assembly_plan["variants"][0]["video_path"] = "jobs/phone/subtitled.mp4"
    save(job, media_overlays=[_overlay_payload(x_frac=0.9)])
    variant = job.assembly_plan["variants"][0]
    assert variant["media_overlays"] and variant["sound_effects"]
    return job


def _sign(path, ttl):
    return f"https://signed/{path}"


def test_lanes_on_keeps_saved_lanes_in_the_status_response_and_native_assets(monkeypatch):
    job = _saved_phone_job(monkeypatch)

    [variant] = gj._variants_for_response(job)
    assert variant["media_overlays"][0]["id"] == "card-1"
    assert variant["sound_effects"][0]["id"] == "sfx-1"
    kinds = sorted(a["kind"] for a in gj._native_editor_assets(job, "subtitled", sign_url=_sign))
    assert kinds == ["media_overlay", "sound_effect"]


def test_flag_off_drops_saved_lanes_from_the_response_only(monkeypatch):
    job = _saved_phone_job(monkeypatch)
    persisted = copy.deepcopy(job.assembly_plan)
    monkeypatch.setattr(gj.settings, "phone_subtitled_editor_lanes_enabled", False)

    [variant] = gj._variants_for_response(job)
    for lane in _LANES:
        assert lane not in variant
    assert variant["editor_capabilities"]["overlays"] is False
    assert variant["editor_capabilities"]["sfx"] is False
    assert gj._native_editor_assets(job, "subtitled", sign_url=_sign) == []
    # Persisted state is untouched: turning the flag back on restores the lanes.
    assert job.assembly_plan == persisted
    assert PHONE_SUBTITLED_EDITOR_LANES_FIELD in job.assembly_plan["variants"][0]


def test_both_lane_kill_switches_off_drop_saved_lanes(monkeypatch):
    job = _saved_phone_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "sound_effects_enabled", False)
    monkeypatch.setattr(gj.settings, "media_overlays_enabled", False)

    [variant] = gj._variants_for_response(job)
    for lane in _LANES:
        assert lane not in variant
    assert gj._native_editor_assets(job, "subtitled", sign_url=_sign) == []


def test_one_lane_kill_switch_keeps_the_lanes_the_editor_can_still_open(monkeypatch):
    """One open lane still makes iOS hydrate the source clip, so the saved
    lanes keep projecting (the closed one is read-only, not black)."""
    job = _saved_phone_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "sound_effects_enabled", False)

    [variant] = gj._variants_for_response(job)
    assert variant["editor_capabilities"]["overlays"] is True
    assert variant["media_overlays"][0]["id"] == "card-1"
    assert variant["sound_effects"][0]["id"] == "sfx-1"


def test_cloud_variant_lanes_are_never_dropped(monkeypatch):
    """The drop is scoped to phone Talking variants: a cloud caption variant's
    persisted lanes survive every phone flag and kill switch."""
    monkeypatch.setattr(gj.settings, "phone_subtitled_editor_lanes_enabled", False)
    monkeypatch.setattr(gj.settings, "sound_effects_enabled", False)
    monkeypatch.setattr(gj.settings, "media_overlays_enabled", False)
    card = {"id": "card-1", "kind": "image", "src_gcs_path": "users/owner/plan/item/pool/a.jpg"}
    effect = {"id": "sfx-1", "src_gcs_path": "sound-effects/pop/audio.m4a", "at_s": 1.0}
    job = SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={
            "variants": [
                {
                    "variant_id": "subtitled",
                    "resolved_archetype": "subtitled",
                    "video_path": "jobs/cloud/subtitled.mp4",
                    "media_overlays": [card],
                    "sound_effects": [effect],
                }
            ]
        },
    )

    [variant] = gj._variants_for_response(job)
    assert variant["media_overlays"][0]["id"] == "card-1"
    assert variant["sound_effects"][0]["id"] == "sfx-1"
    kinds = sorted(a["kind"] for a in gj._native_editor_assets(job, "subtitled", sign_url=_sign))
    assert kinds == ["media_overlay", "sound_effect"]
