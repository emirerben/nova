"""Focused task-adapter tests for speech-cleanup preflight."""

from __future__ import annotations

import signal
import subprocess
import uuid
import wave
from pathlib import Path
from unittest.mock import Mock

import pytest
from billiard.exceptions import SoftTimeLimitExceeded

from app.models import SpeechCleanupAnalysis
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    SpeechCleanupAnalysisResult,
)
from app.pipeline.transcribe import TranscribeError
from app.services.speech_cleanup_preflight import (
    SPEECH_CLEANUP_ENGINE_VERSION,
    SPEECH_CLEANUP_PAYLOAD_VERSION,
    ClaimedSpeechCleanupAnalysis,
    SpeechCleanupOperationalError,
)
from app.services.speech_cleanup_selection import DETECTOR_VERSION
from app.tasks import speech_cleanup_analysis as task_module


def _work(**overrides) -> task_module._ClaimedWork:
    values = {
        "claim": ClaimedSpeechCleanupAnalysis(
            analysis_id=uuid.uuid4(),
            attempt_token="attempt-1",
            source_policy_fingerprint="source-fingerprint",
        ),
        "source_storage_path": "users/u/voiceover.wav",
        "source_generation": "1700000000000000",
        "window_start_s": 1.25,
        "window_end_s": 3.25,
        "engine_version": SPEECH_CLEANUP_ENGINE_VERSION,
        "detector_version": DETECTOR_VERSION,
    }
    values.update(overrides)
    return task_module._ClaimedWork(**values)


def _result(*, candidate_count: int = 1) -> SpeechCleanupAnalysisResult:
    findings = (
        [
            {
                "start_s": 0.5,
                "end_s": 0.75,
                "reason": "filler_lexical",
                "category": "filler",
            }
        ]
        if candidate_count
        else []
    )
    return SpeechCleanupAnalysisResult.model_validate(
        {
            "source_fingerprint": "source-fingerprint",
            "detector_version": DETECTOR_VERSION,
            "source_window_start_s": 1.25,
            "source_window_end_s": 3.25,
            "language": "en",
            "timed_words": [],
            "cut_plan": {
                "keep_segments": (
                    [
                        {"start_s": 0.0, "end_s": 0.5},
                        {"start_s": 0.75, "end_s": 2.0},
                    ]
                    if findings
                    else [{"start_s": 0.0, "end_s": 2.0}]
                ),
                "removed": findings,
                "time_saved_s": 0.25 if findings else 0.0,
                "version": 2,
                "bailout_reason": None,
                "clamped": False,
            },
            "findings": findings,
            "safety_signals": {
                "transcript_low_confidence": False,
                "silence_detection_status": "ok",
                "selected_plan": "candidate",
                "candidate_status": "ready",
                "bailout_reason": None,
                "clamped": False,
            },
            "diagnostics": {"private": "not projected"},
            "public_receipt": {
                "candidate_count": len(findings),
                "category_counts": {
                    "filler_sounds": len(findings),
                    "long_pauses": 0,
                    "retakes": 0,
                },
                "estimated_removed_ms": 250 if findings else 0,
            },
        }
    )


def _write_pcm_wav(path: Path, duration_s: float) -> None:
    frame_count = round(duration_s * task_module.SPEECH_CLEANUP_SAMPLE_RATE_HZ)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(task_module.SPEECH_CLEANUP_CHANNELS)
        output.setsampwidth(task_module.SPEECH_CLEANUP_SAMPLE_WIDTH_BYTES)
        output.setframerate(task_module.SPEECH_CLEANUP_SAMPLE_RATE_HZ)
        output.writeframes(b"\0\0" * frame_count)


