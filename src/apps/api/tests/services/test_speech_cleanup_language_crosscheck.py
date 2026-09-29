"""Speech-cleanup preflight: whisper's detected language vs Gemini's transcript.

Job 385e3b13 (2026-09-24): whisper-1 auto-detected "tr" on Turkish-accented
English and wrote a Turkish TRANSLATION instead of transcribing. Preflight
filler/pause decisions made on that translation cut the wrong audio, so:

  1. the engine re-transcribes in the language Gemini heard when the two
     clearly disagree (``analyze_speech_cleanup`` + ``reference_transcript``);
  2. the scheduler redoes a settled, undecided run that had no reference once
     Gemini's transcript lands and contradicts it — never revoking a decision,
     never repeating a run that already had a reference;
  3. when the plan item carries no Gemini transcript at all (every Kria v2
     project), the worker asks Gemini for one itself.

The worker adapter's reads live in ``tests/tasks/test_speech_cleanup_analysis.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from app.kria.media_sources import is_analysis_proxy_path
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    analyze_speech_cleanup,
)
from app.pipeline.transcribe import Word
from app.services.active_narration_source import (
    ActiveNarrationResolution,
    ActiveNarrationSource,
)
from app.services.plan_item_media import current_detector_policy, resolve_item_narration
from app.services.speech_cleanup_preflight import (
    SPEECH_CLEANUP_ENGINE_VERSION,
    SPEECH_CLEANUP_PAYLOAD_VERSION,
    ClaimedSpeechCleanupAnalysis,
    ensure_current_analysis_async,
    ensure_current_analysis_sync,
    reference_transcript_for_source,
)
from app.services.speech_cleanup_selection import DETECTOR_VERSION
from app.tasks import autoplace
from tests.services.test_speech_cleanup_analysis_lifecycle import (
    _analysis,
    _AsyncDB,
    _no_findings_payload,
    _SyncDB,
)

GEMINI_EN = (
    "So today we built the thing. Let's talk about the best football players "
    "in the Turkish Super League this season."
)
GEMINI_TR = "Bu videoyu çok güzel çektim bugün, hadi bakalım neler var"

# ── 1. engine ────────────────────────────────────────────────────────────────

DURATION_S = 6.5
SILENCES = ((2.5, 4.4),)


def _english_words() -> list[Word]:
    """What was actually said: a lexical filler ("um,") the cleanup removes."""
    return [
        Word(text="so", start_s=0.5, end_s=0.7, confidence=1.0),
        Word(text="um,", start_s=1.0, end_s=1.3, confidence=1.0),
        Word(text="today", start_s=1.5, end_s=1.9, confidence=1.0),
        Word(text="we", start_s=2.0, end_s=2.2, confidence=1.0),
        Word(text="built", start_s=4.6, end_s=4.9, confidence=1.0),
        Word(text="the", start_s=5.0, end_s=5.2, confidence=1.0),
        Word(text="thing.", start_s=5.3, end_s=5.9, confidence=1.0),
    ]


def _translated_words() -> list[Word]:
    """whisper's Turkish translation of the same audio: the filler is gone."""
    return [
        Word(text="Bugün", start_s=0.5, end_s=1.9, confidence=1.0),
        Word(text="şeyi", start_s=2.0, end_s=2.2, confidence=1.0),
        Word(text="yaptık.", start_s=4.6, end_s=5.9, confidence=1.0),
    ]


def _transcribe_by_language(by_language: dict) -> Mock:
    return Mock(
        side_effect=lambda _path, *, language, verbatim_prompt: by_language[language],
    )


def _transcript(words: list[Word], language: str) -> SimpleNamespace:
    return SimpleNamespace(words=words, language=language, low_confidence=False)


def _engine(transcribe: Mock):
    from app.services.clip_speech import SilenceDetectionResult

    return lambda analysis_input: analyze_speech_cleanup(
        analysis_input,
        transcribe_fn=transcribe,
        silence_detect_fn=lambda *_a, **_k: SilenceDetectionResult(spans=SILENCES, status="ok"),
    )


def _analyze(transcribe: Mock, *, reference: str | None):
    return _engine(transcribe)(
        SpeechCleanupAnalysisInput(
            source_fingerprint="f" * 64,
            local_media_path="narration.wav",
            duration_s=DURATION_S,
            reference_transcript=reference,
        )
    )


def _filler_findings(result) -> list:
    return [finding for finding in result.findings if finding.category == "filler"]


