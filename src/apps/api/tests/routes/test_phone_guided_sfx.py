"""KRI-608: sound effects on a phone-rendered guided / montage story.

Drives the real Save path for a device-pinned ``guided_story`` variant
(``prepare_editor_commit`` -> ``prepare_phone_editor_commit`` ->
``compile_phone_guided_plan``) and the two surfaces that decide whether the creator can
ask for an effect at all: the clamped editor capability map and the chat copilot's
allowed op families. All three follow ``phone_guided_sfx_supported()``.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routes import generative_jobs as gj
from app.services.device_render import device_status
from app.services.kria_editor_ops import build_editor_snapshot
from app.services.phone_subtitled_editor import PHONE_SUBTITLED_EDITOR_LANES_FIELD  # noqa: F401
from tests.pipeline.test_phone_subtitled_plan import _sfx_asset
from tests.routes.test_phone_guided_timeline_commit import _guided_phone_job

_SFX_VERIFIED = ["soundEffects", "audioMix"]


def _enable_guided_sfx(monkeypatch, *, enabled: bool = True) -> None:
    monkeypatch.setattr(gj.settings, "phone_guided_sfx_enabled", enabled)
    monkeypatch.setattr(gj.settings, "sound_effects_enabled", True)
    verified = list(gj.settings.phone_render_verified_features)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        verified + [f for f in _SFX_VERIFIED if f not in verified],
    )


def _stub_library_probe(monkeypatch) -> None:
    from app.services import phone_subtitled_editor as pse

    def fake_inspect(path, *, asset_id, catalog, catalog_id):
        return _sfx_asset(catalog_id, generation="42")

    monkeypatch.setattr(pse, "inspect_library_asset", fake_inspect)


def _sfx_row(effect_id="sfx-1", catalog_id="pop", *, at_s=1.0, gain=0.9, **extra):
    return {
        "id": effect_id,
        "sound_effect_id": catalog_id,
        "src_gcs_path": f"sound-effects/{catalog_id}/{catalog_id}.m4a",
        "at_s": at_s,
        "gain": gain,
        "duration_s": 1.0,
        **extra,
    }


def _save(job, revision, **sections):
    payload = gj.EditorCommitRequest(
        base_generation="first",
        guided_revision_number=revision["revision_number"],
        **sections,
    )
    return gj.prepare_editor_commit(job, "guided_story", payload, user_id="owner")


def test_flag_on_save_compiles_the_effects_into_an_sfx_track(monkeypatch):
    job, variant, revision = _guided_phone_job(monkeypatch)
    _enable_guided_sfx(monkeypatch)
    _stub_library_probe(monkeypatch)

    prep = _save(job, revision, sound_effects=[_sfx_row(at_s=2.0), _sfx_row("sfx-2", at_s=9.0)])

    assert prep["render_destination"] == "device"
    request = device_status(job, "guided_story").request
    assert request.identity.recipe_revision == 2
    sfx = next(track for track in request.recipe.tracks if track.id == "sfx")
    assert sfx.kind == "audio"
    assert [(clip.id, clip.timeline_start, clip.volume) for clip in sfx.clips] == [
        ("sfx-sfx-1", 2.0, 0.9),
        ("sfx-sfx-2", 9.0, 0.9),
    ]
    assert {"soundEffects", "audioMix"} <= request.recipe.required_capabilities
    # The revision the editor reads back carries the same effects.
    saved = job.assembly_plan["variants"][0]["guided_edit_revision"]
    assert [row["id"] for row in saved["sound_effects"]] == ["sfx-1", "sfx-2"]


def test_a_later_text_only_save_keeps_the_effects(monkeypatch):
    job, variant, revision = _guided_phone_job(monkeypatch)
    _enable_guided_sfx(monkeypatch)
    _stub_library_probe(monkeypatch)
    _save(job, revision, sound_effects=[_sfx_row(at_s=2.0)])

    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    variant["render_generation_id"] = "second"
    text = [dict(row) for row in revision["text_elements"]]
    payload = gj.EditorCommitRequest(
        base_generation="second",
        guided_revision_number=revision["revision_number"],
        text_elements=text,
    )
    gj.prepare_editor_commit(job, "guided_story", payload, user_id="owner")

    request = device_status(job, "guided_story").request
    assert request.identity.recipe_revision == 3
    sfx = next(track for track in request.recipe.tracks if track.id == "sfx")
    assert [clip.id for clip in sfx.clips] == ["sfx-sfx-1"]


def test_flag_off_save_with_effects_is_still_a_named_422(monkeypatch):
    job, variant, revision = _guided_phone_job(monkeypatch)
    _enable_guided_sfx(monkeypatch, enabled=False)
    _stub_library_probe(monkeypatch)
    before = device_status(job, "guided_story").request

    with pytest.raises(HTTPException) as error:
        _save(job, revision, sound_effects=[_sfx_row()])

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"
    # A refused Save leaves the pinned baseline untouched.
    assert device_status(job, "guided_story").request == before


def test_custom_upload_effect_is_refused_not_dropped(monkeypatch):
    job, variant, revision = _guided_phone_job(monkeypatch)
    _enable_guided_sfx(monkeypatch)
    _stub_library_probe(monkeypatch)
    upload = {
        "id": "up-1",
        "src_gcs_path": "users/owner/plan/item/sfx/upload.m4a",
        "at_s": 1.0,
        "gain": 1.0,
        "duration_s": 1.0,
    }

    with pytest.raises(HTTPException) as error:
        _save(job, revision, sound_effects=[upload])

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"
    assert "custom sound effect" in error.value.detail["reason"]


@pytest.mark.parametrize("enabled", [True, False])
def test_capability_map_and_chat_follow_the_rollout_gate(monkeypatch, enabled):
    job, variant, _revision = _guided_phone_job(monkeypatch)
    _enable_guided_sfx(monkeypatch, enabled=enabled)

    capabilities = gj._editor_capabilities(job, variant)
    assert (capabilities.get("sfx") is True) is enabled
    if not enabled:
        assert capabilities["lanes"]["sfx"]["editable"] is False
    # Overlays stay closed on a guided phone edit either way.
    assert capabilities.get("overlays") is not True

    families = build_editor_snapshot(job, variant)["allowed_op_families"]
    assert ("sfx" in families) is enabled
