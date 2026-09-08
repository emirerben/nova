"""Cache identity for the per-job silence-cut analysis.

``_silence_cut_analysis`` computes one CutPlan per clip and shares it across
every variant of a job. Two settings choose which plan that is for identical
media: ``SPEECH_CLEANUP_BUDGET_CLAMP_ENABLED`` (bailout vs clamp) and
``SPEECH_CLEANUP_MAX_REMOVAL_FRAC_REQUIRED`` (how big the clamp budget is —
1.0 since 2026-09-08, i.e. MIN_OUTPUT_S is the only rail). Both must be part of
the cache key, or a flipped value reuses a plan built under the previous one.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest

from app.services.speech_cleanup_identity import SpeechCleanupAssignment
from app.tasks import generative_build as gb

JOB_ID = str(uuid.UUID("00000000-0000-0000-0000-0000000000aa"))
ASSIGNED = SpeechCleanupAssignment(0, "f" * 64, "assigned")
# Below MIN_CLIP_S: _compute returns the clip_too_short no-op before whisper or
# silencedetect, so the key composition is all this exercises.
TOO_SHORT_S = 0.5


@pytest.fixture
def analysis_env(monkeypatch):
    """Stub every IO edge _compute touches on the clip-too-short path."""

    from app.services import pipeline_trace

    @contextlib.contextmanager
    def no_session():
        raise RuntimeError("db unavailable")  # read fails open by contract
        yield  # pragma: no cover

    monkeypatch.setattr(gb, "_sync_session", no_session)
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_trace, "record_speech_cleanup_detection", lambda *a, **k: "ok")
    monkeypatch.setattr(gb.settings, "speech_cleanup_mixed_gap_mode", "shadow", raising=False)
    monkeypatch.setattr(gb.settings, "speech_cleanup_mixed_gap_rollout_percent", 100, raising=False)
    monkeypatch.setattr(gb.settings, "speech_cleanup_budget_clamp_enabled", True, raising=False)
    return monkeypatch


def _analyze(cache) -> dict:
    return gb._silence_cut_analysis(
        "/tmp/clip.mp4",
        TOO_SHORT_S,
        job_id=JOB_ID,
        cache=cache,
        analysis_policy="required_v1",
        render_trace_id="attempt-1",
        speech_cleanup_assignment=ASSIGNED,
    )


def test_removal_cap_is_part_of_the_analysis_cache_key(analysis_env) -> None:
    cache = gb._SilenceCutCache("/tmp")

    analysis_env.setattr(
        gb.settings, "speech_cleanup_max_removal_frac_required", 1.0, raising=False
    )
    _analyze(cache)
    analysis_env.setattr(
        gb.settings, "speech_cleanup_max_removal_frac_required", 0.55, raising=False
    )
    _analyze(cache)

    keys = sorted(cache.clips)
    assert len(keys) == 2, keys
    assert any("::frac=1" in key for key in keys)
    assert any("::frac=0.55" in key for key in keys)


def test_same_removal_cap_reuses_one_entry(analysis_env) -> None:
    cache = gb._SilenceCutCache("/tmp")
    analysis_env.setattr(
        gb.settings, "speech_cleanup_max_removal_frac_required", 1.0, raising=False
    )

    first = _analyze(cache)
    second = _analyze(cache)

    assert len(cache.clips) == 1
    assert first is second  # once-per-clip contract (plans/010 7A) is preserved
