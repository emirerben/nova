"""Focused task-adapter tests for speech-cleanup preflight."""

from __future__ import annotations

import signal
import subprocess
import uuid
import wave
from pathlib import Path
from types import SimpleNamespace
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


@pytest.fixture(autouse=True)
def _no_gemini_reference(monkeypatch) -> None:
    """Unit tests never reach Gemini; the second-opinion tests opt back in."""
    monkeypatch.setattr(task_module.settings, "speech_cleanup_gemini_reference_enabled", False)


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

    def __init__(self, row: SpeechCleanupAnalysis, *related) -> None:
        self.row = row
        self.related = {value.id: value for value in related}
        self.flushed = 0
        self.committed = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, _model, identifier, **_kwargs):
        return self.row if identifier == self.row.id else self.related.get(identifier)

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


# ── Gemini reference transcript (language cross-check) ──────────────────────
# The engine behavior itself is pinned in
# tests/services/test_speech_cleanup_language_crosscheck.py.

_GEMINI_EN = "So today we built the thing, and here is how it went for everyone."


def _misheard_result(language: str) -> SpeechCleanupAnalysisResult:
    return _result(candidate_count=0).model_copy(update={"language": language})


def test_reference_transcript_is_read_for_clip_audio_only(monkeypatch) -> None:
    row = _queued_row(DETECTOR_VERSION)
    item = object()
    db = Mock()
    db.get.return_value = item
    lookup = Mock(return_value=_GEMINI_EN)
    monkeypatch.setattr(task_module, "reference_transcript_for_source", lookup)

    assert task_module._reference_transcript(db, row) is None  # a voiceover row
    db.get.assert_not_called()

    row.source_kind = "embedded_spine"
    assert task_module._reference_transcript(db, row) == _GEMINI_EN
    lookup.assert_called_once_with(
        item, storage_path=row.source_storage_path, generation=row.source_generation
    )


def test_engine_receives_the_reference_read_at_claim(monkeypatch) -> None:
    captured: list[SpeechCleanupAnalysisInput] = []
    monkeypatch.setattr(
        task_module,
        "run_speech_cleanup_engine",
        lambda analysis_input: captured.append(analysis_input) or _misheard_result("tr"),
    )
    late = Mock()
    monkeypatch.setattr(task_module, "_late_reference_transcript", late)

    work = _work(source_kind="embedded_spine", reference_transcript=_GEMINI_EN)
    task_module._run_engine(work, Path("narration.wav"))

    assert [value.reference_transcript for value in captured] == [_GEMINI_EN]
    late.assert_not_called()  # the engine already had its reference


def test_a_reference_that_lands_mid_run_reruns_a_misheard_analysis(monkeypatch) -> None:
    captured: list[SpeechCleanupAnalysisInput] = []
    rerun = _misheard_result("en")

    def run_engine(analysis_input):
        captured.append(analysis_input)
        return rerun if analysis_input.reference_transcript else _misheard_result("tr")

    monkeypatch.setattr(task_module, "run_speech_cleanup_engine", run_engine)
    monkeypatch.setattr(task_module, "_late_reference_transcript", lambda _work: _GEMINI_EN)

    result = task_module._run_engine(_work(source_kind="embedded_spine"), Path("narration.wav"))

    assert [value.reference_transcript for value in captured] == [None, _GEMINI_EN]
    assert result is rerun


@pytest.mark.parametrize(
    ("source_kind", "late_reference"),
    [
        ("embedded_spine", None),  # Gemini has still not landed
        ("embedded_spine", "Bu videoyu çok güzel çektim bugün, hadi bakalım"),  # agrees
        ("voiceover", _GEMINI_EN),  # a voiceover never has one: not even read
    ],
)
def test_first_result_stands_without_a_contradicting_late_reference(
    monkeypatch, source_kind: str, late_reference: str | None
) -> None:
    first = _misheard_result("tr")
    engine = Mock(return_value=first)
    monkeypatch.setattr(task_module, "run_speech_cleanup_engine", engine)
    late = Mock(return_value=late_reference)
    monkeypatch.setattr(task_module, "_late_reference_transcript", late)

    result = task_module._run_engine(_work(source_kind=source_kind), Path("narration.wav"))

    assert result is first
    assert engine.call_count == 1
    assert late.call_count == (0 if source_kind == "voiceover" else 1)


