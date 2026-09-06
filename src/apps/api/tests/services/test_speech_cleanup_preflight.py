from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    SpeechCleanupAnalysisResult,
    analyze_speech_cleanup,
)
from app.pipeline.transcribe import Transcript, Word
from app.services.clip_speech import SilenceDetectionResult

DURATION_S = 10.0
MIXED_GAP_ISLANDS = ((5.778866, 6.209660), (7.406100, 7.977846))
INCIDENT_WORDS = [
    Word("başla", 1.214694, 1.757506, 0.99),
    Word("devam", 1.875034, 2.529025, 0.98),
    Word("konuşma", 3.255057, 4.358934, 0.97),
    Word("şimdi", 8.293356, 9.677755, 0.96),
]
INCIDENT_SILENCES = (
    (0.0, 1.214694),
    (1.757506, 1.875034),
    (2.529025, 3.255057),
    (4.358934, 5.778866),
    (6.209660, 7.406100),
    (7.977846, 8.293356),
    (9.677755, 10.0),
)


def analysis_input(**changes: object) -> SpeechCleanupAnalysisInput:
    values: dict[str, object] = {
        "source_fingerprint": "source-policy-fingerprint",
        "local_media_path": "/private/tmp/exact-window.wav",
        "duration_s": DURATION_S,
        "source_window_start_s": 20.0,
        "source_window_end_s": 30.0,
        "mixed_gap_mode": "apply",
    }
    values.update(changes)
    return SpeechCleanupAnalysisInput.model_validate(values)


def fake_transcript(words: list[Word] | None = None, *, low_confidence: bool = False) -> Transcript:
    return Transcript(
        words=list(INCIDENT_WORDS if words is None else words),
        full_text="private transcript text",
        low_confidence=low_confidence,
        language="tr",
    )


def run_analysis(
    request: SpeechCleanupAnalysisInput,
    *,
    transcript: Transcript | None = None,
    silences: tuple[tuple[float, float], ...] = INCIDENT_SILENCES,
    silence_status: str = "ok",
) -> SpeechCleanupAnalysisResult:
    return analyze_speech_cleanup(
        request,
        transcribe_fn=lambda *_args, **_kwargs: transcript or fake_transcript(),
        silence_detect_fn=lambda *_args, **_kwargs: SimpleNamespace(
            spans=silences,
            status=silence_status,
        ),
    )


def test_apply_preserves_960_mixed_gap_detection_and_serializes_exact_snapshot() -> None:
    result = run_analysis(analysis_input())

    assert result.safety_signals.selected_plan == "candidate"
    assert result.safety_signals.candidate_status == "ready"
    assert result.cut_plan.version == 2
    acoustic = [finding for finding in result.findings if finding.reason == "filler_acoustic"]
    assert [(item.start_s, item.end_s) for item in acoustic] == list(MIXED_GAP_ISLANDS)
    assert result.public_receipt.candidate_count == len(result.findings)
    assert result.public_receipt.category_counts.filler_sounds == 2
    assert result.public_receipt.estimated_removed_ms == round(result.cut_plan.time_saved_s * 1000)

    payload = result.to_payload()
    # The private persisted payload is JSON-safe and reconstructs an executable
    # CutPlan without any detector or transcription call.
    encoded = json.dumps(payload)
    restored = SpeechCleanupAnalysisResult.from_payload(json.loads(encoded))
    hydrated = restored.cut_plan.to_cut_plan()
    assert hydrated.version == 2
    for island_start, island_end in MIXED_GAP_ISLANDS:
        assert any(
            removal.start_s <= island_start and removal.end_s >= island_end
            for removal in hydrated.removed
        )
    assert restored.timed_words[0].text == "başla"
    assert restored.source_window_start_s == 20.0
    assert restored.source_window_end_s == 30.0


def test_public_receipt_is_bounded_and_contains_no_private_media_or_speech() -> None:
    result = run_analysis(analysis_input())
    public = result.public_receipt.model_dump(mode="json")
    serialized = json.dumps(public)

    assert set(public) == {"candidate_count", "category_counts", "estimated_removed_ms"}
    assert "private transcript text" not in serialized
    assert "başla" not in serialized
    assert "/private/tmp" not in serialized
    assert "source-policy-fingerprint" not in serialized
    assert "start_s" not in serialized


def test_shadow_records_v2_diagnostics_but_keeps_the_v1_execution_plan() -> None:
    result = run_analysis(analysis_input(mixed_gap_mode="shadow"))

    assert result.safety_signals.selected_plan == "baseline"
    assert result.safety_signals.candidate_status == "ready"
    assert result.cut_plan.version == 1
    assert all(finding.category != "filler" for finding in result.findings)
    assert result.diagnostics["mixed_gap_full_total"] == 2
    assert len(result.diagnostics["acoustic_decisions"]) == 2


