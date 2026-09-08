"""Release-blocking #960 proof for chat-first speech cleanup.

The fixture is intentionally generated at test time: four 440 Hz "spoken word"
tones surround two tokenless filler tones at 880/990 Hz.  The test crosses the
new worker boundary (bounded 16 kHz extraction), V2 analysis, decision dispatch,
immutable Job snapshot hydration, and the narrated renderer's real FFmpeg audio
application.  Whisper is deterministic at the preflight seam; after that seam
every analysis entry point is replaced with a counting hard failure.
"""

from __future__ import annotations

import array
import copy
import json
import math
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.pipeline.silence_cut as silence_cut
import app.pipeline.speech_cleanup_analysis as analysis_module
import app.pipeline.transcribe as transcribe_module
import app.services.clip_speech as clip_speech
import app.services.plan_item_media as plan_item_media
from app.pipeline.silence_cut import remap_words
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    SpeechCleanupAnalysisResult,
    analyze_speech_cleanup,
)
from app.pipeline.speech_cleanup_apply import (
    PREFLIGHT_JOB_CONTRACT_FIELD,
    PREFLIGHT_JOB_CONTRACT_VALUE,
    HydratedSpeechCleanupSnapshot,
    SpeechCleanupAudioApplyError,
    apply_speech_cleanup_to_audio,
    hydrate_job_speech_cleanup_snapshot,
)
from app.pipeline.transcribe import Transcript, Word
from app.services.active_narration_source import (
    ActiveNarrationRequest,
    ActiveNarrationResolution,
    RegisteredNarrationMedia,
    resolve_active_narration_source,
)
from app.services.speech_cleanup_identity import SpeechCleanupAssignment
from app.services.speech_cleanup_preflight import (
    SPEECH_CLEANUP_ENGINE_VERSION,
    ClaimedSpeechCleanupAnalysis,
    analysis_snapshot,
    public_projection,
)
from app.services.speech_cleanup_selection import DETECTOR_VERSION
from app.tasks import generative_build
from app.tasks import speech_cleanup_analysis as analysis_task
from app.tasks.content_plan_build import DispatchResult, _speech_cleanup_dispatch_snapshot

DURATION_S = 10.0
EXPECTED_ACOUSTIC_ISLANDS = (
    (5.778866, 6.209660),
    (7.406100, 7.977846),
)
INCIDENT_WORDS = (
    Word("başla", 1.214694, 1.757506, 0.99),
    Word("devam", 1.875034, 2.529025, 0.99),
    Word("konuşma", 3.255057, 4.358934, 0.99),
    Word("şimdi", 8.293356, 9.677755, 0.99),
)
_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@dataclass(frozen=True, slots=True)
class IncidentPreflight:
    source: Path
    bounded_audio: Path
    resolution_without_video: ActiveNarrationResolution
    resolution_with_video: ActiveNarrationResolution
    result: SpeechCleanupAnalysisResult


class _AnalysisSession:
    def __init__(self, row: SimpleNamespace) -> None:
        self.row = row

    def get(self, _model: object, identifier: uuid.UUID, **_kwargs: object) -> object:
        assert identifier == self.row.id
        return self.row


def _run(command: list[str], *, timeout: int = 120) -> None:
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def _make_synthetic_960_source(path: Path) -> None:
    real_word_gate = "+".join(
        f"between(t\\,{start:.6f}\\,{end:.6f})"
        for start, end in (
            (1.214694, 1.757506),
            (1.875034, 2.529025),
            (3.255057, 4.358934),
            (8.293356, 9.677755),
        )
    )
    audio = (
        f"aevalsrc=0.3*sin(2*PI*440*t)*({real_word_gate})"
        "+0.3*sin(2*PI*880*t)*between(t\\,5.778866\\,6.209660)"
        "+0.3*sin(2*PI*990*t)*between(t\\,7.406100\\,7.977846)"
        ":s=48000:d=10"
    )
    _run(
        [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x284:r=10:d=10",
            "-f",
            "lavfi",
            "-i",
            audio,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "pcm_s16le",
            "-shortest",
            str(path),
        ]
    )


def _decoded_pcm(path: Path) -> array.array[float]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "f32le",
            "-",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    samples = array.array("f")
    samples.frombytes(result.stdout)
    return samples


