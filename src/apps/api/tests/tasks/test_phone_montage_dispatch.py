import copy
import types
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.device_render import device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import generative_build as gb
from tests.pipeline.test_phone_montage_plan import _binding
from tests.tasks.test_generative_build import _Meta, _track


def _patch_decide_phase(monkeypatch, *, steps):
    """Let `_decide_generative_variant` run for real (matcher/consolidate_slots/
    build_recipe are stubbed the same way `test_decide_generative_variant_
    performs_no_media_processing` does in test_generative_build.py), so this
    test exercises the actual decision pipeline rather than mocking it away."""
    import app.pipeline.agents.gemini_analyzer as ga
    import app.pipeline.template_matcher as tm
    import app.tasks.template_orchestrate as to

    monkeypatch.setattr(
        ga,
        "build_recipe",
        lambda d: types.SimpleNamespace(
            beat_timestamps_s=d.get("beat_timestamps_s", []), color_grade="none"
        ),
        raising=False,
    )
    monkeypatch.setattr(tm, "consolidate_slots", lambda recipe, metas, **kw: recipe, raising=False)
    monkeypatch.setattr(
        tm, "match", lambda recipe, metas, **kw: types.SimpleNamespace(steps=steps), raising=False
    )

    class _Mismatch(Exception):
        code = "x"
        message = "y"

    monkeypatch.setattr(tm, "TemplateMismatchError", _Mismatch, raising=False)
    monkeypatch.setattr(to, "_enrich_slots_with_energy", lambda slots, beats: slots, raising=False)


def setup(monkeypatch, *, edit_format="montage", archetype="montage", track=None, spec=None):
    import app.pipeline.agents.gemini_analyzer as ga
    import app.pipeline.text_overlay_skia as skia_mod
    import app.storage as storage
    import app.tasks.template_orchestrate as to

    bindings = (_binding("c0"), _binding("c1"))
    steps = [
        ga.AssemblyStep(
            slot={"target_duration_s": 4.0}, clip_id="c0", moment={"start_s": 0.0, "end_s": 4.0}
        ),
        ga.AssemblyStep(
            slot={
                "target_duration_s": 4.0,
                "transition_in": "crossfade",
                "transition_duration_s": 0.3,
            },
            clip_id="c1",
            moment={"start_s": 0.0, "end_s": 4.0},
        ),
    ]
    _patch_decide_phase(monkeypatch, steps=steps)

    snapshot = {
        PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
        "creator_generation_id": "generation",
    }
    all_candidates = {
        "clip_paths": ["phone-proxies/c0.mp4", "phone-proxies/c1.mp4"],
        "edit_format": edit_format,
    }
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        assembly_plan=copy.deepcopy(snapshot),
        status="queued",
        all_candidates=all_candidates,
        error_detail=None,
        failure_reason=None,
    )
    session = Mock()

    @contextmanager
    def sessions():
        yield session

    monkeypatch.setattr(gb, "_sync_session", sessions)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda *args: (job, 3))
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": [_Meta("c0", 5.0), _Meta("c1", 5.0)],
            "clip_id_to_gcs": {"c0": bindings[0].proxy_path, "c1": bindings[1].proxy_path},
            "clip_id_to_local": {"c0": "/tmp/c0.mp4", "c1": "/tmp/c1.mp4"},
            "probe_map": {},
            "hero": _Meta("c0", 5.0),
        },
        raising=False,
    )
    monkeypatch.setattr(gb, "_run_text_agents", lambda *a, **k: (None, {}), raising=False)
    monkeypatch.setattr(
        gb, "_select_generative_style_set", lambda *a, **k: "default", raising=False
    )
    monkeypatch.setattr(gb, "_match_best_track", lambda *a, **k: track, raising=False)
    monkeypatch.setattr(
        gb, "_resolve_archetype", lambda *a, **k: (archetype, None, None), raising=False
    )
    resolved_spec = spec or {
        "variant_id": "original_text",
        "text_mode": "none",
        "track": track,
    }
    monkeypatch.setattr(gb, "_specs_for_archetype", lambda *a, **k: [resolved_spec], raising=False)

    def _fake_music_bed(decision):
        if not decision.music_track_id:
            return None
        from app.kria.render_assets import RenderFingerprint
        from app.pipeline.phone_recipe_shared import PhoneMusicBed

        return PhoneMusicBed(
            catalog_id=decision.music_track_id,
            generation="7",
            fingerprint=RenderFingerprint(sha256="c" * 64, byte_count=500),
            duration_s=60.0,
            start_s=float(decision.music_start_s or 0.0),
            volume=1.0,
        )

    monkeypatch.setattr(gb, "_resolve_phone_music_bed", _fake_music_bed, raising=False)
    monkeypatch.setattr(gb.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "crossfade", "audioMix", "musicBed"],
    )

    def _boom(*a, **k):
        raise AssertionError("phone montage decide phase must not touch media processing")

    monkeypatch.setattr(to, "_assemble_clips", _boom, raising=False)
    monkeypatch.setattr(to, "_mix_template_audio", _boom, raising=False)
    monkeypatch.setattr(storage, "upload_public_read", _boom, raising=False)
    monkeypatch.setattr(skia_mod, "burn_text_overlays_skia", _boom, raising=False)
    monkeypatch.setattr("subprocess.run", _boom, raising=False)
    cloud = Mock(side_effect=AssertionError("phone job entered the cloud renderer"))
    monkeypatch.setattr(gb, "_run_phone_guided_job", cloud)
    return job, snapshot, session, bindings, cloud


