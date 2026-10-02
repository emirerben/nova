"""KRI-216 part A: the `caption_cues`/`caption_meta` editor-capability keys.

Covers `app/routes/generative_jobs.py`'s `_base_editor_capabilities` (cloud
montage/caption map + guided-story map) and `_clamp_phone_editor_capabilities`
(device maps). The server is the single source of truth for "can a Save
carrying `caption_cues` (line text/timing) or `caption_meta` (Style +
Settings) succeed" so iOS never has to infer editability from
`base_video_path` (the bug: a phone-rendered "Talking to camera" variant has
no `base_video_path`, so the old inference disabled the Captions panel even
though the phone subtitled compiler can Save it under the KRI-182 rollout).

`app/services/phone_subtitled_editor.py` / `app/services/phone_rollout.py` own
`is_phone_subtitled_editor_variant` / `phone_subtitled_editor_lanes_supported`;
`generative_jobs.py` re-exports thin wrappers at module scope so this file
monkeypatches `gj.<name>` directly, mirroring
`tests/routes/test_phone_subtitled_editor_capabilities.py`.
"""

from __future__ import annotations

import app.routes.generative_jobs as gj
from tests.routes.test_editor_commit import _arm, _arm_every_editor_lane, _job

_PHONE_CLOSED_CAPTION = {"editable": False, "reason": "phone_edit_unsupported"}


# --------------------------------------------------------------------------
# Cloud (non-device), non-guided archetypes
# --------------------------------------------------------------------------


def test_cloud_subtitled_with_base_is_editable(monkeypatch):
    _arm(monkeypatch)
    job = _job(resolved_archetype="subtitled")
    variant = job.assembly_plan["variants"][0]
    assert variant.get("base_video_path")  # fixture sanity

    caps = gj._editor_capabilities(job, variant)

    assert caps["caption_cues"] == {"editable": True, "reason": None}
    assert caps["caption_meta"] == {"editable": True, "reason": None}
    assert caps["caption_editor_style"] is True


def test_cloud_subtitled_without_base_is_not_editable(monkeypatch):
    _arm(monkeypatch)
    job = _job(resolved_archetype="subtitled", base_video_path=None)
    variant = job.assembly_plan["variants"][0]

    caps = gj._editor_capabilities(job, variant)

    assert caps["caption_cues"] == {"editable": False, "reason": "no_caption_base"}
    assert caps["caption_meta"] == {"editable": False, "reason": "no_caption_base"}
    assert caps["caption_editor_style"] is False


def test_cloud_narrated_with_base_is_editable(monkeypatch):
    _arm(monkeypatch)
    job = _job(resolved_archetype="narrated")
    variant = job.assembly_plan["variants"][0]

    caps = gj._editor_capabilities(job, variant)

    assert caps["caption_cues"] == {"editable": True, "reason": None}
    assert caps["caption_meta"] == {"editable": True, "reason": None}
    assert caps["caption_editor_style"] is True


def test_cloud_montage_is_unsupported_archetype(monkeypatch):
    _arm(monkeypatch)
    job = _job()  # default fixture: resolved_archetype is unset (montage)
    variant = job.assembly_plan["variants"][0]
    assert variant.get("base_video_path")  # not the gate that fails here

    caps = gj._editor_capabilities(job, variant)

    assert caps["caption_cues"] == {"editable": False, "reason": "unsupported_archetype"}
    assert caps["caption_meta"] == {"editable": False, "reason": "unsupported_archetype"}
    assert caps["caption_editor_style"] is False


# --------------------------------------------------------------------------
# Device variants
# --------------------------------------------------------------------------


def _subtitled_device_job(monkeypatch):
    _arm_every_editor_lane(monkeypatch)
    job = _job(resolved_archetype="subtitled")
    variant = {**job.assembly_plan["variants"][0], "render_destination": "device"}
    job.assembly_plan["variants"][0] = variant
    return job, variant


def test_device_subtitled_rollout_on_both_editable(monkeypatch):
    job, variant = _subtitled_device_job(monkeypatch)
    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: True)

    caps = gj._editor_capabilities(job, variant)

    assert caps["caption_cues"] == {"editable": True, "reason": None}
    assert caps["caption_meta"] == {"editable": True, "reason": None}
    assert caps["caption_editor_style"] is True


def test_device_subtitled_rollout_off_is_not_editable_with_phone_reason(monkeypatch):
    job, variant = _subtitled_device_job(monkeypatch)
    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: False)

    caps = gj._editor_capabilities(job, variant)

    assert caps["caption_cues"] == _PHONE_CLOSED_CAPTION
    assert caps["caption_meta"] == _PHONE_CLOSED_CAPTION
    assert caps["caption_editor_style"] is False