def _tone_amplitude(samples: array.array[float], frequency_hz: float) -> float:
    coefficient = 2.0 * math.cos(2.0 * math.pi * frequency_hz / 8000.0)
    previous = 0.0
    previous_two = 0.0
    for sample in samples:
        current = sample + coefficient * previous - previous_two
        previous_two = previous
        previous = current
    power = previous**2 + previous_two**2 - coefficient * previous * previous_two
    return math.sqrt(max(0.0, power)) / max(1, len(samples))


def _duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(path),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return float(result.stdout.decode().strip())


def _voiceover_request(*, with_video: bool) -> ActiveNarrationRequest:
    voiceover = RegisteredNarrationMedia(
        media_id="voiceover-960",
        storage_path="voiceovers/fixture-960.wav",
        generation="1700000000000960",
        media_kind="audio",
        duration_s=DURATION_S,
        has_audio=True,
        manifest_identity="fixture-960-generation",
    )
    clips = (
        (
            RegisteredNarrationMedia(
                media_id="visual-only",
                storage_path="clips/visual-only.mp4",
                generation="1700000000000961",
                media_kind="video",
                duration_s=DURATION_S,
                has_audio=False,
            ),
        )
        if with_video
        else ()
    )
    return ActiveNarrationRequest(
        edit_format="narrated",
        audio_mode="voiceover",
        detector_policy=plan_item_media.current_detector_policy(),
        clips=clips,
        voiceover=voiceover,
    )


@pytest.fixture(scope="module")
def incident_preflight(tmp_path_factory: pytest.TempPathFactory) -> IncidentPreflight:
    if not _HAS_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not installed")

    fixture_dir = tmp_path_factory.mktemp("chat-speech-cleanup-960")
    source = fixture_dir / "mixed-gap-960.mov"
    bounded_audio = fixture_dir / "mixed-gap-960-bounded.wav"
    _make_synthetic_960_source(source)

    without_video = resolve_active_narration_source(_voiceover_request(with_video=False))
    with_video = resolve_active_narration_source(_voiceover_request(with_video=True))
    assert without_video.source is not None
    assert with_video.source is not None
    assert without_video.video_present is False
    assert with_video.video_present is True
    assert (
        without_video.source.source_policy_fingerprint
        == with_video.source.source_policy_fingerprint
    )

    claim = ClaimedSpeechCleanupAnalysis(
        analysis_id=uuid.uuid4(),
        attempt_token="fixture-attempt",
        source_policy_fingerprint=without_video.source.source_policy_fingerprint,
    )
    work = analysis_task._ClaimedWork(
        claim=claim,
        source_storage_path=without_video.source.storage_path,
        source_generation=without_video.source.generation,
        window_start_s=0.0,
        window_end_s=DURATION_S,
        engine_version=SPEECH_CLEANUP_ENGINE_VERSION,
        detector_version=DETECTOR_VERSION,
    )
    extracted_duration = analysis_task._extract_bounded_audio(
        signed_url=str(source),
        output_path=bounded_audio,
        work=work,
    )
    assert extracted_duration == pytest.approx(DURATION_S)

    def deterministic_whisper(
        path: str,
        *,
        language: str | None,
        verbatim_prompt: str | None,
    ) -> Transcript:
        assert Path(path) == bounded_audio
        assert language is None
        assert verbatim_prompt and "Uh, um" in verbatim_prompt
        return Transcript(
            words=list(INCIDENT_WORDS),
            full_text="başla devam konuşma şimdi",
            language="tr",
        )

    def real_silence_detect(path: str, *, min_silence_s: float) -> object:
        return clip_speech.detect_silences_with_status(
            path,
            noise_db=-30.0,
            min_silence_s=min_silence_s,
        )

    def engine_adapter(request: SpeechCleanupAnalysisInput) -> SpeechCleanupAnalysisResult:
        return analyze_speech_cleanup(
            request,
            transcribe_fn=deterministic_whisper,
            silence_detect_fn=real_silence_detect,
        )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(analysis_task, "run_speech_cleanup_engine", engine_adapter)
        result = analysis_task._run_engine(work, bounded_audio)

    acoustic = [finding for finding in result.findings if finding.reason == "filler_acoustic"]
    assert len(acoustic) == len(EXPECTED_ACOUSTIC_ISLANDS)
    for finding, expected in zip(acoustic, EXPECTED_ACOUSTIC_ISLANDS, strict=True):
        assert (finding.start_s, finding.end_s) == pytest.approx(expected, abs=5e-5)
    assert result.safety_signals.selected_plan == "candidate"
    assert result.public_receipt.category_counts.filler_sounds == 2
    return IncidentPreflight(
        source=source,
        bounded_audio=bounded_audio,
        resolution_without_video=without_video,
        resolution_with_video=with_video,
        result=result,
    )


