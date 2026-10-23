"""Failure modes for consent when speech cleanup can remove restarted speech.

1. An old client can submit ``clean`` after analysis finds a retake; that new
   decision must be refused because its card promised the restart would stay.
2. An updated client may submit the same ``clean`` decision only after the card
   explicitly names restart removal.
3. Keeping the original speech and analyses without retakes keep their prior
   behavior.
4. A clean decision already recorded on this exact, current analysis remains
   reusable; a changed source/policy analysis does not reuse it.
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
    SpeechCleanupDecisionConflict,
    SpeechCleanupDecisionOk,
    evaluate_enforce_mode_decision,
    reuse_recorded_cleanup_decision,
)
from app.services.speech_cleanup_preflight import SPEECH_CLEANUP_ENGINE_VERSION, public_projection


def _source() -> ActiveNarrationSource:
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
        source_policy_fingerprint="fingerprint-1",
    )


def _row(*, retakes: int = 1, decision: str | None = None) -> SpeechCleanupAnalysis:
    return SpeechCleanupAnalysis(
        id=uuid.uuid4(),
        plan_item_id=uuid.uuid4(),
        source_kind="voiceover",
        source_media_identity="media-1",
        source_storage_path="users/private/take.wav",
        source_generation="generation-1",
        window_start_s=0.0,
        window_end_s=10.0,
        source_policy_fingerprint="fingerprint-1",
        engine_version=SPEECH_CLEANUP_ENGINE_VERSION,
        detector_version="mixed-gap-v1",
        analysis_payload_version="1",
        status="ready",
        candidate_count=3,
        category_counts={"filler_sounds": 1, "long_pauses": 1, "retakes": retakes},
        decision=decision,
    )


@pytest.fixture(autouse=True)
def _enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)


async def _decision(
    monkeypatch: pytest.MonkeyPatch,
    row: SpeechCleanupAnalysis,
    *,
    choice: str,
    retakes_aware: bool = False,
    reused: bool = False,
):
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async", AsyncMock(return_value=row)
    )
    return await evaluate_enforce_mode_decision(
        db=object(),
        item=SimpleNamespace(id=uuid.uuid4()),
        cleanup_analysis_id=row.id,
        cleanup_choice=choice,
        cleanup_retakes_aware=retakes_aware,
        reused_recorded_decision=reused,
        resolution=ActiveNarrationResolution(source=_source(), reason=None, video_present=True),
    )


@pytest.mark.asyncio
async def test_old_client_new_clean_is_refused_when_persisted_findings_include_retakes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(retakes=1)
    result = await _decision(monkeypatch, row, choice="clean")
    assert result == SpeechCleanupDecisionConflict("speech_cleanup_retakes_consent_required")


@pytest.mark.asyncio
async def test_updated_client_clean_is_allowed_after_explicit_retake_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(retakes=1)
    result = await _decision(monkeypatch, row, choice="clean", retakes_aware=True)
    assert result == SpeechCleanupDecisionOk(row.id, "clean")


@pytest.mark.asyncio
async def test_keep_original_and_no_retake_clean_keep_existing_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_retakes = _row(retakes=2)
    with_retake_result = await _decision(monkeypatch, with_retakes, choice="keep_original")
    assert with_retake_result == SpeechCleanupDecisionOk(with_retakes.id, "keep_original")
    without_retakes = _row(retakes=0)
    assert await _decision(monkeypatch, without_retakes, choice="clean") == SpeechCleanupDecisionOk(
        without_retakes.id, "clean"
    )


@pytest.mark.asyncio
async def test_recorded_clean_on_same_current_analysis_is_reusable_without_new_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(retakes=1, decision="clean")
    reused = reuse_recorded_cleanup_decision(
        row,
        source_policy_fingerprint="fingerprint-1",
        submitted_analysis_id=None,
        submitted_choice=None,
    )
    assert reused == SpeechCleanupDecisionOk(row.id, "clean")
    reused_result = await _decision(monkeypatch, row, choice="clean", reused=True)
    assert reused_result == SpeechCleanupDecisionOk(row.id, "clean")


def test_public_projection_includes_retake_count_for_the_consent_card() -> None:
    projection = public_projection(
        _row(retakes=2), applicable=True, unavailable_reason=None, video_present=True
    )
    assert projection is not None
    assert projection["analysis"]["category_counts"]["retakes"] == 2