def test_off_uses_the_existing_baseline_without_building_v2_diagnostics() -> None:
    result = run_analysis(analysis_input(mixed_gap_mode="off"))

    assert result.cut_plan.version == 1
    assert result.safety_signals.candidate_status == "not_run"
    assert result.diagnostics == {}


def test_non_ok_silence_status_never_selects_the_mixed_gap_candidate() -> None:
    result = run_analysis(analysis_input(), silence_status="ffmpeg_timeout")

    assert result.safety_signals.silence_detection_status == "ffmpeg_timeout"
    assert result.safety_signals.candidate_status == "tool_unavailable"
    assert result.safety_signals.selected_plan == "baseline"
    assert result.cut_plan.version == 1


def test_lexical_fillers_and_pauses_have_typed_category_counts() -> None:
    words = [
        Word("hello", 0.5, 1.2, 0.99),
        Word("um", 2.0, 2.4, 0.7),
        Word("then", 3.0, 3.3, 0.99),
        Word("world", 6.0, 6.7, 0.99),
    ]
    result = run_analysis(
        analysis_input(mixed_gap_mode="off"),
        transcript=fake_transcript(words),
        silences=((3.4, 5.8),),
    )

    assert {finding.category for finding in result.findings} == {"filler", "long_pause"}
    assert result.public_receipt.category_counts.filler_sounds == 1
    assert result.public_receipt.category_counts.long_pauses == 1


def test_exact_detector_inputs_and_verbatim_prompt_are_owned_by_engine() -> None:
    calls: list[tuple[object, ...]] = []

    def transcribe(path: str, *, language: str | None, verbatim_prompt: str | None) -> Transcript:
        calls.append(("transcribe", path, language, verbatim_prompt))
        return fake_transcript([])

    def silence(path: str, *, min_silence_s: float) -> SilenceDetectionResult:
        calls.append(("silence", path, min_silence_s))
        return SilenceDetectionResult(spans=(), status="ok")

    analyze_speech_cleanup(
        analysis_input(),
        transcribe_fn=transcribe,
        silence_detect_fn=silence,
    )

    assert calls[0][0:3] == ("transcribe", "/private/tmp/exact-window.wav", None)
    assert "Uh, um" in str(calls[0][3])
    assert calls[1] == ("silence", "/private/tmp/exact-window.wav", 0.1)


@pytest.mark.parametrize(
    "error",
    [TypeError("bug"), AttributeError("bug"), AssertionError("bug"), NameError("bug")],
)
def test_programming_errors_escape_instead_of_becoming_no_findings(error: Exception) -> None:
    def broken(*_args: object, **_kwargs: object) -> Transcript:
        raise error

    with pytest.raises(type(error), match="bug"):
        analyze_speech_cleanup(
            analysis_input(),
            transcribe_fn=broken,
            silence_detect_fn=lambda *_args, **_kwargs: SilenceDetectionResult(
                spans=(), status="ok"
            ),
        )


def test_low_confidence_and_no_words_remain_explicit_safety_signals() -> None:
    result = run_analysis(
        analysis_input(),
        transcript=fake_transcript([], low_confidence=True),
        silences=(),
    )

    assert result.timed_words == ()
    assert result.findings == ()
    assert result.safety_signals.transcript_low_confidence is True
    assert result.safety_signals.bailout_reason == "no_words"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["public_receipt"].update(candidate_count=999),
            "candidate count",
        ),
        (
            lambda payload: payload["public_receipt"].update(estimated_removed_ms=0),
            "removed duration",
        ),
        (
            lambda payload: payload["cut_plan"].update(time_saved_s=0),
            "time_saved_s",
        ),
        (
            lambda payload: payload["cut_plan"]["keep_segments"][0].update(start_s=0.2),
            "partition",
        ),
    ],
)
def test_persisted_snapshot_validation_rejects_cross_field_tampering(
    mutate: object,
    message: str,
) -> None:
    payload = run_analysis(analysis_input()).to_payload()
    mutate(payload)  # type: ignore[operator]

    with pytest.raises(ValidationError, match=message):
        SpeechCleanupAnalysisResult.from_payload(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"duration_s": 0},
        {"source_window_start_s": 4.0, "source_window_end_s": 3.0},
        {"source_window_start_s": 1.0, "source_window_end_s": 9.0},
        {"retake_spans": ((2, 1),)},
    ],
)
def test_invalid_analysis_contracts_fail_before_any_media_call(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        analysis_input(**changes)