def _analysis_row(preflight: IncidentPreflight, *, plan_item_id: uuid.UUID) -> SimpleNamespace:
    source = preflight.resolution_without_video.source
    assert source is not None
    receipt = preflight.result.public_receipt
    return SimpleNamespace(
        id=uuid.uuid4(),
        plan_item_id=plan_item_id,
        status="ready",
        superseded_at=None,
        source_policy_fingerprint=source.source_policy_fingerprint,
        source_kind=source.source_kind,
        source_media_identity=source.media_id,
        source_storage_path=source.storage_path,
        source_generation=source.generation,
        window_start_s=source.window_start_s,
        window_end_s=source.window_end_s,
        engine_version=SPEECH_CLEANUP_ENGINE_VERSION,
        detector_version=DETECTOR_VERSION,
        analysis_payload=preflight.result.to_payload(),
        candidate_count=receipt.candidate_count,
        category_counts=receipt.category_counts.model_dump(mode="json"),
        estimated_removed_ms=receipt.estimated_removed_ms,
        failure_code=None,
        failure_retryable=None,
        decision=None,
        decision_at=None,
    )


def _dispatch(
    monkeypatch: pytest.MonkeyPatch,
    preflight: IncidentPreflight,
    *,
    choice: str,
) -> tuple[SimpleNamespace, str, dict[str, Any], dict[str, Any] | None]:
    item_id = uuid.uuid4()
    row = _analysis_row(preflight, plan_item_id=item_id)
    item = SimpleNamespace(
        id=item_id,
        clip_gcs_paths=["clips/visual-only.mp4"],
        speech_cleanup_enabled=False,
    )
    monkeypatch.setattr(
        plan_item_media,
        "resolve_item_narration",
        lambda _item, *, detector_policy: preflight.resolution_with_video,
    )
    card = public_projection(
        row,
        applicable=True,
        unavailable_reason=None,
        video_present=True,
    )
    assert card is not None
    assert card["analysis"] == {
        "id": str(row.id),
        "status": "ready",
        "detector_version": DETECTOR_VERSION,
        "has_findings": True,
        "candidate_count": preflight.result.public_receipt.candidate_count,
        "category_counts": {"filler_sounds": 2, "long_pauses": 3},
        "estimated_removed_ms": preflight.result.public_receipt.estimated_removed_ms,
        # The column-derived projection must agree with the payload receipt.
        "source_duration_ms": preflight.result.public_receipt.source_duration_ms,
        "result_duration_ms": preflight.result.public_receipt.result_duration_ms,
        "error": None,
    }
    assert card["requires_choice"] is True
    public_json = json.dumps(card, sort_keys=True)
    assert "başla" not in public_json
    assert "start_s" not in public_json
    assert preflight.resolution_without_video.source is not None
    assert preflight.resolution_without_video.source.storage_path not in public_json
    dispatched = _speech_cleanup_dispatch_snapshot(
        _AnalysisSession(row),
        item,
        analysis_id=str(row.id),
        choice=choice,
    )
    assert not isinstance(dispatched, DispatchResult)
    contract, snapshot, outcome = dispatched
    assert contract in {"required_v1", "off_v1"}
    assert snapshot is not None
    assert item.speech_cleanup_enabled is (choice == "clean")
    assert row.decision == choice
    return row, contract, snapshot, outcome