def test_engine_retranscribes_in_the_language_gemini_heard() -> None:
    transcribe = _transcribe_by_language(
        {
            None: _transcript(_translated_words(), "tr"),
            "en": _transcript(_english_words(), "en"),
        }
    )

    result = _analyze(transcribe, reference=GEMINI_EN)

    assert [c.kwargs["language"] for c in transcribe.call_args_list] == [None, "en"]
    # The second pass keeps the verbatim prompt: fillers must still be emitted.
    assert (
        transcribe.call_args_list[0].kwargs["verbatim_prompt"]
        == (transcribe.call_args_list[1].kwargs["verbatim_prompt"])
    )
    assert result.language == "en"
    assert [word.text for word in result.timed_words][:2] == ["so", "um,"]
    # The cut decision now sees the filler the translation had dropped.
    assert _filler_findings(result)
    assert result.diagnostics["language_crosscheck"] == {
        "whisper_language": "tr",
        "reference_language": "en",
        "applied": True,
    }


def test_engine_without_a_reference_is_unchanged() -> None:
    """No Gemini transcript yet: one pass, today's behavior, no receipt — so a
    later reschedule knows this run still needs its cross-check."""
    transcribe = _transcribe_by_language({None: _transcript(_translated_words(), "tr")})

    result = _analyze(transcribe, reference=None)

    assert transcribe.call_count == 1
    assert result.language == "tr"
    assert not _filler_findings(result)
    assert "language_crosscheck" not in result.diagnostics


def test_engine_keeps_whisper_when_gemini_agrees() -> None:
    transcribe = _transcribe_by_language({None: _transcript(_english_words(), "en")})

    result = _analyze(transcribe, reference=GEMINI_EN)

    assert transcribe.call_count == 1
    assert result.language == "en"
    assert result.diagnostics["language_crosscheck"] == {
        "whisper_language": "en",
        "reference_language": "en",
        "applied": False,
    }


def test_engine_keeps_the_first_pass_when_the_retranscribe_is_empty() -> None:
    transcribe = _transcribe_by_language(
        {
            None: _transcript(_translated_words(), "tr"),
            "en": _transcript([], "en"),
        }
    )

    result = _analyze(transcribe, reference=GEMINI_EN)

    assert transcribe.call_count == 2
    assert result.language == "tr"
    assert [word.text for word in result.timed_words] == ["Bugün", "şeyi", "yaptık."]
    assert result.diagnostics["language_crosscheck"]["applied"] is False


# ── 2. reference lookup + late-arrival redo ──────────────────────────────────

CLIP_PATH = "users/u/plan/item/clip.mp4"
CLIP_GENERATION = "1758000000000001"


def _clip_source(fingerprint: str = "c" * 64) -> ActiveNarrationSource:
    return ActiveNarrationSource(
        source_kind="embedded_spine",
        media_id="clip-1",
        storage_path=CLIP_PATH,
        generation=CLIP_GENERATION,
        media_kind="video",
        manifest_identity="clip-1",
        window_start_s=0.0,
        window_end_s=10.0,
        edit_format="subtitled",
        audio_mode="original",
        resolved_renderer="subtitled",
        detector_policy="policy-v1",
        source_policy_fingerprint=fingerprint,
    )


def _assignment(transcript: str | None, *, generation: str = CLIP_GENERATION) -> dict:
    """A clip assignment as the Gemini clip_metadata writers leave it."""
    entry: dict = {
        "gcs_path": CLIP_PATH,
        "media_id": "clip-1",
        "storage_generation": CLIP_GENERATION,
        "generation": generation,
    }
    if transcript is not None:
        entry["analysis"] = {
            "source": "clip_metadata",
            "analysis_version": autoplace.ANALYSIS_VERSION,
            "transcript": transcript,
            "on_screen_text": transcript[:400],
            "subject": "football",
            "description": "talking to camera",
            "best_moments": [],
        }
    return entry


def _item(assignments: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=assignments,
        speech_cleanup_enabled=False,
        speech_cleanup_notice=None,
    )


def _settled(
    source: ActiveNarrationSource,
    *,
    language: str = "tr",
    status: str = "no_findings",
    diagnostics: dict | None = None,
):
    row = _analysis(source, status=status, superseded_at=None)
    payload = _no_findings_payload(source)
    payload["language"] = language
    payload["diagnostics"] = diagnostics or {}
    row.analysis_payload = payload
    row.candidate_count = 0
    row.category_counts = {"filler_sounds": 0, "long_pauses": 0, "retakes": 0}
    row.estimated_removed_ms = 0
    return row


def _ensure_sync(monkeypatch, row, item, source):
    row.plan_item_id = item.id
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_sync",
        lambda *_a, **_k: row,
    )
    return ensure_current_analysis_sync(
        _SyncDB((row,)),
        item,
        ActiveNarrationResolution(source=source, reason=None, video_present=True),
    )