def _narrated_device_job(monkeypatch, *, enabled: bool, verified: list[str]):
    _arm_every_editor_lane(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_narrated_caption_edits_enabled", enabled)
    monkeypatch.setattr(gj.settings, "phone_render_verified_features", verified)
    job = _job(resolved_archetype="narrated")
    variant = {**job.assembly_plan["variants"][0], "render_destination": "device"}
    return job, variant


def test_device_narrated_is_editable_like_phone_talking(monkeypatch):
    """KRI-280: a phone Narrated variant opens both caption keys and the
    caption style control, the same map a phone Talking variant gets."""
    job, variant = _narrated_device_job(monkeypatch, enabled=True, verified=["positionedText"])

    device = gj._editor_capabilities(job, variant)

    assert device["caption_cues"] == {"editable": True, "reason": None}
    assert device["caption_meta"] == {"editable": True, "reason": None}
    assert device["caption_editor_style"] is True
    # Only the caption lane opens: the narrated compiler still has no text lane.
    assert device["text_elements"] is False


def test_device_narrated_rollout_off_is_not_editable(monkeypatch):
    job, variant = _narrated_device_job(monkeypatch, enabled=False, verified=["positionedText"])

    cloud = gj._editor_capabilities(job, job.assembly_plan["variants"][0])
    device = gj._editor_capabilities(job, variant)

    # Fixture sanity: the cloud map must actually offer this, or the clamp
    # proves nothing.
    assert cloud["caption_cues"]["editable"] is True
    assert cloud["caption_meta"]["editable"] is True

    assert device["caption_cues"] == _PHONE_CLOSED_CAPTION
    assert device["caption_meta"] == _PHONE_CLOSED_CAPTION
    assert device["caption_editor_style"] is False


def test_device_narrated_without_verified_text_is_not_editable(monkeypatch):
    job, variant = _narrated_device_job(monkeypatch, enabled=True, verified=[])

    device = gj._editor_capabilities(job, variant)

    assert device["caption_cues"] == _PHONE_CLOSED_CAPTION
    assert device["caption_meta"] == _PHONE_CLOSED_CAPTION
    assert device["caption_editor_style"] is False


def test_device_montage_is_not_editable(monkeypatch):
    _arm_every_editor_lane(monkeypatch)
    job = _job()  # default montage fixture
    variant = {**job.assembly_plan["variants"][0], "render_destination": "device"}

    device = gj._editor_capabilities(job, variant)

    assert device["caption_cues"] == _PHONE_CLOSED_CAPTION
    assert device["caption_meta"] == _PHONE_CLOSED_CAPTION
    assert device["caption_editor_style"] is False


def test_device_guided_story_leaves_caption_capability_untouched(monkeypatch):
    """A device guided_story variant's caption_meta/caption_cues are gated on
    the same revision-availability `operation()` as the cloud map, not on the
    phone lane clamp — the clamp must not regress today's behavior."""
    _arm_every_editor_lane(monkeypatch)
    monkeypatch.setattr(gj, "_guided_v2_revision", lambda *_args: {"revision_number": 1})
    job = _job(resolved_archetype="guided_story")
    variant = job.assembly_plan["variants"][0]

    cloud = gj._editor_capabilities(job, variant)
    device = gj._editor_capabilities(job, {**variant, "render_destination": "device"})

    assert cloud["caption_cues"] == {"editable": True, "reason": None}
    assert cloud["caption_meta"] == {"editable": True, "reason": None}
    assert device["caption_cues"] == cloud["caption_cues"]
    assert device["caption_meta"] == cloud["caption_meta"]
    assert device["caption_editor_style"] == cloud["caption_editor_style"]


def test_device_guided_story_without_revision_leaves_caption_capability_untouched(
    monkeypatch,
):
    """Same guarantee when the guided revision is unavailable: the clamp must
    not paper over `guided_story_revision_unavailable` with the phone reason."""
    _arm_every_editor_lane(monkeypatch)
    monkeypatch.setattr(gj, "_guided_v2_revision", lambda *_args: None)
    job = _job(resolved_archetype="guided_story")
    variant = job.assembly_plan["variants"][0]

    cloud = gj._editor_capabilities(job, variant)
    device = gj._editor_capabilities(job, {**variant, "render_destination": "device"})

    assert cloud["caption_cues"] == {
        "editable": False,
        "reason": "guided_story_revision_unavailable",
    }
    assert device["caption_cues"] == cloud["caption_cues"]
    assert device["caption_meta"] == cloud["caption_meta"]
    assert device["caption_editor_style"] == cloud["caption_editor_style"]


# --------------------------------------------------------------------------
# Device/cloud key-set parity
# --------------------------------------------------------------------------


def test_device_and_cloud_maps_keep_the_same_keys_including_caption_keys(monkeypatch):
    job, variant = _subtitled_device_job(monkeypatch)
    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: True)

    cloud = gj._editor_capabilities(job, {**variant, "render_destination": "cloud"})
    device = gj._editor_capabilities(job, variant)

    assert "caption_cues" in cloud and "caption_meta" in cloud
    assert device.keys() == cloud.keys()