def _job_snapshot(
    contract: str,
    snapshot: dict[str, Any],
) -> HydratedSpeechCleanupSnapshot:
    return hydrate_job_speech_cleanup_snapshot(
        {
            "speech_cleanup_contract": contract,
            PREFLIGHT_JOB_CONTRACT_FIELD: PREFLIGHT_JOB_CONTRACT_VALUE,
            "_speech_cleanup_internal": {"preflight_snapshot": snapshot},
        }
    )


def _install_render_analysis_tripwires(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, int]:
    calls = {"whisper": 0, "silencedetect": 0, "mixed_gap": 0, "retake": 0}

    def hard_failure(name: str):
        def fail(*_args: object, **_kwargs: object) -> object:
            calls[name] += 1
            raise AssertionError(f"render attempted forbidden {name} analysis")

        return fail

    monkeypatch.setattr(transcribe_module, "transcribe_whisper", hard_failure("whisper"))
    monkeypatch.setattr(
        clip_speech,
        "detect_silences_with_status",
        hard_failure("silencedetect"),
    )
    monkeypatch.setattr(
        silence_cut,
        "build_cut_plan_comparison",
        hard_failure("mixed_gap"),
    )
    monkeypatch.setattr(
        analysis_module,
        "build_cut_plan_comparison",
        hard_failure("mixed_gap"),
    )
    monkeypatch.setattr(
        generative_build,
        "_silence_cut_analysis",
        hard_failure("mixed_gap"),
    )
    monkeypatch.setattr(
        generative_build,
        "_silence_cut_retake_spans",
        hard_failure("retake"),
    )
    return calls