def test_extract_streams_exact_window_to_bounded_16khz_mono_pcm(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    work = _work()
    output_path = tmp_path / "bounded.wav"

    class Process:
        pid = 314
        returncode = 0

        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs

        def communicate(self, *, timeout):
            captured["timeout"] = timeout
            _write_pcm_wav(output_path, work.duration_s)
            return None, b""

    monkeypatch.setattr(task_module.subprocess, "Popen", Process)

    duration = task_module._extract_bounded_audio(
        signed_url="https://storage.invalid/object?secret=do-not-log",
        output_path=output_path,
        work=work,
    )

    command = captured["command"]
    assert command[command.index("-ss") + 1] == "1.250000"
    assert command[command.index("-t") + 1] == "2.000000"
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == "16000"
    assert command[command.index("-c:a") + 1] == "pcm_s16le"
    assert int(command[command.index("-fs") + 1]) == task_module._maximum_pcm_artifact_bytes(
        work.duration_s
    )
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] is subprocess.PIPE
    assert captured["timeout"] == task_module.SPEECH_CLEANUP_AUDIO_EXTRACTION_TIMEOUT_S
    assert duration == pytest.approx(work.duration_s)


def test_extraction_timeout_cleans_process_group_and_redacts_url(
    monkeypatch, tmp_path: Path
) -> None:
    cleaned: list[object] = []

    class Process:
        pid = 2718
        returncode = None

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, *, timeout):
            raise subprocess.TimeoutExpired(
                ["ffmpeg", "https://storage.invalid/object?token=top-secret"], timeout
            )

    monkeypatch.setattr(task_module.subprocess, "Popen", Process)
    monkeypatch.setattr(task_module, "_terminate_process_group", cleaned.append)

    with pytest.raises(SpeechCleanupOperationalError) as raised:
        task_module._extract_bounded_audio(
            signed_url="https://storage.invalid/object?token=top-secret",
            output_path=tmp_path / "never-written.wav",
            work=_work(),
        )

    assert raised.value.code == "audio_extraction_timeout"
    assert "top-secret" not in str(raised.value)
    assert "top-secret" not in str(raised.value.private_detail)
    assert len(cleaned) == 1


def test_process_group_cleanup_escalates_from_term_to_kill(monkeypatch) -> None:
    sent: list[tuple[int, signal.Signals]] = []

    class Process:
        pid = 99

        def __init__(self):
            self.waits = 0

        def wait(self, *, timeout):
            assert timeout == task_module.SPEECH_CLEANUP_PROCESS_KILL_GRACE_S
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired(["ffmpeg"], timeout)
            return -signal.SIGKILL

    monkeypatch.setattr(task_module.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    task_module._terminate_process_group(Process())

    assert sent == [(99, signal.SIGTERM), (99, signal.SIGKILL)]


def test_nonzero_extraction_maps_missing_audio_without_leaking_stderr(
    monkeypatch, tmp_path: Path
) -> None:
    secret = "signed-token-secret"

    class Process:
        pid = 12
        returncode = 1

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, *, timeout):
            return None, (
                f"https://storage.invalid/file?token={secret}: "
                "Stream map '0:a:0' matches no streams"
            ).encode()

    monkeypatch.setattr(task_module.subprocess, "Popen", Process)

    with pytest.raises(SpeechCleanupOperationalError) as raised:
        task_module._extract_bounded_audio(
            signed_url=f"https://storage.invalid/file?token={secret}",
            output_path=tmp_path / "never-written.wav",
            work=_work(),
        )

    assert raised.value.code == "no_speech_track"
    assert secret not in str(raised.value)
    assert secret not in str(raised.value.private_detail)


def test_analyze_work_signs_captured_generation_and_cleans_artifact(
    monkeypatch,
) -> None:
    events: list[tuple] = []
    local_paths: list[Path] = []
    expected = _result()
    work = _work()

    def sign(path, *, generation, expiration_minutes):
        events.append(("sign", path, generation, expiration_minutes))
        return "https://storage.invalid/generation-pinned"

    def extract(*, signed_url, output_path, work):
        events.append(("extract", signed_url, work.window_start_s, work.window_end_s))
        _write_pcm_wav(output_path, work.duration_s)
        return work.duration_s

    def run_engine(_work, local_path):
        local_paths.append(local_path)
        assert local_path.is_file()
        return expected

    monkeypatch.setattr(task_module, "signed_get_url_for_generation", sign)
    monkeypatch.setattr(task_module, "_extract_bounded_audio", extract)
    monkeypatch.setattr(task_module, "_run_engine", run_engine)

    assert task_module._analyze_work(work) is expected
    assert events == [
        (
            "sign",
            work.source_storage_path,
            work.source_generation,
            task_module.SPEECH_CLEANUP_SIGNED_URL_TTL_MINUTES,
        ),
        ("extract", "https://storage.invalid/generation-pinned", 1.25, 3.25),
    ]
    assert local_paths and not local_paths[0].exists()


