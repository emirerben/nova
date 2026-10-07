"""KRI-470 PR-E: the real classic renderers emit receipts the verifier accepts or refuses.

Drives `_render_generative_variant` (montage / song / voiceover) through its real
decision + processing code with only the ffmpeg/GCS edges stubbed, then hands the
returned variant to the real publication path (`_finalize_job`).

Failure modes written first:
  * a voiceover job renders fully and is then rejected because the classic renderer
    emitted no receipt (the regression this PR fixes);
  * the voiceover mixer fails, copies the video through, and the receipt still
    claims the recording (`narration_applied` copied from intent);
  * camera audio is reported for a song variant that replaced it, or a muted
    voice-only mix is reported as keeping the footage sound;
  * a job without the contract marker grows a receipt (byte-identity);
  * a contract-bound job's receipt lists clips that were never in the output.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

import app.tasks.generative_build as gb
from app.services.cloud_render_contract import verify_cloud_variant
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
    build_render_contract,
)
from app.tasks.template_orchestrate import VoiceoverMixOutcome
from tests.tasks.conftest import FakeJob, patch_job_session
from tests.tasks.test_generative_build import _Meta, _track

JOB_ID = "11111111-1111-1111-1111-111111111111"
CLIP_PATHS = ["u/a.mp4", "u/b.mp4", "u/c.mp4"]
_SNAPSHOT = {
    "clip_assignments": [
        {"media_id": "c", "gcs_path": "u/c.mp4", "capture": {"capture_time": "2026-10-06T12:00Z"}},
        {"media_id": "a", "gcs_path": "u/a.mp4", "capture": {"capture_time": "2026-10-06T10:00Z"}},
        {"media_id": "b", "gcs_path": "u/b.mp4", "capture": {"capture_time": "2026-10-06T11:00Z"}},
    ]
}


def _assembly(strategy: dict[str, Any], *, order: bool = False) -> dict[str, Any]:
    contract = build_render_contract(
        strategy,
        generation_id="generation-1",
        media_snapshot=_SNAPSHOT if order else None,
    )
    assert contract is not None and not contract.unresolved
    return {
        CONTRACT_FIELD: contract.model_dump(mode="json"),
        "creator_brief_binding": {"media_snapshot": _SNAPSHOT},
    }


class _Probe:
    def __init__(self, duration_s: float, *, has_audio: bool = True) -> None:
        self.duration_s = duration_s
        self.has_audio = has_audio


def _stub_renderer(monkeypatch: pytest.MonkeyPatch, *, steps: list[Any], voice=None) -> None:
    """The ffmpeg/GCS edges of the montage render, with a faithful resolved-plans sink."""
    import app.pipeline.agents.gemini_analyzer as ga
    import app.pipeline.template_matcher as tm
    import app.storage as storage
    import app.tasks.template_orchestrate as to

    monkeypatch.setattr(
        ga,
        "build_recipe",
        lambda d: types.SimpleNamespace(
            beat_timestamps_s=d.get("beat_timestamps_s", []), color_grade="none"
        ),
        raising=False,
    )
    monkeypatch.setattr(tm, "consolidate_slots", lambda recipe, metas: recipe, raising=False)
    monkeypatch.setattr(
        tm, "match", lambda recipe, metas, **kw: types.SimpleNamespace(steps=steps), raising=False
    )

    class _Mismatch(Exception):
        code = "x"
        message = "y"

    monkeypatch.setattr(tm, "TemplateMismatchError", _Mismatch, raising=False)
    monkeypatch.setattr(to, "_enrich_slots_with_energy", lambda slots, beats: slots, raising=False)

    def _assemble(steps_, c2l, probe, out_path, tmpdir, **kw):
        for step in steps_:
            moment = step.moment or {}
            start = float(moment.get("start_s", 0.0))
            end = float(moment.get("end_s", start))
            kw["resolved_plans_out"].append(
                {"clip_id": step.clip_id, "start_s": start, "end_s": end, "duration_s": end - start}
            )
        with open(out_path, "wb") as f:
            f.write(b"\x00" * 16)

    monkeypatch.setattr(to, "_assemble_clips", _assemble, raising=False)

    def _mix_song(*a, **k):
        with open(a[2], "wb") as f:
            f.write(b"\x00" * 16)

    monkeypatch.setattr(to, "_mix_template_audio", _mix_song, raising=False)

    def _mix_voice(video, voice_path, out, tmpdir, **kw):
        with open(out, "wb") as f:
            f.write(b"\x00" * 16)
        return voice

    monkeypatch.setattr(to, "_mix_user_voiceover", _mix_voice, raising=False)
    monkeypatch.setattr(storage, "download_to_file", lambda gcs, local: None, raising=False)
    monkeypatch.setattr(to, "_probe_duration", lambda p: 6.0, raising=False)
    monkeypatch.setattr(
        storage, "upload_public_read", lambda local, gcs: f"https://signed/{gcs}", raising=False
    )
    monkeypatch.setattr(gb, "_rendered_duration_s", lambda path: 6.0)


def _step(clip_id: str, start: float, end: float) -> Any:
    return types.SimpleNamespace(
        clip_id=clip_id, slot={"position": 1}, moment={"start_s": start, "end_s": end}
    )


def _render(tmp_path, spec: dict[str, Any]) -> dict[str, Any]:
    vdir = tmp_path / spec["variant_id"]
    vdir.mkdir()
    return gb._render_generative_variant(
        job_id=JOB_ID,
        rank=1,
        spec={"rank": 1, "text_mode": "none", "track": None, **spec},
        clip_metas=[_Meta("c1", 5.0), _Meta("c2", 4.0), _Meta("c3", 3.0)],
        clip_id_to_local={"c1": "/a.mp4", "c2": "/b.mp4", "c3": "/c.mp4"},
        clip_id_to_gcs=dict(zip(["c1", "c2", "c3"], CLIP_PATHS, strict=True)),
        probe_map={
            "/a.mp4": _Probe(10.0),
            "/b.mp4": _Probe(8.0, has_audio=False),
            "/c.mp4": _Probe(9.0),
        },
        available_footage_s=18.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
    )


def _context(monkeypatch: pytest.MonkeyPatch, assembly: dict[str, Any], *, marked: bool = True):
    job = FakeJob(
        assembly_plan=assembly,
        all_candidates={REQUIREMENT_VERSION_FIELD: 1} if marked else {},
    )
    patch_job_session(monkeypatch, job)
    return job


_STEPS = [_step("c1", 0.0, 2.0), _step("c2", 0.0, 2.0), _step("c3", 0.0, 2.0)]
_VOICE_SPEC = {
    "variant_id": "voiceover_only",
    "text_mode": "agent_text",
    "archetype": "voiceover",
    "voiceover_gcs_path": "voiceover-uploads/abc/voice.webm",
    "mix": 1.0,
}


def _published(monkeypatch, assembly, result) -> dict[str, Any]:
    """Hand a rendered result to the real publication path and return what was stored."""
    job = _context(monkeypatch, assembly)
    gb._finalize_job(JOB_ID, [result])
    return job.assembly_plan["variants"][0]


# ── the regression: classic voiceover with require_voiceover ─────────────────────


def test_classic_voiceover_job_publishes_with_its_own_receipt(monkeypatch, tmp_path):
    assembly = _assembly({"audio_strategy": "voiceover"})
    _context(monkeypatch, assembly)
    _stub_renderer(
        monkeypatch, steps=_STEPS, voice=VoiceoverMixOutcome(applied=True, footage_audible=False)
    )
    ctx = gb._cloud_evidence_context(JOB_ID)
    assert ctx is not None
    result = _render(tmp_path, {**_VOICE_SPEC, "cloud_evidence": ctx})

    assert result["ok"] is True
    receipt = result["render_receipt"]
    assert receipt["narration_applied"] is True
    assert receipt["actual_duration_s"] == 6.0
    assert receipt["source_audio_ids"] == [] and receipt["source_audio_reason"] == (
        "replaced_by_narration"
    )
    stored = _published(monkeypatch, assembly, result)
    assert stored["render_status"] == "ready" and stored["video_path"]


def test_the_same_render_without_a_receipt_is_the_old_rejection(monkeypatch, tmp_path):
    """Control: what every classic voiceover job hit before renderers emitted evidence."""
    assembly = _assembly({"audio_strategy": "voiceover"})
    _context(monkeypatch, assembly)
    _stub_renderer(
        monkeypatch, steps=_STEPS, voice=VoiceoverMixOutcome(applied=True, footage_audible=False)
    )
    result = _render(tmp_path, _VOICE_SPEC)  # no cloud_evidence => legacy shape
    assert "render_receipt" not in result
    stored = _published(monkeypatch, assembly, result)
    assert stored["render_status"] == "failed"
    assert stored["error_class"] == "creator_render_contract_unverified"
    assert stored["decline_reason"] == "evidence_missing"


@pytest.mark.parametrize("outcome", [VoiceoverMixOutcome(False, True), None])
def test_a_failed_voice_mix_is_never_reported_as_narration(monkeypatch, tmp_path, outcome):
    """The mixer copies the video through on failure; intent must not become proof."""
    assembly = _assembly({"audio_strategy": "voiceover"})
    _context(monkeypatch, assembly)
    _stub_renderer(monkeypatch, steps=_STEPS, voice=outcome)
    result = _render(
        tmp_path, {**_VOICE_SPEC, "cloud_evidence": gb._cloud_evidence_context(JOB_ID)}
    )
    assert result["render_receipt"]["narration_applied"] is False
    stored = _published(monkeypatch, assembly, result)
    assert stored["render_status"] == "failed"
    assert stored["decline_reason"] == "evidence_missing"


# ── order and camera audio from the assembled timeline ───────────────────────────


def test_original_audio_variant_reports_order_and_the_clips_with_sound(monkeypatch, tmp_path):
    assembly = _assembly(
        {"ordering_choice": "chronological", "audio_strategy": "original_audio"}, order=True
    )
    _context(monkeypatch, assembly)
    ordered = [_step("c1", 0, 2), _step("c2", 0, 2), _step("c3", 0, 2)]  # a, b, c
    _stub_renderer(monkeypatch, steps=ordered)
    spec = {"variant_id": "original_text", "cloud_evidence": gb._cloud_evidence_context(JOB_ID)}
    result = _render(tmp_path, spec)

    receipt = result["render_receipt"]
    assert receipt["actual_clip_order"] == ["a", "b", "c"]
    # b has no audio stream, so only a and c are audible
    assert receipt["source_audio_ids"] == ["a", "c"] and receipt["source_audio_state"] == "audible"
    assert _published(monkeypatch, assembly, result)["render_status"] == "ready"


def test_an_out_of_order_cut_is_refused_with_its_evidence_in_the_variant(monkeypatch, tmp_path):
    assembly = _assembly({"ordering_choice": "chronological"}, order=True)
    _context(monkeypatch, assembly)
    shuffled = [_step("c3", 0, 2), _step("c1", 0, 2), _step("c2", 0, 2)]  # c, a, b
    _stub_renderer(monkeypatch, steps=shuffled)
    spec = {"variant_id": "original_text", "cloud_evidence": gb._cloud_evidence_context(JOB_ID)}
    result = _render(tmp_path, spec)
    assert result["render_receipt"]["actual_clip_order"] == ["c", "a", "b"]
    stored = _published(monkeypatch, assembly, result)
    assert stored["render_status"] == "failed"
    assert stored["decline_reason"] == "evidence_missing"
    assert stored["field_path"] == "ordering_choice"


def test_song_variant_replaces_camera_audio_and_satisfies_forbid_not_require(monkeypatch, tmp_path):
    track = _track()
    forbid = _assembly({"montage_audio": {"source_media_ids": [], "preserve_source_audio": False}})
    require = _assembly({"audio_strategy": "original_audio"})
    _context(monkeypatch, forbid)
    _stub_renderer(monkeypatch, steps=_STEPS)
    import app.pipeline.music_recipe as mr

    monkeypatch.setattr(
        mr,
        "generate_music_recipe",
        lambda td, **_kw: {
            "slots": [{"position": 1, "target_duration_s": 2.0, "text_overlays": []}],
            "beat_timestamps_s": [0.0, 2.0, 4.0, 6.0],
        },
        raising=False,
    )
    spec = {
        "variant_id": "song_text",
        "track": track,
        "cloud_evidence": gb._cloud_evidence_context(JOB_ID),
        "music_start_s": 0.0,
        "music_window_video_duration_s": 6.0,
    }
    result = _render(tmp_path, spec)
    receipt = result["render_receipt"]
    assert receipt["source_audio_ids"] == []
    assert receipt["source_audio_reason"] == "replaced_by_music"
    assert _published(monkeypatch, forbid, result)["render_status"] == "ready"
    assert _published(monkeypatch, require, result)["render_status"] == "failed"


# ── legacy byte-identity ─────────────────────────────────────────────────────────


def test_an_unmarked_job_gets_no_evidence_context_and_no_receipt(monkeypatch, tmp_path):
    job = FakeJob(assembly_plan={}, all_candidates={})
    patch_job_session(monkeypatch, job)
    assert gb._cloud_evidence_context(JOB_ID) is None
    assert gb._job_contract_bound(JOB_ID) is False
    _stub_renderer(monkeypatch, steps=_STEPS)
    result = _render(tmp_path, {"variant_id": "original_text"})
    assert "render_receipt" not in result


def test_a_marked_job_gets_the_approved_media_map(monkeypatch):
    _context(monkeypatch, _assembly({"audio_strategy": "voiceover"}))
    assert gb._cloud_evidence_context(JOB_ID) == {
        "media_ids_by_gcs": {"u/c.mp4": "c", "u/a.mp4": "a", "u/b.mp4": "b"}
    }


def test_a_receipt_never_lists_clips_the_snapshot_does_not_know(monkeypatch, tmp_path):
    """An unmapped path voids the order/audio claim instead of guessing a media id."""
    assembly = _assembly({"ordering_choice": "chronological"}, order=True)
    assembly["creator_brief_binding"] = {"media_snapshot": {"clip_assignments": []}}
    _context(monkeypatch, assembly)
    _stub_renderer(monkeypatch, steps=_STEPS)
    spec = {"variant_id": "original_text", "cloud_evidence": gb._cloud_evidence_context(JOB_ID)}
    receipt = _render(tmp_path, spec)["render_receipt"]
    assert "actual_clip_order" not in receipt and "source_audio_ids" not in receipt
    with pytest.raises(Exception) as exc:
        verify_cloud_variant(
            assembly,
            {"ok": True, "render_status": "ready", "video_path": "x", "render_receipt": receipt},
            candidates={REQUIREMENT_VERSION_FIELD: 1},
        )
    assert getattr(exc.value, "decline_reason", None) == "evidence_missing"