def _patch_narrated_io(
    monkeypatch: pytest.MonkeyPatch,
    preflight: IncidentPreflight,
    captured: dict[str, Any],
) -> None:
    import app.pipeline.narrated_assembler as narrated_assembler
    import app.storage as storage

    def download_to_file(storage_path: str, local_path: str) -> None:
        source = preflight.resolution_without_video.source
        assert source is not None
        assert storage_path == source.storage_path
        shutil.copyfile(preflight.bounded_audio, local_path)

    def download_generation_to_file(
        storage_path: str,
        local_path: str,
        *,
        generation: str,
    ) -> None:
        source = preflight.resolution_without_video.source
        assert source is not None
        assert storage_path == source.storage_path
        assert generation == source.generation
        captured["download_generation"] = generation
        shutil.copyfile(preflight.bounded_audio, local_path)

    def upload_public_read(local_path: str, storage_path: str) -> str:
        assert Path(local_path).is_file()
        captured.setdefault("uploads", []).append(storage_path)
        return f"https://storage.invalid/{storage_path}"

    def assemble_narrated(*args: object, **kwargs: object) -> list[dict[str, object]]:
        voiceover_path = Path(str(args[2]))
        output_path = Path(str(args[3]))
        voiceover_duration = _duration(voiceover_path)
        transcript = kwargs["transcript"]
        assert isinstance(transcript, Transcript)
        captured["voiceover_path"] = voiceover_path
        captured["transcript"] = transcript
        captured["output_path"] = output_path
        _run(
            [
                "ffmpeg",
                "-y",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s=160x284:r=10:d={voiceover_duration:.6f}",
                "-i",
                str(voiceover_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                "-t",
                f"{voiceover_duration:.6f}",
                str(output_path),
            ]
        )
        base_output = kwargs.get("base_output_path")
        if isinstance(base_output, str):
            shutil.copyfile(output_path, base_output)
        return [
            {"text": word.text, "start_s": word.start_s, "end_s": word.end_s}
            for word in transcript.words
        ]

    monkeypatch.setattr(storage, "download_to_file", download_to_file)
    monkeypatch.setattr(
        storage,
        "download_generation_to_file",
        download_generation_to_file,
    )
    monkeypatch.setattr(storage, "upload_public_read", upload_public_read)
    monkeypatch.setattr(narrated_assembler, "assemble_narrated", assemble_narrated)


@pytest.mark.parametrize(
    ("choice", "expected_contract", "expected_outcome"),
    [
        ("clean", "required_v1", None),
        (
            "keep_original",
            "off_v1",
            {"status": "declined", "removal_count": 0, "removed_ms": 0},
        ),
    ],
)
def test_960_clean_and_keep_snapshot_render_make_zero_analysis_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    incident_preflight: IncidentPreflight,
    choice: str,
    expected_contract: str,
    expected_outcome: dict[str, object] | None,
) -> None:
    row, contract, raw_snapshot, outcome = _dispatch(
        monkeypatch,
        incident_preflight,
        choice=choice,
    )
    assert contract == expected_contract
    assert outcome == expected_outcome
    assert raw_snapshot["analysis_id"] == str(row.id)
    assert raw_snapshot["source"]["source_policy_fingerprint"] == (row.source_policy_fingerprint)
    assert raw_snapshot["engine_version"] == SPEECH_CLEANUP_ENGINE_VERSION
    assert raw_snapshot["detector_version"] == DETECTOR_VERSION
    assert (
        raw_snapshot["analysis"]["cut_plan"] == (incident_preflight.result.to_payload()["cut_plan"])
    )

    forbidden_calls = _install_render_analysis_tripwires(monkeypatch)
    snapshot = _job_snapshot(contract, raw_snapshot)
    assert snapshot.analysis_id == str(row.id)
    assert snapshot.analysis == incident_preflight.result

    captured: dict[str, Any] = {}
    _patch_narrated_io(monkeypatch, incident_preflight, captured)
    variant_dir = tmp_path / choice
    variant_dir.mkdir()
    render = generative_build._render_narrated_variant(
        job_id=f"job-{choice}",
        rank=0,
        spec={
            "variant_id": "narrated_960",
            "voiceover_gcs_path": row.source_storage_path,
            "storage_generation": "render-generation-1",
        },
        filming_guide=[
            {"shot_id": "one", "what": "başla devam"},
            {"shot_id": "two", "what": "konuşma şimdi"},
        ],
        narrative_order=["one", "two"],
        clip_id_to_local={"one": str(incident_preflight.source), "two": "visual.mp4"},
        variant_dir=str(variant_dir),
        speech_cleanup_contract=contract,
        speech_cleanup_snapshot=snapshot,
    )

    assert render["ok"] is True, render
    assert forbidden_calls == {
        "whisper": 0,
        "silencedetect": 0,
        "mixed_gap": 0,
        "retake": 0,
    }
    assert captured["download_generation"] == row.source_generation
    rendered_path = Path(captured["output_path"])
    source_pcm = _decoded_pcm(incident_preflight.bounded_audio)
    rendered_pcm = _decoded_pcm(rendered_path)
    transcript = captured["transcript"]
    assert isinstance(transcript, Transcript)

    if choice == "clean":
        kept_s = sum(end - start for start, end in snapshot.cut_plan.keep_segments)
        assert _duration(rendered_path) == pytest.approx(kept_s, abs=0.08)
        assert Path(captured["voiceover_path"]).name == "voiceover_cleaned.wav"
        expected_words = remap_words(list(INCIDENT_WORDS), snapshot.cut_plan)
        assert [word.text for word in transcript.words] == [word["text"] for word in expected_words]
        for word, expected in zip(transcript.words, expected_words, strict=True):
            assert (word.start_s, word.end_s) == pytest.approx(
                (expected["start_s"], expected["end_s"])
            )
        for filler_hz in (880.0, 990.0):
            assert _tone_amplitude(rendered_pcm, filler_hz) < 0.03 * _tone_amplitude(
                source_pcm,
                filler_hz,
            )
        context = render["_speech_cleanup_outcome_context"]
        assert context["analysis_attempt_id"] == str(row.id)
        assert context["output_removal_count"] == len(snapshot.cut_plan.removed)
        assert context["output_removed_ms"] == round(snapshot.cut_plan.time_saved_s * 1000)
        assert render["silence_cut_outcome"] == "applied"
    else:
        assert _duration(rendered_path) == pytest.approx(DURATION_S, abs=0.08)
        assert Path(captured["voiceover_path"]).name == "voiceover_src"
        assert [(word.text, word.start_s, word.end_s) for word in transcript.words] == [
            (word.text, word.start_s, word.end_s) for word in INCIDENT_WORDS
        ]
        for filler_hz in (880.0, 990.0):
            assert _tone_amplitude(rendered_pcm, filler_hz) > 0.4 * _tone_amplitude(
                source_pcm,
                filler_hz,
            )
        assert "_speech_cleanup_outcome_context" not in render
        assert render["silence_cut_outcome"] is None

    assert _tone_amplitude(rendered_pcm, 440.0) > 0.2 * _tone_amplitude(source_pcm, 440.0)