def test_reference_transcript_reads_the_exact_generation_only() -> None:
    item = _item([_assignment(GEMINI_EN)])
    assert (
        reference_transcript_for_source(item, storage_path=CLIP_PATH, generation=CLIP_GENERATION)
        == GEMINI_EN
    )
    # Other bytes at the same path, a pending analysis, or another path: none.
    assert reference_transcript_for_source(item, storage_path=CLIP_PATH, generation="other") is None
    assert (
        reference_transcript_for_source(
            _item([_assignment(None)]), storage_path=CLIP_PATH, generation=CLIP_GENERATION
        )
        is None
    )
    assert (
        reference_transcript_for_source(
            item, storage_path="users/u/voiceover.wav", generation=CLIP_GENERATION
        )
        is None
    )


@pytest.mark.parametrize("status", ["no_findings", "ready"])
def test_late_gemini_transcript_redoes_an_undecided_misheard_analysis(
    monkeypatch, status: str
) -> None:
    source = _clip_source()
    row = _settled(source, language="tr", status=status)
    item = _item([_assignment(GEMINI_EN)])

    intent = _ensure_sync(monkeypatch, row, item, source)

    assert intent is not None
    assert (intent.analysis_id, intent.created) == (row.id, True)
    assert row.status == "queued"
    assert row.analysis_payload is None
    assert row.attempt_token is None
    assert row.superseded_at is None
    # Same identity: dispatch fences keep matching the item's fingerprint.
    assert row.source_policy_fingerprint == source.source_policy_fingerprint


@pytest.mark.parametrize(
    ("assignments", "language", "decision", "diagnostics"),
    [
        pytest.param([_assignment(None)], "tr", None, None, id="gemini-not-landed"),
        pytest.param([_assignment(GEMINI_TR)], "tr", None, None, id="gemini-agrees"),
        pytest.param([_assignment(GEMINI_EN)], "tr", "keep_original", None, id="decided"),
        pytest.param(
            [_assignment(GEMINI_EN)],
            "tr",
            None,
            {
                "language_crosscheck": {
                    "whisper_language": "tr",
                    "reference_language": "en",
                    "applied": False,
                }
            },
            id="already-had-a-reference",
        ),
    ],
)
def test_settled_analysis_is_left_alone(
    monkeypatch, assignments, language, decision, diagnostics
) -> None:
    source = _clip_source()
    row = _settled(source, language=language, diagnostics=diagnostics)
    if decision is not None:
        row.decision = decision
        row.decision_at = datetime.now(UTC)
    item = _item(assignments)

    intent = _ensure_sync(monkeypatch, row, item, source)

    assert intent is not None
    assert (intent.analysis_id, intent.created) == (row.id, False)
    assert row.status == "no_findings"
    assert row.analysis_payload is not None
    assert row.decision == decision


def test_in_flight_analysis_is_left_to_the_worker(monkeypatch) -> None:
    """A running row is never reset here: the worker re-reads the reference
    before persisting (`_late_reference_transcript`)."""
    source = _clip_source()
    row = _analysis(source, status="running", superseded_at=None)
    row.attempt_token = "attempt-1"
    item = _item([_assignment(GEMINI_EN)])

    intent = _ensure_sync(monkeypatch, row, item, source)

    assert intent is not None and intent.created is False
    assert (row.status, row.attempt_token) == ("running", "attempt-1")


@pytest.mark.asyncio
async def test_async_scheduler_redoes_a_misheard_analysis_too(monkeypatch) -> None:
    source = _clip_source()
    row = _settled(source, language="tr")
    item = _item([_assignment(GEMINI_EN)])
    row.plan_item_id = item.id

    async def current(*_a, **_k):
        return row

    monkeypatch.setattr("app.services.speech_cleanup_preflight.current_analysis_async", current)

    intent = await ensure_current_analysis_async(
        _AsyncDB((row,)),
        item,
        ActiveNarrationResolution(source=source, reason=None, video_present=True),
    )

    assert intent is not None and (intent.analysis_id, intent.created) == (row.id, True)
    assert row.status == "queued"


def test_reactivated_historical_evidence_is_redone_when_misheard(monkeypatch) -> None:
    """A -> B -> A reuses A's settled evidence; if Gemini now contradicts its
    language, the reused row is analyzed again instead of trusted."""
    source = _clip_source()
    historical = _settled(source, language="tr")
    historical.superseded_at = datetime.now(UTC)
    item = _item([_assignment(GEMINI_EN)])
    historical.plan_item_id = item.id
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_sync", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight._exact_historical_analysis_sync",
        lambda *_a, **_k: historical,
    )

    intent = ensure_current_analysis_sync(
        _SyncDB((historical,)),
        item,
        ActiveNarrationResolution(source=source, reason=None, video_present=True),
    )

    assert intent is not None and (intent.analysis_id, intent.created) == (historical.id, True)
    assert historical.superseded_at is None
    assert historical.status == "queued"
    assert historical.engine_version == SPEECH_CLEANUP_ENGINE_VERSION
    assert historical.detector_version == DETECTOR_VERSION
    assert historical.analysis_payload_version == SPEECH_CLEANUP_PAYLOAD_VERSION