def test_late_reference_read_fails_open_on_a_database_error(monkeypatch) -> None:
    from sqlalchemy.exc import OperationalError

    def broken_session():
        raise OperationalError("SELECT 1", {}, Exception("db down"))

    monkeypatch.setattr(task_module, "sync_session", broken_session)

    assert task_module._late_reference_transcript(_work(source_kind="embedded_spine")) is None


# ── Gemini's own reference when the item has none (Kria v2, voiceovers) ─────


def _gemini_reference_on(monkeypatch, transcribe: Mock) -> None:
    monkeypatch.setattr(task_module.settings, "speech_cleanup_gemini_reference_enabled", True)
    monkeypatch.setattr(task_module, "_transcribe_reference_sample", transcribe)


def _write_silent_pcm(path: Path, *, seconds: float) -> None:
    with wave.open(str(path), "wb") as artifact:
        artifact.setnchannels(task_module.SPEECH_CLEANUP_CHANNELS)
        artifact.setsampwidth(task_module.SPEECH_CLEANUP_SAMPLE_WIDTH_BYTES)
        artifact.setframerate(task_module.SPEECH_CLEANUP_SAMPLE_RATE_HZ)
        artifact.writeframes(b"\x00\x00" * int(seconds * task_module.SPEECH_CLEANUP_SAMPLE_RATE_HZ))


@pytest.mark.parametrize("source_kind", ["embedded_spine", "voiceover"])
def test_worker_asks_gemini_when_the_item_has_no_reference(monkeypatch, source_kind: str) -> None:
    captured: list[SpeechCleanupAnalysisInput] = []
    monkeypatch.setattr(
        task_module,
        "run_speech_cleanup_engine",
        lambda analysis_input: captured.append(analysis_input) or _misheard_result("en"),
    )
    transcribe = Mock(return_value=_GEMINI_EN)
    _gemini_reference_on(monkeypatch, transcribe)
    late = Mock()
    monkeypatch.setattr(task_module, "_late_reference_transcript", late)

    work = _work(source_kind=source_kind)
    task_module._run_engine(work, Path("narration.wav"))

    transcribe.assert_called_once_with(work, Path("narration.wav"))
    assert [(value.reference_transcript, value.reference_source) for value in captured] == [
        (_GEMINI_EN, "gemini_audio")
    ]
    late.assert_not_called()  # the run already had its second opinion


def test_an_item_reference_needs_no_gemini_call(monkeypatch) -> None:
    captured: list[SpeechCleanupAnalysisInput] = []
    monkeypatch.setattr(
        task_module,
        "run_speech_cleanup_engine",
        lambda analysis_input: captured.append(analysis_input) or _misheard_result("en"),
    )
    transcribe = Mock(return_value="unused")
    _gemini_reference_on(monkeypatch, transcribe)

    work = _work(source_kind="embedded_spine", reference_transcript=_GEMINI_EN)
    task_module._run_engine(work, Path("narration.wav"))

    transcribe.assert_not_called()
    assert [(value.reference_transcript, value.reference_source) for value in captured] == [
        (_GEMINI_EN, "clip_analysis")
    ]


def test_a_failed_gemini_call_keeps_the_single_listener_analysis(monkeypatch) -> None:
    first = _misheard_result("tr")
    engine = Mock(return_value=first)
    monkeypatch.setattr(task_module, "run_speech_cleanup_engine", engine)
    _gemini_reference_on(monkeypatch, Mock(side_effect=RuntimeError("gemini 503")))
    late = Mock(return_value=None)
    monkeypatch.setattr(task_module, "_late_reference_transcript", late)

    result = task_module._run_engine(_work(source_kind="embedded_spine"), Path("narration.wav"))

    assert result is first
    analysis_input = engine.call_args.args[0]
    assert (analysis_input.reference_transcript, analysis_input.reference_source) == (None, None)
    late.assert_called_once()  # the item's own transcript can still land mid-run


def test_the_kill_switch_skips_the_gemini_call(monkeypatch) -> None:
    monkeypatch.setattr(task_module, "run_speech_cleanup_engine", Mock(return_value=_result()))
    transcribe = Mock(return_value=_GEMINI_EN)
    monkeypatch.setattr(task_module, "_transcribe_reference_sample", transcribe)

    task_module._run_engine(_work(), Path("narration.wav"))  # autouse fixture: flag off

    transcribe.assert_not_called()


