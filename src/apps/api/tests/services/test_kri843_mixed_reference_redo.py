"""KRI-843 follow-up: a MIXED Gemini reference redoes a preflight that ran without one (L7, L12).

`_redo_for_misheard_language` only redid a settled, undecided preflight when the EN/TR
cross-check named one language. A reference that speaks several (Don Julio T1's R1 reads
neither English nor Turkish to it) inferred "" and the run stood on whisper's one-language
translation forever. The worker's late-reference re-run had the same hole.

Bounded by construction: the redone run has the reference, so the engine writes a
`language_crosscheck` receipt (mixed or EN/TR), and a run with a receipt is never redone.
Failure modes: `docs/reviews/kri-843-live-branch-failure-modes.md`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.config import settings
from app.pipeline.caption_language import crosscheck_detected_language
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    analyze_speech_cleanup,
    language_redo_reason,
)
from app.services.clip_speech import SilenceDetectionResult
from app.tasks import speech_cleanup_analysis as task_module
from tests.pipeline.test_kri843_mixed_language_transcript import DURATION_S, T1, _t1_passes
from tests.services.test_speech_cleanup_language_crosscheck import (
    GEMINI_EN,
    GEMINI_TR,
    _assignment,
    _clip_source,
    _ensure_sync,
    _item,
    _settled,
)
from tests.tasks.test_speech_cleanup_analysis import _misheard_result, _work

R1 = T1["references"]["R1"]["text"]
R2 = T1["references"]["R2"]["text"]


# -- the predicate -------------------------------------------------------------------------------


def test_language_redo_reason_names_mixed_or_the_single_language(monkeypatch) -> None:
    assert crosscheck_detected_language("tr", reference_text=R1) is None  # today: nothing
    assert language_redo_reason("tr", R1) == "mixed"
    assert language_redo_reason("tr", R2) == "mixed"  # mixed beats the EN/TR read ("en")
    assert language_redo_reason("tr", GEMINI_EN) == "en"
    assert language_redo_reason("tr", GEMINI_TR) is None
    assert language_redo_reason("tr", None) is None
    assert language_redo_reason("tr", "") is None
    monkeypatch.setattr(settings, "speech_cleanup_mixed_language_enabled", False)
    assert language_redo_reason("tr", R1) is None
    assert language_redo_reason("tr", R2) == "en"


# -- L7: the scheduler ----------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["no_findings", "ready"])
def test_l7_a_mixed_reference_redoes_a_settled_undecided_run(monkeypatch, status) -> None:
    source = _clip_source()
    row = _settled(source, language="tr", status=status)
    item = _item([_assignment(R1)])

    intent = _ensure_sync(monkeypatch, row, item, source)

    assert intent is not None and (intent.analysis_id, intent.created) == (row.id, True)
    assert row.status == "queued"
    assert row.analysis_payload is None
    assert row.source_policy_fingerprint == source.source_policy_fingerprint


def test_l7_kill_switch_off_is_todays_scheduler(monkeypatch) -> None:
    monkeypatch.setattr(settings, "speech_cleanup_mixed_language_enabled", False)
    source = _clip_source()
    row = _settled(source, language="tr")
    item = _item([_assignment(R1)])

    intent = _ensure_sync(monkeypatch, row, item, source)

    assert intent is not None and intent.created is False
    assert row.status == "no_findings"


def test_l7_a_decided_run_is_never_redone(monkeypatch) -> None:
    source = _clip_source()
    row = _settled(source, language="tr")
    row.decision = "keep_original"
    row.decision_at = datetime.now(UTC)
    item = _item([_assignment(R1)])

    intent = _ensure_sync(monkeypatch, row, item, source)

    assert intent is not None and intent.created is False
    assert row.decision == "keep_original"


def test_l7_a_run_that_already_had_a_single_language_reference_is_not_redone(monkeypatch) -> None:
    """The reference it had read as one language; a later mixed read of the same text is no
    reason to pay again (the receipt is the bound)."""
    source = _clip_source()
    row = _settled(
        source,
        language="tr",
        diagnostics={
            "language_crosscheck": {
                "whisper_language": "tr",
                "reference_language": "",
                "applied": False,
                "reference_source": "clip_analysis",
            }
        },
    )
    item = _item([_assignment(R1)])

    intent = _ensure_sync(monkeypatch, row, item, source)

    assert intent is not None and intent.created is False


def test_l7_the_redone_run_writes_a_mixed_receipt_and_is_never_redone_again(monkeypatch) -> None:
    """Round trip: redo -> the engine reads R1 and merges per stretch -> its receipt stops a
    second redo. Once per snapshot."""
    source = _clip_source()
    item = _item([_assignment(R1)])
    first_row = _settled(source, language="tr")
    assert _ensure_sync(monkeypatch, first_row, item, source).created is True

    passes = _t1_passes()
    transcribe = Mock(
        side_effect=lambda _path, *, language, verbatim_prompt: passes[
            "tr" if language is None else language
        ]
    )
    result = analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint="a" * 64,
            local_media_path="take.wav",
            duration_s=DURATION_S,
            reference_transcript=R1,
            reference_source="clip_analysis",
        ),
        transcribe_fn=transcribe,
        silence_detect_fn=lambda *_a, **_k: SilenceDetectionResult(
            spans=tuple((s, e) for s, e in T1["silence_spans"]), status="ok"
        ),
    )
    receipt = result.diagnostics["language_crosscheck"]
    assert receipt["mixed"] is True and receipt["applied"] is True

    redone = _settled(
        source, language=result.language, diagnostics={"language_crosscheck": receipt}
    )
    intent = _ensure_sync(monkeypatch, redone, item, source)

    assert intent is not None and intent.created is False
    assert redone.status == "no_findings"


# -- L12: the worker's late reference -------------------------------------------------------------


def test_l12_a_mixed_reference_landing_mid_run_reruns_the_engine(monkeypatch) -> None:
    captured: list[SpeechCleanupAnalysisInput] = []
    rerun = _misheard_result("en")

    def run_engine(analysis_input):
        captured.append(analysis_input)
        return rerun if analysis_input.reference_transcript else _misheard_result("tr")

    monkeypatch.setattr(task_module, "run_speech_cleanup_engine", run_engine)
    monkeypatch.setattr(task_module, "_late_reference_transcript", lambda _work: R1)

    result = task_module._run_engine(_work(source_kind="embedded_spine"), Path("narration.wav"))

    assert [value.reference_transcript for value in captured] == [None, R1]
    assert captured[1].reference_source == "clip_analysis"
    assert captured[1].reference_covers_s is None  # the item's analysis heard the whole clip
    assert result is rerun


def test_l12_kill_switch_off_keeps_todays_late_reference_rule(monkeypatch) -> None:
    monkeypatch.setattr(settings, "speech_cleanup_mixed_language_enabled", False)
    first = _misheard_result("tr")
    engine = Mock(return_value=first)
    monkeypatch.setattr(task_module, "run_speech_cleanup_engine", engine)
    monkeypatch.setattr(task_module, "_late_reference_transcript", lambda _work: R1)

    result = task_module._run_engine(_work(source_kind="embedded_spine"), Path("narration.wav"))

    assert engine.call_count == 1 and result is first