def test_task_persists_typed_operational_failure(monkeypatch) -> None:
    work = _work()
    persisted: list[tuple[str, bool, str | None]] = []
    monkeypatch.setattr(task_module, "_claim_work", lambda _analysis_id: work)
    monkeypatch.setattr(
        task_module,
        "_analyze_work",
        lambda _work: (_ for _ in ()).throw(
            SpeechCleanupOperationalError("unsupported_media", detail="redacted")
        ),
    )

    def persist(_work, error):
        persisted.append((error.code, error.retryable, error.private_detail))
        return True

    monkeypatch.setattr(task_module, "_persist_failure", persist)

    result = task_module.analyze_speech_cleanup.run(str(work.claim.analysis_id))

    assert result == {"status": "failed", "error_code": "unsupported_media"}
    assert persisted == [("unsupported_media", False, "redacted")]


def test_soft_limit_persists_analysis_timeout(monkeypatch) -> None:
    work = _work()
    persisted: list[str] = []
    monkeypatch.setattr(task_module, "_claim_work", lambda _analysis_id: work)
    monkeypatch.setattr(
        task_module,
        "_analyze_work",
        lambda _work: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )
    monkeypatch.setattr(
        task_module,
        "_persist_failure",
        lambda _work, error: persisted.append(error.code) or True,
    )

    result = task_module.analyze_speech_cleanup.run(str(work.claim.analysis_id))

    assert result == {"status": "failed", "error_code": "analysis_timeout"}
    assert persisted == ["analysis_timeout"]


def test_unexpected_programming_error_is_reraised(monkeypatch) -> None:
    work = _work()
    persist = Mock()
    monkeypatch.setattr(task_module, "_claim_work", lambda _analysis_id: work)
    monkeypatch.setattr(
        task_module,
        "_analyze_work",
        lambda _work: (_ for _ in ()).throw(TypeError("bad adapter contract")),
    )
    monkeypatch.setattr(task_module, "_persist_failure", persist)

    with pytest.raises(TypeError, match="bad adapter contract"):
        task_module.analyze_speech_cleanup.run(str(work.claim.analysis_id))

    persist.assert_not_called()


def test_engine_adapter_maps_transcription_failure_but_reraises_programming_error(
    monkeypatch, tmp_path: Path
) -> None:
    work = _work()
    audio_path = tmp_path / "audio.wav"
    _write_pcm_wav(audio_path, work.duration_s)
    monkeypatch.setattr(
        task_module,
        "run_speech_cleanup_engine",
        lambda _input: (_ for _ in ()).throw(TranscribeError("provider secret")),
    )

    with pytest.raises(SpeechCleanupOperationalError) as operational:
        task_module._run_engine(work, audio_path)
    assert operational.value.code == "transcription_unavailable"
    assert "provider secret" not in str(operational.value)
    assert "provider secret" not in str(operational.value.private_detail)

    monkeypatch.setattr(
        task_module,
        "run_speech_cleanup_engine",
        lambda _input: (_ for _ in ()).throw(TypeError("broken contract")),
    )
    with pytest.raises(TypeError, match="broken contract"):
        task_module._run_engine(work, audio_path)