def test_transient_application_retry_reuses_exact_snapshot_and_never_reanalyzes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    incident_preflight: IncidentPreflight,
) -> None:
    row, contract, raw_snapshot, _outcome = _dispatch(
        monkeypatch,
        incident_preflight,
        choice="clean",
    )
    failed_job_snapshot = copy.deepcopy(raw_snapshot)
    forbidden_calls = _install_render_analysis_tripwires(monkeypatch)
    captured: dict[str, Any] = {}
    _patch_narrated_io(monkeypatch, incident_preflight, captured)
    failed_dir = tmp_path / "failed-application"
    failed_dir.mkdir()

    def fail_apply(*_args: object, **_kwargs: object) -> str:
        raise SpeechCleanupAudioApplyError("transient ffmpeg failure")

    monkeypatch.setattr(generative_build, "apply_speech_cleanup_to_audio", fail_apply)
    failed = generative_build._render_narrated_variant(
        job_id="job-failed-application",
        rank=0,
        spec={
            "variant_id": "narrated_960",
            "voiceover_gcs_path": row.source_storage_path,
            "storage_generation": "failed-generation",
        },
        filming_guide=[
            {"shot_id": "one", "what": "başla devam"},
            {"shot_id": "two", "what": "konuşma şimdi"},
        ],
        narrative_order=["one", "two"],
        clip_id_to_local={"one": str(incident_preflight.source), "two": "visual.mp4"},
        variant_dir=str(failed_dir),
        speech_cleanup_contract=contract,
        speech_cleanup_snapshot=_job_snapshot(contract, failed_job_snapshot),
    )

    assert failed["ok"] is False
    assert failed["error_class"] == "speech_cleanup_failed"
    assert failed["speech_cleanup_failure_reason"] == "apply_failed"
    assert forbidden_calls == {
        "whisper": 0,
        "silencedetect": 0,
        "mixed_gap": 0,
        "retake": 0,
    }

    retry_job_snapshot = copy.deepcopy(failed_job_snapshot)
    retry_snapshot = _job_snapshot(contract, retry_job_snapshot)
    assert retry_job_snapshot == failed_job_snapshot
    assert retry_job_snapshot is not failed_job_snapshot
    assert retry_snapshot.analysis_id == str(row.id)
    assert retry_snapshot.cut_plan == _job_snapshot(contract, failed_job_snapshot).cut_plan
    monkeypatch.setattr(
        generative_build,
        "apply_speech_cleanup_to_audio",
        apply_speech_cleanup_to_audio,
    )
    retry_dir = tmp_path / "retry-application"
    retry_dir.mkdir()
    recovered = generative_build._render_narrated_variant(
        job_id="job-retry-application",
        rank=0,
        spec={
            "variant_id": "narrated_960",
            "voiceover_gcs_path": row.source_storage_path,
            "storage_generation": "retry-generation",
        },
        filming_guide=[
            {"shot_id": "one", "what": "başla devam"},
            {"shot_id": "two", "what": "konuşma şimdi"},
        ],
        narrative_order=["one", "two"],
        clip_id_to_local={"one": str(incident_preflight.source), "two": "visual.mp4"},
        variant_dir=str(retry_dir),
        speech_cleanup_contract=contract,
        speech_cleanup_snapshot=retry_snapshot,
    )

    assert recovered["ok"] is True, recovered
    assert recovered["silence_cut_outcome"] == "applied"
    assert forbidden_calls == {
        "whisper": 0,
        "silencedetect": 0,
        "mixed_gap": 0,
        "retake": 0,
    }
    source_pcm = _decoded_pcm(incident_preflight.bounded_audio)
    rendered_pcm = _decoded_pcm(Path(captured["output_path"]))
    for filler_hz in (880.0, 990.0):
        assert _tone_amplitude(rendered_pcm, filler_hz) < 0.03 * _tone_amplitude(
            source_pcm,
            filler_hz,
        )


