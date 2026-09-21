"""Worker-level tests for `_run_phone_subtitled_job` / `_run_phone_narrated_job`
(KRI-132), modeled on `test_phone_montage_dispatch.py`'s `setup()` -- same
fences (immutable generation, bound sources, single pinned device revision,
redelivery idempotency), but exercising the LEAN subtitled/narrated paths
instead of the montage-family archetype/spec prework.
"""

from __future__ import annotations

import copy
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.transcribe import Transcript, Word
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import generative_build as gb
from tests.pipeline.test_phone_montage_plan import _binding
from tests.tasks.test_generative_build import _Meta


def _job_and_session(monkeypatch, *, assembly_plan: dict, all_candidates: dict):
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        assembly_plan=copy.deepcopy(assembly_plan),
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
    monkeypatch.setattr(gb.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [
            "basicComposition",
            "local1080Export",
            "positionedText",
            "animatedText",
            "narrationAudio",
            "audioMix",
        ],
    )
    monkeypatch.setattr(gb.settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(gb.settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_narrated_rendering_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_narration_rendering_enabled", True)
    monkeypatch.setattr(gb.settings, "narrated_archetype_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "narrated_self_narration_enabled", True)
    # No network calls: skip the LLM-based caption spelling pass entirely.
    monkeypatch.setattr(gb.settings, "subtitled_caption_correction_enabled", False)
    return job, session


def _words(*spans: tuple[str, float, float]) -> list[Word]:
    return [Word(text=t, start_s=s, end_s=e, confidence=1.0) for t, s, e in spans]


# --- subtitled ("Talking to camera") ----------------------------------------


def _setup_subtitled(monkeypatch, *, edit_format="subtitled", transcript_words=None):
    binding = _binding("c0", duration_s=10.0)
    snapshot = {
        PHONE_SOURCES_FIELD: [binding.model_dump(mode="json")],
        "creator_generation_id": "generation",
    }
    all_candidates = {
        "clip_paths": ["phone-proxies/c0.mp4"],
        "edit_format": edit_format,
        "language": "en",
    }
    job, session = _job_and_session(
        monkeypatch, assembly_plan=snapshot, all_candidates=all_candidates
    )

    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": [_Meta("c0", 5.0)],
            "clip_id_to_gcs": {"c0": binding.proxy_path},
            "clip_id_to_local": {"c0": "/tmp/c0.mp4"},
            "probe_map": {},
            "hero": _Meta("c0", 5.0),
        },
        raising=False,
    )

    import app.pipeline.probe as probe_mod
    import app.pipeline.transcribe as transcribe_mod

    monkeypatch.setattr(
        probe_mod, "probe_video", lambda path: SimpleNamespace(duration_s=10.0), raising=False
    )
    words = transcript_words or _words(("Hello", 0.0, 0.5), ("there.", 0.5, 1.0))
    monkeypatch.setattr(
        transcribe_mod,
        "transcribe_whisper_cached",
        lambda *a, **k: Transcript(words=words, language="en"),
        raising=False,
    )
    return job, snapshot, session, binding


def test_subtitled_compiles_and_pins_device_request(monkeypatch):
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    status = device_status(job, "subtitled")
    assert status.request.identity.variant_id == "subtitled"
    variant = job.assembly_plan["variants"][0]
    assert variant["variant_id"] == "subtitled"
    assert variant["resolved_archetype"] == "subtitled"
    assert variant["render_status"] == "awaiting_device"
    assert variant["render_destination"] == "device"
    assert variant["caption_cues"]
    assert job.assembly_plan["phone_deferred_variants"] == []


def test_subtitled_redelivery_is_a_no_op(monkeypatch):
    job, snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    gb._run_generative_job(str(job.id))
    first_request = device_status(job, "subtitled").request

    reingest = Mock(side_effect=AssertionError("redelivery must not re-run ingest"))
    monkeypatch.setattr(gb, "_ingest_clips", reingest, raising=False)
    gb._run_phone_subtitled_job(
        str(job.id), copy.deepcopy(job.assembly_plan), job.all_candidates, ownership_epoch=3
    )
    assert device_status(job, "subtitled").request == first_request
    reingest.assert_not_called()


def test_subtitled_rejects_multi_clip_bindings(monkeypatch):
    job, snapshot, _session, binding = _setup_subtitled(monkeypatch)
    extra = _binding("c1", duration_s=8.0)
    snapshot[PHONE_SOURCES_FIELD] = [
        binding.model_dump(mode="json"),
        extra.model_dump(mode="json"),
    ]
    with pytest.raises(ValueError, match="exactly one clip"):
        gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


def test_subtitled_rejects_when_flag_off(monkeypatch):
    job, snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_rendering_enabled", False)
    with pytest.raises(ValueError, match="talking-to-camera"):
        gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


def test_subtitled_fails_closed_on_required_speech_cleanup_contract(monkeypatch):
    job, snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    snapshot["speech_cleanup_contract"] = "required_v1"
    with pytest.raises(UnsupportedPhonePlan, match="speech-cleanup"):
        gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


def test_subtitled_rejects_clip_over_five_minutes(monkeypatch):
    job, snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    import app.pipeline.probe as probe_mod

    monkeypatch.setattr(
        probe_mod, "probe_video", lambda path: SimpleNamespace(duration_s=301.0), raising=False
    )
    with pytest.raises(UnsupportedPhonePlan, match="5 minutes"):
        gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


def test_self_narrated_narrated_format_resolves_through_subtitled_compiler(monkeypatch):
    """A `narrated_ready` item with NO voiceover -- the dispatch gate only
    lets the single-clip self-narration shape through, which the worker fork
    routes to `_run_phone_subtitled_job`. This re-verifies the REAL archetype
    resolution and only proceeds when it lands on `subtitled`."""
    job, snapshot, _session, _binding_ = _setup_subtitled(monkeypatch, edit_format="narrated_ready")
    monkeypatch.setattr(
        gb, "_resolve_archetype", lambda *a, **k: ("subtitled", None, None), raising=False
    )
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert variant["resolved_archetype"] == "subtitled"


def test_self_narrated_resolving_to_talking_head_fails_closed(monkeypatch):
    """The real `_resolve_archetype` can decide `talking_head`/`montage` for a
    self-narrated item once footage is actually probed -- neither has a
    phone compiler reachable from this runner, so it must fail closed rather
    than silently render the wrong shape."""
    job, snapshot, _session, _binding_ = _setup_subtitled(monkeypatch, edit_format="narrated_ready")
    monkeypatch.setattr(
        gb, "_resolve_archetype", lambda *a, **k: ("talking_head", "c0", None), raising=False
    )
    with pytest.raises(UnsupportedPhonePlan, match="unsupported"):
        gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


def test_subtitled_worker_rejects_direct_format_it_does_not_own(monkeypatch):
    """`_run_phone_subtitled_job`'s own defense-in-depth: only `subtitled` or
    a no-voiceover narrated* item may reach it -- a montage-family item
    (routed to `_run_phone_montage_job` by the dispatch fork in normal
    operation) is rejected if ever called directly."""
    job, snapshot, _session, _binding_ = _setup_subtitled(monkeypatch, edit_format="montage")
    with pytest.raises(ValueError, match="No phone renderer is registered"):
        gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


# --- narrated (WITH a recorded voiceover) -----------------------------------


def _fake_narration_bed(_job_id, voiceover_gcs_path):
    if not voiceover_gcs_path:
        return None
    from app.kria.render_assets import RenderFingerprint
    from app.pipeline.phone_recipe_shared import PhoneNarrationBed

    return PhoneNarrationBed(
        plan_item_id="item-1",
        generation="9",
        fingerprint=RenderFingerprint(sha256="d" * 64, byte_count=999),
        duration_s=12.0,
    )


def _setup_narrated(monkeypatch, *, edit_format="narrated_ready", filming_guide=None):
    bindings = tuple(_binding(f"c{i}", duration_s=10.0) for i in range(3))
    snapshot = {
        PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
        "creator_generation_id": "generation",
    }
    all_candidates = {
        "clip_paths": [f"phone-proxies/c{i}.mp4" for i in range(3)],
        "edit_format": edit_format,
        "voiceover_gcs_path": "voiceover-uploads/direct/u/i/voice.m4a",
        "filming_guide": filming_guide or [],
        "language": "en",
    }
    job, session = _job_and_session(
        monkeypatch, assembly_plan=snapshot, all_candidates=all_candidates
    )

    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": [_Meta(f"c{i}", 5.0) for i in range(3)],
            "clip_id_to_gcs": {f"c{i}": bindings[i].proxy_path for i in range(3)},
            "clip_id_to_local": {f"c{i}": f"/tmp/c{i}.mp4" for i in range(3)},
            "probe_map": {},
            "hero": _Meta("c0", 5.0),
        },
        raising=False,
    )
    monkeypatch.setattr(gb, "_resolve_phone_voiceover_bed", _fake_narration_bed, raising=False)

    import app.pipeline.phrase_sequence as phrase_mod
    import app.pipeline.transcribe as transcribe_mod
    import app.storage as storage_mod
    import app.tasks.template_orchestrate as to

    words = _words(
        ("First", 0.0, 0.5),
        ("clip.", 0.5, 1.0),
        ("Second", 4.0, 4.5),
        ("clip.", 4.5, 5.0),
        ("Third", 8.0, 8.5),
        ("clip.", 8.5, 9.0),
    )
    monkeypatch.setattr(
        transcribe_mod,
        "transcribe_whisper",
        lambda *a, **k: Transcript(words=words, language="en"),
        raising=False,
    )
    monkeypatch.setattr(storage_mod, "download_to_file", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(to, "_probe_duration", lambda *a, **k: 12.0, raising=False)
    monkeypatch.setattr(
        phrase_mod,
        "split_phrases",
        lambda *a, **k: [
            {"speech_start_s": 0.0, "speech_end_s": 4.0},
            {"speech_start_s": 4.0, "speech_end_s": 8.0},
            {"speech_start_s": 8.0, "speech_end_s": 12.0},
        ],
        raising=False,
    )
    return job, snapshot, session, bindings


def test_narrated_ready_compiles_and_pins_device_request(monkeypatch):
    job, _snapshot, _session, _bindings = _setup_narrated(monkeypatch)
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    status = device_status(job, "narrated")
    assert status.request.identity.variant_id == "narrated"
    assert status.request.recipe.duration == pytest.approx(12.0)
    variant = job.assembly_plan["variants"][0]
    assert variant["variant_id"] == "narrated"
    assert variant["resolved_archetype"] == "narrated"
    assert variant["render_status"] == "awaiting_device"
    assert variant["render_destination"] == "device"
    assert variant["caption_cues"]
    video_track = next(t for t in status.request.recipe.tracks if t.kind == "video")
    assert [clip.timeline_start for clip in video_track.clips] == [0.0, 4.0, 8.0]


def test_narrated_scripted_two_plus_steps_uses_force_alignment(monkeypatch):
    filming_guide = [
        {"shot_id": "s0", "what": "First clip narration"},
        {"shot_id": "s1", "what": "Second clip narration"},
        {"shot_id": "s2", "what": "Third clip narration"},
    ]
    job, _snapshot, _session, _bindings = _setup_narrated(
        monkeypatch, edit_format="narrated_planned", filming_guide=filming_guide
    )

    import app.pipeline.narrated_alignment as alignment_mod

    monkeypatch.setattr(
        alignment_mod,
        "align_script_to_voiceover",
        lambda steps, words: [
            alignment_mod.StepTiming(
                step_id=s.step_id, start_s=i * 4.0, end_s=(i + 1) * 4.0, confidence=1.0
            )
            for i, s in enumerate(steps)
        ],
        raising=False,
    )
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"


def test_narrated_rejects_when_voiceover_flag_off(monkeypatch):
    job, snapshot, _session, _bindings = _setup_narrated(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_narration_rendering_enabled", False)
    with pytest.raises(ValueError, match="voiceover"):
        gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


def test_narrated_rejects_without_a_voiceover(monkeypatch):
    job, snapshot, _session, _bindings = _setup_narrated(monkeypatch)
    candidates = {**job.all_candidates, "voiceover_gcs_path": None}
    with pytest.raises(ValueError, match="recorded voiceover"):
        gb._run_phone_narrated_job(str(job.id), snapshot, candidates, ownership_epoch=3)


def test_narrated_fails_closed_on_required_speech_cleanup_contract(monkeypatch):
    job, snapshot, _session, _bindings = _setup_narrated(monkeypatch)
    snapshot["speech_cleanup_contract"] = "required_v1"
    with pytest.raises(UnsupportedPhonePlan, match="speech-cleanup"):
        gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


def test_narrated_bed_level_mix_math_matches_montage_convention(monkeypatch):
    job, snapshot, _session, _bindings = _setup_narrated(monkeypatch)
    candidates = {**job.all_candidates, "voiceover_bed_level": 0.4}
    gb._run_phone_narrated_job(str(job.id), snapshot, candidates, ownership_epoch=3)
    status = device_status(job, "narrated")
    assert status.request.recipe.audio.original_volume == pytest.approx(0.4)


def test_narrated_worker_rejects_format_it_does_not_own(monkeypatch):
    job, snapshot, _session, _bindings = _setup_narrated(monkeypatch, edit_format="montage")
    with pytest.raises(ValueError, match="No phone renderer is registered"):
        gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