@pytest.mark.parametrize("configured_frac", [1.0, 0.55])
def test_engine_adapter_passes_the_configured_removal_cap(
    monkeypatch, tmp_path: Path, configured_frac: float
) -> None:
    """The preflight worker must analyze under the SAME removal cap the render
    path uses, or a rollback to a fraction cap would leave the persisted plan
    disagreeing with what gets rendered."""

    work = _work()
    audio_path = tmp_path / "audio.wav"
    _write_pcm_wav(audio_path, work.duration_s)
    monkeypatch.setattr(
        task_module.settings,
        "speech_cleanup_max_removal_frac_required",
        configured_frac,
        raising=False,
    )
    captured: dict[str, SpeechCleanupAnalysisInput] = {}

    def run_engine(analysis_input: SpeechCleanupAnalysisInput) -> SpeechCleanupAnalysisResult:
        captured["input"] = analysis_input
        return _result()

    monkeypatch.setattr(task_module, "run_speech_cleanup_engine", run_engine)

    task_module._run_engine(work, audio_path)

    assert captured["input"].max_removal_frac_required == pytest.approx(configured_frac)
    # The clamp policy itself stays a module constant; only the cap is tunable.
    assert captured["input"].over_budget_policy == "clamp"


@pytest.mark.parametrize("candidate_count, expected_status", [(1, "ready"), (0, "no_findings")])
def test_task_persists_success_state(
    monkeypatch, candidate_count: int, expected_status: str
) -> None:
    work = _work()
    result = _result(candidate_count=candidate_count)
    persist = Mock(return_value=True)
    monkeypatch.setattr(task_module, "_claim_work", lambda _analysis_id: work)
    monkeypatch.setattr(task_module, "_analyze_work", lambda _work: result)
    monkeypatch.setattr(task_module, "_persist_success", persist)

    assert task_module.analyze_speech_cleanup.run(str(work.claim.analysis_id)) == {
        "status": expected_status
    }
    persist.assert_called_once_with(work, result)


def test_success_persistence_splits_private_snapshot_from_bounded_receipt(monkeypatch) -> None:
    work = _work()
    result = _result()
    captured: dict[str, object] = {}

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def commit(self):
            captured["committed"] = True

    def finalize(_db, claim, **kwargs):
        captured["claim"] = claim
        captured.update(kwargs)
        return True

    monkeypatch.setattr(task_module, "sync_session", Session)
    monkeypatch.setattr(task_module, "finalize_analysis_success", finalize)

    assert task_module._persist_success(work, result) is True
    assert captured["claim"] == work.claim
    assert captured["committed"] is True
    assert captured["analysis_payload"] == result.to_payload()
    assert captured["candidate_count"] == 1
    assert captured["category_counts"] == {
        "filler_sounds": 1,
        "long_pauses": 0,
        "retakes": 0,
    }
    diagnostic_receipt = captured["diagnostic_receipt"]
    assert diagnostic_receipt == {
        "schema_version": 1,
        "selected_plan": "candidate",
        "candidate_status": "ready",
        "silence_detection_status": "ok",
        "transcript_low_confidence": False,
        "bailout": False,
        "clamped": False,
    }
    assert "timed_words" not in diagnostic_receipt
    assert "cut_plan" not in diagnostic_receipt


class _Session:
    def __init__(self, events: list[tuple]):
        self.events = events

    def __enter__(self):
        self.events.append(("session_enter",))
        return self

    def __exit__(self, *_args):
        self.events.append(("session_exit",))

    def commit(self):
        self.events.append(("commit",))


def test_reconciler_commits_bounded_claim_before_publish(monkeypatch) -> None:
    analysis_id = uuid.uuid4()
    events: list[tuple] = []
    monkeypatch.setattr(task_module, "sync_session", lambda: _Session(events))

    def claim(_db, *, limit):
        events.append(("claim", limit))
        return [analysis_id]

    def publish(*, args, queue):
        events.append(("publish", args, queue))

    monkeypatch.setattr(task_module, "claim_reconciliation_batch", claim)
    monkeypatch.setattr(task_module.analyze_speech_cleanup, "apply_async", publish)

    result = task_module.reconcile_speech_cleanup_analyses.run()

    assert result == {"claimed": 1, "published": 1, "publish_failed": 0}
    assert events.index(("commit",)) < events.index(
        ("publish", [str(analysis_id)], task_module.settings.speech_cleanup_analysis_queue)
    )
    assert ("claim", task_module.SPEECH_CLEANUP_RECONCILE_BATCH) in events