@pytest.mark.parametrize("choice", ["clean", "keep_original"])
def test_audio_only_analysis_survives_later_video_attachment(
    monkeypatch: pytest.MonkeyPatch,
    incident_preflight: IncidentPreflight,
    choice: str,
) -> None:
    without_video = incident_preflight.resolution_without_video
    with_video = incident_preflight.resolution_with_video
    assert without_video.source is not None
    assert with_video.source is not None
    assert without_video.source.source_policy_fingerprint == (
        with_video.source.source_policy_fingerprint
    )

    item_id = uuid.uuid4()
    row = _analysis_row(incident_preflight, plan_item_id=item_id)
    item = SimpleNamespace(
        id=item_id,
        clip_gcs_paths=[],
        speech_cleanup_enabled=False,
    )

    card = public_projection(
        row,
        applicable=True,
        unavailable_reason=None,
        video_present=False,
    )
    assert card is not None
    assert card["analysis"]["id"] == str(row.id)
    assert card["analysis"]["has_findings"] is True
    assert card["requires_choice"] is False
    assert card["render_blocker"] == "video_required"

    monkeypatch.setattr(
        plan_item_media,
        "resolve_item_narration",
        lambda _item, *, detector_policy: with_video if _item.clip_gcs_paths else without_video,
    )
    blocked = _speech_cleanup_dispatch_snapshot(
        _AnalysisSession(row),
        item,
        analysis_id=str(row.id),
        choice=choice,
    )
    assert blocked == DispatchResult("video_required")
    assert row.decision is None
    assert row.decision_at is None

    item.clip_gcs_paths = ["clips/visual-only.mp4"]
    dispatched = _speech_cleanup_dispatch_snapshot(
        _AnalysisSession(row),
        item,
        analysis_id=str(row.id),
        choice=choice,
    )
    assert not isinstance(dispatched, DispatchResult)
    contract, snapshot, outcome = dispatched
    assert contract == ("required_v1" if choice == "clean" else "off_v1")
    assert snapshot is not None
    assert snapshot["analysis_id"] == str(row.id)
    assert snapshot["source"]["source_policy_fingerprint"] == (
        without_video.source.source_policy_fingerprint
    )
    assert row.decision == choice
    if choice == "keep_original":
        assert outcome == {"status": "declined", "removal_count": 0, "removed_ms": 0}
    else:
        assert outcome is None


def test_embedded_snapshot_binding_survives_durable_path_and_clip_id_rewrites(
    incident_preflight: IncidentPreflight,
) -> None:
    row = _analysis_row(incident_preflight, plan_item_id=uuid.uuid4())
    raw_snapshot = analysis_snapshot(row)
    raw_snapshot["source"] = {
        **raw_snapshot["source"],
        "kind": "embedded_spine",
        "media_identity": "registered-spine",
        "storage_path": "uploads/original-spine.mp4",
    }
    source_instance_id = "00000000-0000-4000-8000-000000000002"
    snapshot = hydrate_job_speech_cleanup_snapshot(
        {
            "speech_cleanup_contract": "required_v1",
            PREFLIGHT_JOB_CONTRACT_FIELD: PREFLIGHT_JOB_CONTRACT_VALUE,
            "_speech_cleanup_internal": {
                "preflight_snapshot": raw_snapshot,
                "source_binding": {
                    # The source was at slot zero when the Job snapshot was
                    # minted; a later visual-only reorder moved the same UUID.
                    "source_slot": 0,
                    "source_instance_id": source_instance_id,
                },
            },
        }
    )
    current_identity = SimpleNamespace(
        valid=True,
        source_instance_ids=(
            "00000000-0000-4000-8000-000000000001",
            source_instance_id,
        ),
    )

    clip_id = generative_build._speech_cleanup_snapshot_clip_id(
        snapshot,
        original_clip_paths=(
            "generative-jobs/job/sources/slot-0000.mp4",
            "generative-jobs/job/sources/slot-0001.mp4",
        ),
        assignment_by_clip_id={
            "gemini-file-renamed": SpeechCleanupAssignment(1, "rollout", "assigned")
        },
        source_identity=current_identity,
    )

    assert clip_id == "gemini-file-renamed"