def test_pins_device_record_and_variant_without_assembling(monkeypatch):
    job, _snapshot, session, _bindings, cloud = setup(monkeypatch)
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    status = device_status(job, "original_text")
    assert status.request.identity.variant_id == "original_text"
    assert status.request.recipe.duration == pytest.approx(7.7)
    variant = job.assembly_plan["variants"][0]
    assert variant["variant_id"] == "original_text"
    assert variant["rank"] == 1
    assert variant["render_status"] == "awaiting_device"
    assert variant["render_destination"] == "device"
    assert variant["render_generation_id"] == "generation"
    assert variant["ok"] is False
    assert job.assembly_plan["phone_deferred_variants"] == []
    cloud.assert_not_called()


def test_redelivery_with_same_generation_is_a_no_op(monkeypatch):
    job, snapshot, session, _bindings, _cloud = setup(monkeypatch)
    gb._run_generative_job(str(job.id))
    first_request = device_status(job, "original_text").request

    reingest = Mock(side_effect=AssertionError("redelivery must not re-run ingest"))
    monkeypatch.setattr(gb, "_ingest_clips", reingest, raising=False)
    gb._run_phone_montage_job(
        str(job.id), copy.deepcopy(job.assembly_plan), job.all_candidates, ownership_epoch=3
    )
    assert device_status(job, "original_text").request == first_request
    reingest.assert_not_called()


def test_ownership_epoch_mismatch_bails_before_publishing(monkeypatch):
    job, snapshot, session, _bindings, _cloud = setup(monkeypatch)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda *args: (job, 99))
    gb._run_phone_montage_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert "_device_render_v1" not in job.assembly_plan
    session.commit.assert_not_called()


def test_voiceover_job_is_rejected(monkeypatch):
    job, snapshot, session, _bindings, _cloud = setup(monkeypatch)
    candidates = {**job.all_candidates, "voiceover_gcs_path": "users/u/voice.m4a"}
    with pytest.raises(ValueError, match="voiceover"):
        gb._run_phone_montage_job(str(job.id), snapshot, candidates, ownership_epoch=3)
    session.commit.assert_not_called()


def test_deferred_specs_are_recorded_and_not_lost(monkeypatch):
    track = _track("track1")
    specs = [
        {"variant_id": "song_text", "text_mode": "none", "track": track},
        {"variant_id": "original_text", "text_mode": "none", "track": None},
    ]
    job, snapshot, session, _bindings, _cloud = setup(monkeypatch, track=track)
    monkeypatch.setattr(gb, "_specs_for_archetype", lambda *a, **k: specs, raising=False)
    gb._run_generative_job(str(job.id))

    assert job.assembly_plan["variants"][0]["variant_id"] == "song_text"
    assert job.assembly_plan["phone_deferred_variants"] == ["original_text"]


def test_per_variant_upsert_replaces_awaiting_and_refuses_ready(monkeypatch):
    job, snapshot, session, _bindings, _cloud = setup(monkeypatch)
    gb._run_generative_job(str(job.id))
    assert len(job.assembly_plan["variants"]) == 1

    # A second, later generation may replace an awaiting entry in place.
    job.assembly_plan["creator_generation_id"] = "generation-2"
    job.assembly_plan[PHONE_SOURCES_FIELD] = snapshot[PHONE_SOURCES_FIELD]
    del job.assembly_plan["_device_render_v1"]["original_text"]
    new_snapshot = copy.deepcopy(job.assembly_plan)
    gb._run_phone_montage_job(str(job.id), new_snapshot, job.all_candidates, ownership_epoch=3)
    assert len(job.assembly_plan["variants"]) == 1
    assert job.assembly_plan["variants"][0]["render_generation_id"] == "generation-2"

    # A ready entry must never be silently replaced.
    job.assembly_plan["variants"][0]["render_status"] = "ready"
    job.assembly_plan["creator_generation_id"] = "generation-3"
    del job.assembly_plan["_device_render_v1"]["original_text"]
    newer_snapshot = copy.deepcopy(job.assembly_plan)
    with pytest.raises(ValueError, match="ready"):
        gb._run_phone_montage_job(
            str(job.id), newer_snapshot, job.all_candidates, ownership_epoch=3
        )


def test_dispatcher_routes_montage_and_guided_snapshots_to_their_own_runner(monkeypatch):
    job, snapshot, session, _bindings, _cloud = setup(monkeypatch)
    montage_runner = Mock()
    guided_runner = Mock()
    monkeypatch.setattr(gb, "_run_phone_montage_job", montage_runner)
    monkeypatch.setattr(gb, "_run_phone_guided_job", guided_runner)

    gb._run_generative_job(str(job.id))
    montage_runner.assert_called_once()
    guided_runner.assert_not_called()

    montage_runner.reset_mock()
    job.assembly_plan["guided_edit"] = {"approved": True}
    gb._run_generative_job(str(job.id))
    guided_runner.assert_called_once()
    montage_runner.assert_not_called()