def test_reconciler_marks_broker_failure_retryable_and_continues(monkeypatch) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    events: list[tuple] = []
    monkeypatch.setattr(task_module, "sync_session", lambda: _Session(events))
    monkeypatch.setattr(
        task_module,
        "claim_reconciliation_batch",
        lambda _db, *, limit: [first, second],
    )

    def publish(*, args, queue):
        events.append(("publish", args, queue))
        if args == [str(first)]:
            raise ConnectionError("redis password must not be logged")

    retries: list[uuid.UUID] = []
    monkeypatch.setattr(task_module.analyze_speech_cleanup, "apply_async", publish)
    monkeypatch.setattr(task_module, "_mark_publish_retry", retries.append)

    result = task_module.reconcile_speech_cleanup_analyses.run()

    assert result == {"claimed": 2, "published": 1, "publish_failed": 1}
    assert retries == [first]
    assert [event[1] for event in events if event[0] == "publish"] == [
        [str(first)],
        [str(second)],
    ]


class _ClaimSession:
    """Minimal row-locked session for the real claim path."""

    def __init__(self, row: SpeechCleanupAnalysis) -> None:
        self.row = row
        self.flushed = 0
        self.committed = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, _model, identifier, **_kwargs):
        return self.row if identifier == self.row.id else None

    def flush(self):
        self.flushed += 1

    def commit(self):
        self.committed += 1


def _queued_row(detector_version: str) -> SpeechCleanupAnalysis:
    return SpeechCleanupAnalysis(
        id=uuid.uuid4(),
        plan_item_id=uuid.uuid4(),
        source_kind="voiceover",
        source_media_identity="voiceover-1",
        source_storage_path="users/u/voiceover.wav",
        source_generation="1700000000000000",
        window_start_s=1.25,
        window_end_s=3.25,
        source_policy_fingerprint="a" * 64,
        engine_version=SPEECH_CLEANUP_ENGINE_VERSION,
        detector_version=detector_version,
        analysis_payload_version=SPEECH_CLEANUP_PAYLOAD_VERSION,
        status="queued",
        attempt_count=0,
    )


def test_claim_restamps_a_pre_deploy_detector_label_onto_the_running_row(monkeypatch) -> None:
    """A row queued before a detector deploy is analyzed by the deployed code.

    The claimed label is what the persisted plan is stamped with, so it must
    describe the detector that actually produced that plan — never the version
    that happened to be current when the row was enqueued.
    """

    stale = "mixed-gap-v0-test-only"
    assert stale != DETECTOR_VERSION
    row = _queued_row(stale)
    session = _ClaimSession(row)
    monkeypatch.setattr(task_module, "sync_session", lambda: session)

    work = task_module._claim_work(str(row.id))

    assert work is not None
    assert row.status == "running"
    assert row.detector_version == DETECTOR_VERSION
    assert work.detector_version == DETECTOR_VERSION
    assert session.committed == 1


def test_engine_input_is_stamped_with_the_claimed_detector_label(monkeypatch) -> None:
    captured: list[SpeechCleanupAnalysisInput] = []

    def run_engine(analysis_input):
        captured.append(analysis_input)
        return _result()

    monkeypatch.setattr(task_module, "run_speech_cleanup_engine", run_engine)

    assert task_module._run_engine(_work(), Path("narration.wav")) is not None
    assert captured and captured[0].detector_version == DETECTOR_VERSION


def test_validate_work_fails_closed_on_a_detector_label_the_engine_will_not_produce() -> None:
    """Any unclaimed/foreign label must never reach the persisted plan."""

    work = _work(detector_version="mixed-gap-v0-test-only")

    with pytest.raises(SpeechCleanupOperationalError) as raised:
        task_module._validate_work(work)

    assert raised.value.code == "snapshot_mismatch"
    assert raised.value.private_detail == "detector_version"
    assert raised.value.retryable is False
