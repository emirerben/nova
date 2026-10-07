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

import shutil
import types
from typing import Any

import pytest

import app.tasks.generative_build as gb
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
    result = _render(tmp_path, {**_VOICE_SPEC, "cloud_evidence_ctx": ctx})

    assert result["ok"] is True
    assert "render_receipt" not in result  # evidence rides in a sibling key
    receipt = result["cloud_evidence"]
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
    assert "render_receipt" not in result and "cloud_evidence" not in result
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
        tmp_path, {**_VOICE_SPEC, "cloud_evidence_ctx": gb._cloud_evidence_context(JOB_ID)}
    )
    assert result["cloud_evidence"]["narration_applied"] is False
    stored = _published(monkeypatch, assembly, result)
    assert stored["render_status"] == "failed"
    assert stored["decline_reason"] == "evidence_missing"


# ── order and camera audio: reported by classic, but declined up front ───────────


def test_classic_reports_order_and_camera_audio_but_the_contract_is_declined(monkeypatch, tmp_path):
    """The receipt can describe the cut; only requirements the renderer HONOURS may be
    lifted, and the classic matcher/songs do not read order or audio_strategy."""
    assembly = _assembly(
        {"ordering_choice": "chronological", "audio_strategy": "original_audio"}, order=True
    )
    _context(monkeypatch, assembly)
    ordered = [_step("c1", 0, 2), _step("c2", 0, 2), _step("c3", 0, 2)]  # a, b, c
    _stub_renderer(monkeypatch, steps=ordered)
    spec = {"variant_id": "original_text", "cloud_evidence_ctx": gb._cloud_evidence_context(JOB_ID)}
    result = _render(tmp_path, spec)

    evidence = result["cloud_evidence"]
    assert evidence["actual_clip_order"] == ["a", "b", "c"]
    # b has no audio stream, so only a and c are audible
    assert evidence["source_audio_ids"] == ["a", "c"]
    assert evidence["source_audio_state"] == "audible"
    # slots are source-time windows: classic never claims per-clip output timing
    assert "picture_timeline" not in evidence
    stored = _published(monkeypatch, assembly, result)
    assert stored["render_status"] == "failed"
    assert stored["decline_reason"] == "capability_unavailable"


def test_an_out_of_order_classic_cut_reports_its_real_order(monkeypatch, tmp_path):
    assembly = _assembly({"ordering_choice": "chronological"}, order=True)
    _context(monkeypatch, assembly)
    shuffled = [_step("c3", 0, 2), _step("c1", 0, 2), _step("c2", 0, 2)]  # c, a, b
    _stub_renderer(monkeypatch, steps=shuffled)
    spec = {"variant_id": "original_text", "cloud_evidence_ctx": gb._cloud_evidence_context(JOB_ID)}
    result = _render(tmp_path, spec)
    assert result["cloud_evidence"]["actual_clip_order"] == ["c", "a", "b"]


def test_song_variant_evidence_says_the_song_replaced_camera_audio(monkeypatch, tmp_path):
    forbid = _assembly({"montage_audio": {"source_media_ids": [], "preserve_source_audio": False}})
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
        "track": _track(),
        "cloud_evidence_ctx": gb._cloud_evidence_context(JOB_ID),
        "music_start_s": 0.0,
        "music_window_video_duration_s": 6.0,
    }
    evidence = _render(tmp_path, spec)["cloud_evidence"]
    assert evidence["source_audio_ids"] == []
    assert evidence["source_audio_reason"] == "replaced_by_music"


# ── legacy byte-identity ─────────────────────────────────────────────────────────


def test_an_unmarked_job_gets_no_evidence_context_and_no_receipt(monkeypatch, tmp_path):
    job = FakeJob(assembly_plan={}, all_candidates={})
    patch_job_session(monkeypatch, job)
    assert gb._cloud_evidence_context(JOB_ID) is None
    assert gb._job_contract_bound(JOB_ID) is False
    _stub_renderer(monkeypatch, steps=_STEPS)
    result = _render(tmp_path, {"variant_id": "original_text"})
    assert "render_receipt" not in result and "cloud_evidence" not in result


def test_a_marked_job_gets_the_approved_media_map(monkeypatch):
    _context(monkeypatch, _assembly({"audio_strategy": "voiceover"}))
    assert gb._cloud_evidence_context(JOB_ID) == {
        "media_ids_by_gcs": {"u/c.mp4": "c", "u/a.mp4": "a", "u/b.mp4": "b"}
    }


def test_evidence_never_lists_clips_the_snapshot_does_not_know(monkeypatch, tmp_path):
    """An unmapped path voids the order/audio claim instead of guessing a media id."""
    assembly = _assembly({"ordering_choice": "chronological"}, order=True)
    assembly["creator_brief_binding"] = {"media_snapshot": {"clip_assignments": []}}
    _context(monkeypatch, assembly)
    _stub_renderer(monkeypatch, steps=_STEPS)
    spec = {"variant_id": "original_text", "cloud_evidence_ctx": gb._cloud_evidence_context(JOB_ID)}
    evidence = _render(tmp_path, spec)["cloud_evidence"]
    assert "actual_clip_order" not in evidence and "source_audio_ids" not in evidence


def test_a_failed_evidence_read_is_logged_not_silent(monkeypatch):
    seen: list[dict] = []

    class _Boom:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(gb, "_sync_session", lambda: _Boom())
    monkeypatch.setattr(
        gb.log, "warning", lambda event, **kw: seen.append({"event": event, **kw}), raising=False
    )
    assert gb._cloud_evidence_context(JOB_ID) is None
    assert seen and seen[0]["event"] == "cloud_evidence_context_unreadable"
    assert seen[0]["job_id"] == JOB_ID


