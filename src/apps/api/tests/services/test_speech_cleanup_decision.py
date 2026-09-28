"""Unit coverage for the shared v1/Kria-runtime-v2 speech-cleanup decision policy.

`app.services.speech_cleanup_decision` extracts (2026-09, KRI-205) the enforce-
mode fence `routes/creation_threads.py` ran inline, so both v1's chat route and
`app.kria.runtime.decide_approval` apply byte-identical policy. These tests
exercise the pure/near-pure pieces directly; `tests/routes/test_creation_threads_speech_cleanup.py`
covers the v1 HTTP surface end-to-end and `tests/kria/test_runtime_v2.py`
covers the `decide_approval` integration.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.models import SpeechCleanupAnalysis
from app.services.active_narration_source import ActiveNarrationResolution, ActiveNarrationSource
from app.services.speech_cleanup_decision import (
    LegacyCleanupDefault,
    SpeechCleanupDecisionConflict,
    SpeechCleanupDecisionOk,
    evaluate_enforce_mode_decision,
    legacy_default_decision,
    resolve_next_audio_mode,
)
from app.services.speech_cleanup_preflight import SPEECH_CLEANUP_ENGINE_VERSION


def _source(fingerprint: str = "fingerprint-1") -> ActiveNarrationSource:
    return ActiveNarrationSource(
        source_kind="voiceover",
        media_id="media-1",
        storage_path="users/private/take.wav",
        generation="generation-1",
        media_kind="audio",
        manifest_identity="media-1",
        window_start_s=0.0,
        window_end_s=10.0,
        edit_format="narrated",
        audio_mode="voiceover",
        resolved_renderer="narrated",
        detector_policy="engine:detector",
        source_policy_fingerprint=fingerprint,
    )


def _row(
    *,
    fingerprint: str = "fingerprint-1",
    status: str = "ready",
    candidate_count: int = 3,
    decision: str | None = None,
) -> SpeechCleanupAnalysis:
    return SpeechCleanupAnalysis(
        id=uuid.uuid4(),
        plan_item_id=uuid.uuid4(),
        source_kind="voiceover",
        source_media_identity="media-1",
        source_storage_path="users/private/take.wav",
        source_generation="generation-1",
        window_start_s=0.0,
        window_end_s=10.0,
        source_policy_fingerprint=fingerprint,
        engine_version=SPEECH_CLEANUP_ENGINE_VERSION,
        detector_version="mixed-gap-v1",
        analysis_payload_version="1",
        status=status,
        candidate_count=candidate_count,
        decision=decision,
    )


# ---------------------------------------------------------------------------
# resolve_next_audio_mode
# ---------------------------------------------------------------------------


def test_resolve_next_audio_mode_prefers_explicit_strategy_audio() -> None:
    item = SimpleNamespace(voiceover_gcs_path=None)
    assert (
        resolve_next_audio_mode(SimpleNamespace(audio_strategy="original_audio"), item)
        == "original"
    )
    assert resolve_next_audio_mode(SimpleNamespace(audio_strategy="licensed_music"), item) == "kria"


def test_resolve_next_audio_mode_falls_back_to_recorded_voiceover() -> None:
    item = SimpleNamespace(voiceover_gcs_path="users/1/voiceover.wav")
    assert resolve_next_audio_mode(SimpleNamespace(audio_strategy="voiceover"), item) == "voiceover"


def test_resolve_next_audio_mode_none_without_a_recorded_voiceover() -> None:
    item = SimpleNamespace(voiceover_gcs_path=None)
    assert resolve_next_audio_mode(SimpleNamespace(audio_strategy="voiceover"), item) is None


# ---------------------------------------------------------------------------
# legacy_default_decision
# ---------------------------------------------------------------------------


def test_legacy_default_for_missing_analysis_is_nothing() -> None:
    assert legacy_default_decision(None) == LegacyCleanupDefault(None, None)


def test_legacy_default_ready_with_findings_keeps_original() -> None:
    row = _row(status="ready", candidate_count=2)
    result = legacy_default_decision(row)
    assert result.choice == "keep_original"
    assert result.analysis_id == row.id


@pytest.mark.parametrize("status", ["queued", "running", "failed"])
def test_legacy_default_in_flight_or_failed_uses_unchecked_bypass(status: str) -> None:
    row = _row(status=status, candidate_count=0)
    result = legacy_default_decision(row)
    assert result.choice == "create_without_cleanup"
    assert result.analysis_id == row.id


def test_legacy_default_no_findings_submits_nothing() -> None:
    row = _row(status="no_findings", candidate_count=0)
    assert legacy_default_decision(row) == LegacyCleanupDefault(None, None)


# ---------------------------------------------------------------------------
# evaluate_enforce_mode_decision
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enforce_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)


@pytest.mark.asyncio
async def test_pending_conflict_when_source_enforced_but_no_analysis_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=None),
    )
    resolution = ActiveNarrationResolution(source=_source(), reason=None, video_present=True)

    result = await evaluate_enforce_mode_decision(
        db=object(),
        item=SimpleNamespace(id=uuid.uuid4()),
        cleanup_analysis_id=None,
        cleanup_choice=None,
        resolution=resolution,
    )

    assert result == SpeechCleanupDecisionConflict("speech_cleanup_pending")


@pytest.mark.asyncio
async def test_choice_required_when_ready_analysis_has_no_submitted_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(status="ready", candidate_count=4)
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    resolution = ActiveNarrationResolution(source=_source(), reason=None, video_present=True)

    result = await evaluate_enforce_mode_decision(
        db=object(),
        item=SimpleNamespace(id=uuid.uuid4()),
        cleanup_analysis_id=row.id,
        cleanup_choice=None,
        resolution=resolution,
    )

    assert result == SpeechCleanupDecisionConflict("speech_cleanup_choice_required")


@pytest.mark.asyncio
async def test_ok_passthrough_when_a_valid_choice_matches_the_current_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(status="ready", candidate_count=4)
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    resolution = ActiveNarrationResolution(source=_source(), reason=None, video_present=True)

    result = await evaluate_enforce_mode_decision(
        db=object(),
        item=SimpleNamespace(id=uuid.uuid4()),
        cleanup_analysis_id=row.id,
        cleanup_choice="keep_original",
        resolution=resolution,
    )

    assert result == SpeechCleanupDecisionOk(analysis_id=row.id, choice="keep_original")


@pytest.mark.asyncio
async def test_stale_analysis_id_conflicts_even_with_a_valid_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(status="ready", candidate_count=4)
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    resolution = ActiveNarrationResolution(source=_source(), reason=None, video_present=True)

    result = await evaluate_enforce_mode_decision(
        db=object(),
        item=SimpleNamespace(id=uuid.uuid4()),
        cleanup_analysis_id=uuid.uuid4(),
        cleanup_choice="keep_original",
        resolution=resolution,
    )

    assert result == SpeechCleanupDecisionConflict("speech_cleanup_analysis_changed")


@pytest.mark.asyncio
async def test_failed_analysis_is_a_dedicated_conflict_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(status="failed", candidate_count=0)
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    resolution = ActiveNarrationResolution(source=_source(), reason=None, video_present=True)

    result = await evaluate_enforce_mode_decision(
        db=object(),
        item=SimpleNamespace(id=uuid.uuid4()),
        cleanup_analysis_id=row.id,
        cleanup_choice=None,
        resolution=resolution,
    )

    assert result == SpeechCleanupDecisionConflict("speech_cleanup_failed")


@pytest.mark.asyncio
async def test_no_findings_with_a_submitted_choice_is_not_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(status="no_findings", candidate_count=0)
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    resolution = ActiveNarrationResolution(source=_source(), reason=None, video_present=True)

    result = await evaluate_enforce_mode_decision(
        db=object(),
        item=SimpleNamespace(id=uuid.uuid4()),
        cleanup_analysis_id=row.id,
        cleanup_choice="keep_original",
        resolution=resolution,
    )

    assert result == SpeechCleanupDecisionConflict("speech_cleanup_choice_not_allowed")


@pytest.mark.asyncio
async def test_policy_stale_fingerprint_refreshes_and_reports_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detector/engine deploy restamps the fingerprint of untouched media.

    The row must be replaced (not left dangling) and the caller told so it can
    commit that replacement before surfacing the conflict.
    """

    stale_row = _row(fingerprint="pre-deploy-fingerprint", status="ready", candidate_count=2)
    fresh_source = _source(fingerprint="post-deploy-fingerprint")
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=stale_row),
    )
    refreshed_id = uuid.uuid4()
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.refresh_policy_stale_analysis_async",
        AsyncMock(return_value=SimpleNamespace(refreshed=True, analysis_id=refreshed_id)),
    )
    resolution = ActiveNarrationResolution(source=fresh_source, reason=None, video_present=True)

    result = await evaluate_enforce_mode_decision(
        db=object(),
        item=SimpleNamespace(id=uuid.uuid4()),
        cleanup_analysis_id=stale_row.id,
        cleanup_choice="keep_original",
        resolution=resolution,
    )

    assert result == SpeechCleanupDecisionConflict(
        "speech_cleanup_analysis_changed", refreshed=True, refreshed_analysis_id=refreshed_id
    )


@pytest.mark.asyncio
async def test_not_enforced_for_source_and_no_analysis_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 0)
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=None),
    )
    resolution = ActiveNarrationResolution(source=_source(), reason=None, video_present=True)

    result = await evaluate_enforce_mode_decision(
        db=object(),
        item=SimpleNamespace(id=uuid.uuid4()),
        cleanup_analysis_id=None,
        cleanup_choice=None,
        resolution=resolution,
    )

    assert result == SpeechCleanupDecisionOk(analysis_id=None, choice=None)
