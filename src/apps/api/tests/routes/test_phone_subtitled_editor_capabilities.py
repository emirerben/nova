"""KRI-182 step 1 (Lane B — server routes): editable phone Talking (subtitled) edits.

Covers the two `app/routes/generative_jobs.py` edit points this step owns:

1. `_clamp_phone_editor_capabilities` / `_editor_capabilities` — a subtitled
   device variant keeps `sfx`/`overlays` open (matching the cloud map) when
   `phone_subtitled_editor_lanes_supported()` is true, while `visual_blocks`,
   `motion_scenes`, `camera_effects`, the clip operations, and `text_elements`
   stay closed. Flag off (the default) must stay byte-identical to today.
2. `_variants_for_response` / `_native_editor_assets` — lazily backfill
   `sound_effects`/`media_overlays` for a subtitled device variant from the
   pinned recipe (`project_phone_subtitled_editor_sections`) whenever the
   variant hasn't persisted them yet (i.e. never Saved through the editor).

`app/services/phone_subtitled_editor.py` owns `is_phone_subtitled_editor_variant`
/ `project_phone_subtitled_editor_sections` and `app/services/phone_rollout.py`
owns `phone_subtitled_editor_lanes_supported`; `generative_jobs.py` re-exports
thin, lazily-imported wrappers of those three names at module scope so this
file can monkeypatch `gj.<name>` directly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import app.routes.generative_jobs as gj
from tests.routes.test_editor_commit import _arm_every_editor_lane, _job

_PHONE_CLOSED = {"editable": False, "reason": "phone_edit_unsupported"}


def test_lanes_available_requires_both_variant_shape_and_flag(monkeypatch):
    job = SimpleNamespace()
    variant: dict = {}

    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: False)
    assert gj._phone_subtitled_editor_lanes_available(job, variant) is False

    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: False)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: True)
    assert gj._phone_subtitled_editor_lanes_available(job, variant) is False

    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: True)
    assert gj._phone_subtitled_editor_lanes_available(job, variant) is True


# --------------------------------------------------------------------------
# Capability clamp
# --------------------------------------------------------------------------


def _subtitled_job(monkeypatch):
    """A subtitled device-eligible job/variant, mirroring
    `test_editor_commit.test_device_variant_closes_the_camera_lane`."""
    _arm_every_editor_lane(monkeypatch)
    # Open the cloud subtitled text lane too, so `text_elements` is True on
    # the cloud map and the phone clamp closing it under the flag is a real
    # (non-trivial) assertion rather than an already-closed no-op.
    monkeypatch.setattr(gj.settings, "subtitled_text_lane_enabled", True, raising=False)
    job = _job(resolved_archetype="subtitled")
    variant = {**job.assembly_plan["variants"][0], "base_video_path": "base.mp4"}
    job.assembly_plan["variants"][0] = variant
    return job, variant


def test_flag_off_capability_clamp_is_byte_identical_to_today(monkeypatch):
    job, variant = _subtitled_job(monkeypatch)
    # A variant that DOES look like a subtitled-editor candidate, but the
    # rollout flag is off — the byte-identical guarantee is about the flag,
    # not the variant shape.
    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: False)

    cloud = gj._editor_capabilities(job, variant)
    device = gj._editor_capabilities(job, {**variant, "render_destination": "device"})

    # Fixture sanity: the cloud map must actually offer these, or the clamp proves nothing.
    assert cloud["sfx"] is True
    assert cloud["overlays"] is True
    assert cloud["text_elements"] is True

    for lane in ("sfx", "overlays", "visual_blocks", "motion_scenes", "camera_effects"):
        assert device[lane] is False
        assert device[f"{lane}_reason"] == "phone_edit_unsupported"
    # text_elements is untouched by the clamp when the flag is off — today's
    # (admittedly buggy) behavior is preserved byte-for-byte.
    assert device["text_elements"] == cloud["text_elements"]
    assert device.keys() == cloud.keys()


def test_flag_on_keeps_sfx_and_overlays_open_closes_everything_else(monkeypatch):
    job, variant = _subtitled_job(monkeypatch)
    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: True)

    cloud = gj._editor_capabilities(job, variant)
    device = gj._editor_capabilities(job, {**variant, "render_destination": "device"})

    assert cloud["sfx"] is True
    assert cloud["overlays"] is True

    # sfx/overlays are kept EXACTLY as the base map computed them — same value,
    # same (missing) reason.
    assert device["sfx"] == cloud["sfx"]
    assert device["sfx_reason"] == cloud["sfx_reason"]
    assert device["overlays"] == cloud["overlays"]
    assert device["overlays_reason"] == cloud["overlays_reason"]

    # Everything the phone subtitled compiler has no lane for stays closed.
    for lane in ("visual_blocks", "motion_scenes", "camera_effects"):
        assert device[lane] is False
        assert device[f"{lane}_reason"] == "phone_edit_unsupported"
    assert device["text_elements"] is False

    # Device and cloud maps carry the SAME keys (iOS/web decoders share one shape).
    assert device.keys() == cloud.keys()


def test_flag_on_leaves_clip_operations_and_lanes_dicts_closed_when_present(monkeypatch):
    """Legacy/subtitled capability maps carry no `clips`/`lanes` operation-group
    dicts (those are guided_story-only) — the clamp must not invent them, and
    must not zero a `("lanes", "sfx")`/`("lanes", "overlays")` entry if a
    future archetype shape ever does carry one alongside the flat booleans."""
    job, variant = _subtitled_job(monkeypatch)
    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: True)

    cloud = gj._editor_capabilities(job, variant)
    device = gj._editor_capabilities(job, {**variant, "render_destination": "device"})

    assert "clips" not in cloud and "clips" not in device
    assert "lanes" not in cloud and "lanes" not in device

    # Exercise the (group, name) carve-out directly against a synthetic
    # guided_story-shaped `lanes` dict to prove the operation-group loop
    # itself respects the carve-out (not just the flat top-level booleans).
    synthetic = {
        "lanes": {
            "sfx": {"editable": True, "reason": None},
            "overlays": {"editable": True, "reason": None},
            "visual_blocks": {"editable": True, "reason": None},
            "motion_scenes": {"editable": True, "reason": None},
        }
    }
    clamped = gj._clamp_phone_editor_capabilities(synthetic, subtitled_lanes=True)
    assert clamped["lanes"]["sfx"] == {"editable": True, "reason": None}
    assert clamped["lanes"]["overlays"] == {"editable": True, "reason": None}
    assert clamped["lanes"]["visual_blocks"] == _PHONE_CLOSED
    assert clamped["lanes"]["motion_scenes"] == _PHONE_CLOSED


# --------------------------------------------------------------------------
# `_variants_for_response` — lazy projection on read
# --------------------------------------------------------------------------


def _subtitled_device_variant(**extra) -> dict:
    return {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        **extra,
    }


def test_variants_for_response_leaves_sections_absent_when_flag_off(monkeypatch):
    job = SimpleNamespace(
        id=uuid.uuid4(), assembly_plan={"variants": [_subtitled_device_variant()]}
    )
    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: False)

    def boom(*_args, **_kwargs):
        raise AssertionError("projection must not run when the flag is off")

    monkeypatch.setattr(gj, "project_phone_subtitled_editor_sections", boom)

    [out] = gj._variants_for_response(job)
    assert "sound_effects" not in out
    assert "media_overlays" not in out


def test_variants_for_response_fills_missing_sections_from_projection(monkeypatch):
    job = SimpleNamespace(
        id=uuid.uuid4(), assembly_plan={"variants": [_subtitled_device_variant()]}
    )
    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: True)
    monkeypatch.setattr(gj, "signed_get_url", lambda p, ttl=None: f"https://signed/{p}")
    sfx = [{"id": "sfx-1", "src_gcs_path": "sound-effects/pop/a.wav", "at_s": 1.0, "gain": 0.8}]
    overlays = [
        {
            "id": "card-1",
            "kind": "image",
            "src_gcs_path": "users/u/plan/p/card.png",
            "source": "phone_lane",
        }
    ]
    monkeypatch.setattr(
        gj,
        "project_phone_subtitled_editor_sections",
        lambda assembly_plan, variant: {"sound_effects": sfx, "media_overlays": overlays},
    )

    [out] = gj._variants_for_response(job)
    assert [e["id"] for e in out["sound_effects"]] == ["sfx-1"]
    assert out["media_overlays"][0]["id"] == "card-1"
    # Backfilled cards still go through the normal preview-url signing step.
    assert out["media_overlays"][0]["preview_url"] == "https://signed/users/u/plan/p/card.png"
    # The raw job.assembly_plan is never mutated by the lazy projection.
    assert "sound_effects" not in job.assembly_plan["variants"][0]
    assert "media_overlays" not in job.assembly_plan["variants"][0]


def test_variants_for_response_never_overwrites_a_present_key(monkeypatch):
    job = SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={
            "variants": [
                _subtitled_device_variant(
                    sound_effects=[{"id": "existing", "src_gcs_path": "sound-effects/x.wav"}]
                )
            ]
        },
    )
    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: True)
    overlays = [{"id": "card-1", "kind": "image", "src_gcs_path": "users/u/plan/p/card.png"}]
    monkeypatch.setattr(
        gj,
        "project_phone_subtitled_editor_sections",
        lambda assembly_plan, variant: {
            "sound_effects": [{"id": "should-not-appear", "src_gcs_path": "sound-effects/y.wav"}],
            "media_overlays": overlays,
        },
    )

    [out] = gj._variants_for_response(job)
    # sound_effects already persisted -> untouched (only media_overlays, the
    # actually-missing key, gets backfilled).
    assert [e["id"] for e in out["sound_effects"]] == ["existing"]
    assert out["media_overlays"][0]["id"] == "card-1"


def test_variants_for_response_ignores_non_subtitled_device_variants(monkeypatch):
    """A guided_story (or any non-subtitled) device variant must never be
    routed through the subtitled projection, even with the flag on."""
    job = SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={
            "variants": [
                {
                    "variant_id": "guided_story",
                    "resolved_archetype": "guided_story",
                    "render_destination": "device",
                }
            ]
        },
    )
    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: False)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: True)

    def boom(*_args, **_kwargs):
        raise AssertionError("projection must not run for a non-subtitled-editor variant")

    monkeypatch.setattr(gj, "project_phone_subtitled_editor_sections", boom)

    [out] = gj._variants_for_response(job)
    assert "sound_effects" not in out
    assert "media_overlays" not in out


# --------------------------------------------------------------------------
# `_native_editor_assets` — timeline source pool
# --------------------------------------------------------------------------


def test_native_editor_assets_include_derived_phone_lane_rows(monkeypatch):
    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: True)
    monkeypatch.setattr(
        gj,
        "project_phone_subtitled_editor_sections",
        lambda assembly_plan, variant: {
            "sound_effects": [{"id": "sfx-1", "src_gcs_path": "sound-effects/pop/a.wav"}],
            "media_overlays": [
                {
                    "id": "card-1",
                    "kind": "image",
                    "src_gcs_path": "users/u/plan/p/card.png",
                    "source": "phone_lane",
                }
            ],
        },
    )
    job = SimpleNamespace(
        id=uuid.uuid4(), assembly_plan={"variants": [_subtitled_device_variant()]}
    )

    assets = gj._native_editor_assets(
        job, "subtitled", sign_url=lambda path, ttl: f"https://signed/{path}"
    )
    by_kind = {a["kind"]: a for a in assets}
    assert by_kind["sound_effect"]["id"] == "sfx-1"
    assert by_kind["media_overlay"]["id"] == "card-1"
    # A phone-lane image card keeps alpha the way the native compositor
    # already renders it, independent of the cloud-only
    # `media_overlay_alpha_enabled` flag (default False, unset here).
    assert by_kind["media_overlay"]["preserve_alpha"] is True


def test_native_editor_assets_omit_derived_rows_when_flag_off(monkeypatch):
    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: False)

    def boom(*_args, **_kwargs):
        raise AssertionError("projection must not run when the flag is off")

    monkeypatch.setattr(gj, "project_phone_subtitled_editor_sections", boom)
    job = SimpleNamespace(
        id=uuid.uuid4(), assembly_plan={"variants": [_subtitled_device_variant()]}
    )

    assets = gj._native_editor_assets(
        job, "subtitled", sign_url=lambda path, ttl: f"https://signed/{path}"
    )
    assert assets == []


def test_native_editor_assets_non_phone_lane_image_card_keeps_flag_gated_alpha(monkeypatch):
    """Regression guard: an ordinary (non-`phone_lane`) image card must keep
    the pre-existing, cloud-flag-gated `preserve_alpha` behavior untouched."""
    job = SimpleNamespace(
        assembly_plan={
            "variants": [
                {
                    "variant_id": "one",
                    "media_overlays": [
                        {"id": "image", "kind": "image", "src_gcs_path": "users/owner/photo.png"},
                    ],
                }
            ]
        }
    )
    for enabled in (False, True):
        monkeypatch.setattr(gj.settings, "media_overlay_alpha_enabled", enabled)
        [asset] = gj._native_editor_assets(
            job, "one", sign_url=lambda path, ttl: f"https://signed/{path}"
        )
        assert asset["preserve_alpha"] is enabled


# --------------------------------------------------------------------------
# Real catalog audio paths for lanes derived from the pinned recipe
# --------------------------------------------------------------------------


def test_native_editor_assets_prefer_the_catalog_path_for_phone_lane_rows(monkeypatch):
    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: True)
    monkeypatch.setattr(
        gj,
        "project_phone_subtitled_editor_sections",
        lambda assembly_plan, variant: {
            "sound_effects": [
                {
                    "id": "sfx-1",
                    "sound_effect_id": "pop",
                    "src_gcs_path": "sound-effects/pop/pop",
                    "source": "phone_lane",
                }
            ],
            "media_overlays": [],
        },
    )
    job = SimpleNamespace(
        id=uuid.uuid4(), assembly_plan={"variants": [_subtitled_device_variant()]}
    )

    assets = gj._native_editor_assets(
        job,
        "subtitled",
        sign_url=lambda path, ttl: f"https://signed/{path}",
        sfx_paths={"pop": "sound-effects/pop/audio.m4a"},
    )

    assert [a["source_url"] for a in assets] == ["https://signed/sound-effects/pop/audio.m4a"]


async def test_phone_subtitled_sfx_paths_reads_catalog_rows_only_for_derived_lanes(monkeypatch):
    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: True)
    monkeypatch.setattr(
        gj,
        "project_phone_subtitled_editor_sections",
        lambda assembly_plan, variant: {
            "sound_effects": [{"id": "sfx-1", "sound_effect_id": "pop", "source": "phone_lane"}],
            "media_overlays": [],
        },
    )
    executed: list = []

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(id="pop", audio_gcs_path="sound-effects/pop/audio.m4a")]

    class _DB:
        async def execute(self, statement):
            executed.append(statement)
            return _Result()

    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan={})

    paths = await gj._phone_subtitled_sfx_paths(_DB(), job, _subtitled_device_variant())
    assert paths == {"pop": "sound-effects/pop/audio.m4a"}
    assert len(executed) == 1

    # A variant that already persisted its sound lane (a Save happened) needs
    # no lookup, and neither does one outside the gate.
    persisted = _subtitled_device_variant(sound_effects=[])
    assert await gj._phone_subtitled_sfx_paths(_DB(), job, persisted) == {}
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: False)
    assert await gj._phone_subtitled_sfx_paths(_DB(), job, _subtitled_device_variant()) == {}
    assert len(executed) == 1