# ── the pre-render guided plan gate, through the real worker entry ───────────────


def test_a_guided_plan_out_of_order_is_declined_before_any_render_spend(monkeypatch):
    from app.pipeline import guided_story
    from app.services.cloud_render_contract import CloudRenderContractError
    from tests.pipeline.test_guided_story import _guided_snapshot

    plan = guided_story.compile_execution_plan(_guided_snapshot(), track=None)
    timeline_order = ["food-photo", "town-photo", "coast-video"]
    assert plan["selected_media_ids"] == timeline_order
    contract = build_render_contract({"opening_title": "x"}, generation_id="g")
    contract = contract.rebind(
        order_ids=("coast-video", "food-photo", "town-photo"),  # capture order differs
        order_required=True,
        order_basis="capture_time",
    )
    job = FakeJob(
        assembly_plan={CONTRACT_FIELD: contract.model_dump(mode="json")},
        all_candidates={REQUIREMENT_VERSION_FIELD: 1},
    )
    patch_job_session(monkeypatch, job)

    def _spend(*_a, **_k):
        raise AssertionError("render spend happened before the plan gate")

    monkeypatch.setattr(gb, "_guided_execution_plan", lambda *_a, **_k: (plan, None))
    monkeypatch.setattr(gb, "record_phase", lambda *_a, **_k: None)
    monkeypatch.setattr(gb, "_claim_guided_story_attempt", _spend)
    monkeypatch.setattr(guided_story, "render_execution_plan", _spend)
    with pytest.raises(CloudRenderContractError) as exc:
        gb._run_guided_story_job(JOB_ID, {}, render_trace_id="t")
    assert (exc.value.decline_reason, exc.value.field_path) == (
        "capability_unavailable",
        "ordering_choice",
    )

    # the same plan against a contract that matches it proceeds to the claim
    ok = contract.rebind(order_ids=tuple(timeline_order))
    job.assembly_plan = {CONTRACT_FIELD: ok.model_dump(mode="json")}
    reached: list[str] = []
    monkeypatch.setattr(
        gb,
        "_claim_guided_story_attempt",
        lambda *_a, **_k: reached.append("claim") or ("rejected", None),
    )
    gb._run_guided_story_job(JOB_ID, {}, render_trace_id="t")
    assert reached == ["claim"]


# ── the real voiceover mixer's outcome (no mock) ─────────────────────────────────


def _ffmpeg(*args: str) -> None:
    import subprocess

    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture
def scratch_media(tmp_path):
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe not installed")
    video = tmp_path / "assembled.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=2:r=30",
        "-f", "lavfi", "-i", "sine=frequency=330:duration=2",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video),
    )  # fmt: skip
    voice = tmp_path / "voice.m4a"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:duration=2", "-c:a", "aac", str(voice))
    broken = tmp_path / "broken.m4a"
    broken.write_bytes(b"not audio at all")
    return video, voice, broken


def test_real_voiceover_mixer_reports_what_reached_the_file(scratch_media, tmp_path):
    from app.tasks.template_orchestrate import VoiceoverMixOutcome, _mix_user_voiceover

    video, voice, broken = scratch_media
    out = tmp_path / "out.mp4"
    full = _mix_user_voiceover(
        str(video), str(voice), str(out), str(tmp_path), mix=1.0, target_duration_s=2.0
    )
    assert full == VoiceoverMixOutcome(applied=True, footage_audible=False)
    under = _mix_user_voiceover(
        str(video), str(voice), str(out), str(tmp_path), mix=0.5, target_duration_s=2.0
    )
    assert under == VoiceoverMixOutcome(applied=True, footage_audible=True)
    failed = _mix_user_voiceover(
        str(video), str(broken), str(out), str(tmp_path), mix=1.0, target_duration_s=2.0
    )
    # the mixer copies the video through: the footage sound stays, the recording never lands
    assert failed == VoiceoverMixOutcome(applied=False, footage_audible=True)
    assert out.stat().st_size == video.stat().st_size


@pytest.mark.parametrize("broken", [False, True])
def test_classic_voiceover_regression_with_the_real_mixer(
    monkeypatch, tmp_path, scratch_media, broken
):
    video, voice, bad = scratch_media
    from app.tasks.template_orchestrate import _mix_user_voiceover

    real_mixer = _mix_user_voiceover  # captured before the renderer stubs replace it
    assembly = _assembly({"audio_strategy": "voiceover"})
    _context(monkeypatch, assembly)
    _stub_renderer(monkeypatch, steps=_STEPS)
    import app.storage as storage
    import app.tasks.template_orchestrate as to

    monkeypatch.setattr(to, "_mix_user_voiceover", real_mixer, raising=False)
    monkeypatch.setattr(to, "_probe_duration", lambda p: 2.0, raising=False)
    monkeypatch.setattr(
        storage,
        "download_to_file",
        lambda gcs, local: shutil.copyfile(bad if broken else voice, local),
        raising=False,
    )

    def _assemble(steps_, c2l, probe, out_path, tmpdir, **kw):
        for step in steps_:
            kw["resolved_plans_out"].append(
                {"clip_id": step.clip_id, "start_s": 0.0, "end_s": 2.0, "duration_s": 2.0}
            )
        shutil.copyfile(video, out_path)

    monkeypatch.setattr(to, "_assemble_clips", _assemble, raising=False)
    monkeypatch.setattr(gb, "_rendered_duration_s", lambda path: 2.0)
    result = _render(
        tmp_path, {**_VOICE_SPEC, "cloud_evidence_ctx": gb._cloud_evidence_context(JOB_ID)}
    )
    assert result["cloud_evidence"]["narration_applied"] is (not broken)
    stored = _published(monkeypatch, assembly, result)
    assert stored["render_status"] == ("failed" if broken else "ready")