# ── 3. phone Talking on Kria v2: the worker's own Gemini reference ──────────
#
# Kria v2 never saves Gemini's clip analysis on the plan item (prod, 2026-09-28:
# no runtime-v2 item had one), so the lookup above finds nothing and the worker
# asks Gemini itself. A single-clip phone Talking edit applies this cut on the
# iPhone, so a translated transcript would cut the creator's real words.

PHONE_PROXY_PATH = "users/u/creation-threads/t/analysis-proxy-clip-1.mp4"


def _phone_talking_item() -> SimpleNamespace:
    """One analysis-proxy clip, exactly as the creation-thread attach leaves it."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        clip_gcs_paths=[PHONE_PROXY_PATH],
        clip_assignments=[
            {
                "gcs_path": PHONE_PROXY_PATH,
                "media_id": "clip-1",
                "kind": "video",
                "shot_id": None,
                "storage_generation": CLIP_GENERATION,
                "duration_s": DURATION_S,
                "has_audio": True,
                "manifest_identity": "clip-1",
                "upload_contract": {"purpose": "analysis_proxy"},
            }
        ],
        edit_format="subtitled",
        audio_mode="original",
        voiceover_gcs_path=None,
        speech_cleanup_enabled=False,
        speech_cleanup_notice=None,
    )


def _phone_source(item: SimpleNamespace) -> ActiveNarrationSource:
    source = resolve_item_narration(item, detector_policy=current_detector_policy()).source
    assert source is not None and source.source_kind == "embedded_spine"
    assert is_analysis_proxy_path(source.storage_path)
    return source


def test_phone_talking_cut_is_built_on_what_was_said(monkeypatch) -> None:
    from app.tasks import speech_cleanup_analysis as worker

    item = _phone_talking_item()
    source = _phone_source(item)
    assert (
        reference_transcript_for_source(
            item, storage_path=source.storage_path, generation=source.generation
        )
        is None
    )
    transcribe = _transcribe_by_language(
        {
            None: _transcript(_translated_words(), "tr"),
            "en": _transcript(_english_words(), "en"),
        }
    )
    monkeypatch.setattr(worker, "run_speech_cleanup_engine", _engine(transcribe))
    monkeypatch.setattr(worker.settings, "speech_cleanup_gemini_reference_enabled", True)
    monkeypatch.setattr(worker, "_transcribe_reference_sample", lambda _work, _path: GEMINI_EN)
    work = worker._ClaimedWork(
        claim=ClaimedSpeechCleanupAnalysis(
            analysis_id=uuid.uuid4(),
            attempt_token="attempt-1",
            source_policy_fingerprint=source.source_policy_fingerprint,
        ),
        source_storage_path=source.storage_path,
        source_generation=source.generation,
        window_start_s=0.0,
        window_end_s=DURATION_S,
        engine_version=SPEECH_CLEANUP_ENGINE_VERSION,
        detector_version=DETECTOR_VERSION,
        source_kind=source.source_kind,
    )

    result = worker._run_engine(work, Path("narration.wav"))

    assert result.language == "en"
    assert _filler_findings(result)  # the real "um," that the translation dropped
    assert result.diagnostics["language_crosscheck"] == {
        "whisper_language": "tr",
        "reference_language": "en",
        "applied": True,
        "reference_source": "gemini_audio",
    }


def test_phone_clean_confirmed_during_a_language_redo_asks_again(monkeypatch) -> None:
    """A phone clip that DOES carry Gemini's analysis (the v1 flow) can be redone
    by the scheduler. A "clean" confirmed meanwhile must not cut from the reset
    row: dispatch answers with the conflict Kria turns into "Tap Refresh project"."""
    from app.tasks.content_plan_build import DispatchResult, _speech_cleanup_dispatch_snapshot

    item = _phone_talking_item()
    item.clip_assignments[0]["generation"] = CLIP_GENERATION
    item.clip_assignments[0]["analysis"] = _assignment(GEMINI_EN)["analysis"]
    source = _phone_source(item)
    row = _settled(source, language="tr", status="ready")

    intent = _ensure_sync(monkeypatch, row, item, source)
    assert intent is not None and intent.created is True
    assert row.status == "queued"

    session = MagicMock()
    session.get.return_value = row
    result = _speech_cleanup_dispatch_snapshot(
        session, item, analysis_id=str(row.id), choice="clean"
    )

    assert isinstance(result, DispatchResult)
    assert result.outcome == "speech_cleanup_analysis_conflict"
    assert row.decision is None