def test_a_soft_time_limit_during_the_gemini_call_is_not_swallowed(monkeypatch) -> None:
    engine = Mock()
    monkeypatch.setattr(task_module, "run_speech_cleanup_engine", engine)
    _gemini_reference_on(monkeypatch, Mock(side_effect=SoftTimeLimitExceeded()))

    with pytest.raises(SoftTimeLimitExceeded):
        task_module._run_engine(_work(source_kind="embedded_spine"), Path("narration.wav"))
    engine.assert_not_called()


def test_gemini_hears_only_the_opening_and_bills_the_creator(monkeypatch, tmp_path) -> None:
    from app.agents import transcript as transcript_agent

    monkeypatch.setattr(task_module, "SPEECH_CLEANUP_GEMINI_REFERENCE_SAMPLE_S", 0.5)
    artifact = tmp_path / "narration.wav"
    _write_silent_pcm(artifact, seconds=2.0)
    uploaded_seconds: list[float] = []

    def upload(sample_path: Path):
        with wave.open(str(sample_path), "rb") as sample:
            uploaded_seconds.append(sample.getnframes() / sample.getframerate())
        return SimpleNamespace(uri="https://generativelanguage.googleapis.com/v1beta/files/a")

    calls = []

    def run(_agent, agent_input, *, ctx):
        calls.append((agent_input, ctx))
        return transcript_agent.TranscriptOutput(full_text="  So today\n we built  the thing. ")

    monkeypatch.setattr(task_module, "_upload_reference_sample", upload)
    monkeypatch.setattr(transcript_agent.TranscriptAgent, "run", run)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    work = _work(
        source_kind="embedded_spine",
        plan_item_id=uuid.uuid4(),
        creator_id=str(uuid.uuid4()),
    )

    text = task_module._transcribe_reference_sample(work, artifact)

    assert text == "So today we built the thing."
    assert uploaded_seconds == [0.5]
    agent_input, ctx = calls[0]
    assert agent_input.file_mime == "audio/wav"
    assert (ctx.creator_id, ctx.plan_item_id) == (work.creator_id, str(work.plan_item_id))
    assert ctx.usage_purpose == "optional_background"
    assert ctx.request_id == f"speech-cleanup-reference:{work.claim.analysis_id}"


def test_reference_upload_makes_one_attempt_and_waits_for_active(monkeypatch, tmp_path) -> None:
    uploads: list[str] = []

    class _Files:
        def upload(self, *, file, config):
            uploads.append(config.mime_type)
            return SimpleNamespace(name="files/a", state=SimpleNamespace(name="PROCESSING"))

        def get(self, *, name):
            return SimpleNamespace(name=name, state=SimpleNamespace(name="ACTIVE"))

    monkeypatch.setattr(
        "app.pipeline.agents.gemini_analyzer._get_client",
        lambda: SimpleNamespace(files=_Files()),
    )
    monkeypatch.setattr(task_module.time, "sleep", lambda _seconds: None)

    file_ref = task_module._upload_reference_sample(tmp_path / "reference-sample.wav")

    assert file_ref.state.name == "ACTIVE"
    assert uploads == ["audio/wav"]


def test_reference_upload_does_not_retry_an_outage(monkeypatch, tmp_path) -> None:
    attempts: list[int] = []

    class _Files:
        def upload(self, *, file, config):
            attempts.append(1)
            raise ConnectionError("503 from Gemini")

    monkeypatch.setattr(
        "app.pipeline.agents.gemini_analyzer._get_client",
        lambda: SimpleNamespace(files=_Files()),
    )

    with pytest.raises(ConnectionError):
        task_module._upload_reference_sample(tmp_path / "reference-sample.wav")
    assert attempts == [1]


def test_claim_carries_the_plan_owner_for_gemini_attribution(monkeypatch) -> None:
    row = _queued_row(DETECTOR_VERSION)
    plan = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    item = SimpleNamespace(id=row.plan_item_id, content_plan_id=plan.id)
    session = _ClaimSession(row, item, plan)
    monkeypatch.setattr(task_module, "sync_session", lambda: session)

    work = task_module._claim_work(str(row.id))

    assert work is not None
    assert (work.plan_item_id, work.creator_id) == (row.plan_item_id, str(plan.user_id))
