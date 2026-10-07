"""KRI-470 PR-F: what the creator SEES when a plan-authority cloud job is declined.

End to end: the real cloud entry (``orchestrate_generative_job`` -> ``_run_generative_job_impl``)
raises a typed decline, the real ``_fail_job`` persists it on the job, and the real recovery
observer (``_finish_..._observe`` via ``tests.kria.test_runtime_phone_v2._observe``) turns it
into the creator's chat reply.  Failure modes written first: the typed reason is persisted but
the observer shows the generic "retry" copy (a dead Retry loop that re-runs ingest each time),
the alternative is dropped, or a decline that used to RENDER now ends in an unexplained failure.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.tasks import generative_build as gb
from tests.kria.test_runtime_phone_v2 import _observe
from tests.tasks.test_route_override_flips import (
    VOICE_FILE,
    CloudRun,
    _cloud,
    _guided_cloud,
    _no_speech,
    _voiceover_execution_strategy,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _run_through_the_orchestrator(
    monkeypatch: pytest.MonkeyPatch, run: CloudRun
) -> SimpleNamespace:
    """Run the real task entry; return the job as ``_fail_job`` persisted it."""
    job = run.job

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def get(self, _model, _pk, **_kw):
            return job

        def commit(self):
            pass

    monkeypatch.setattr(gb, "_sync_session", lambda: _Session())
    monkeypatch.setattr(gb, "_owned_job_task_fence", lambda _id, **_k: contextlib.nullcontext(True))
    monkeypatch.setattr(
        "app.services.cloud_render_policy.block_cloud_render_task", lambda *a, **k: False
    )
    monkeypatch.setattr(gb, "job_heartbeat", lambda _id: contextlib.nullcontext())
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _id: contextlib.nullcontext()
    )
    monkeypatch.setattr(gb, "mark_finished", Mock())
    monkeypatch.setattr(gb, "mark_failed_phase", Mock())
    monkeypatch.setattr(gb, "_cancelled_job_write_rejected", lambda *a, **k: False)
    monkeypatch.setattr(gb, "_build_preflight_public_outcome", lambda *a, **k: None)
    gb.orchestrate_generative_job.run(str(job.id))
    assert job.status == "processing_failed", "the task did not terminalize the job"
    return job


def _creator_sees(job: SimpleNamespace):
    job.content_plan_ownership_epoch = 1
    job.status = "processing_failed"
    _outcome, execution, events = _observe(job, {})
    return execution.error, events[-1]["content"]


def _assert_asked_not_retried(error: dict, content: str, *, reason: str, alternative: str) -> None:
    assert error["recovery"] == "ask_user" and error["retryable"] is False, error
    assert error["decline_reason"] == reason
    assert alternative in content, content
    assert "retry without rebuilding" not in content


def test_a_required_voice_with_no_recording_asks_for_the_recording(monkeypatch) -> None:
    run = _cloud(monkeypatch, stamped=True, audio_strategy="voiceover")
    job = _run_through_the_orchestrator(monkeypatch, run)
    assert job.failure_reason == "creator_render_contract_unsupported"
    error, content = _creator_sees(job)
    _assert_asked_not_retried(
        error,
        content,
        reason="needs_choice",
        alternative="Record or upload your voice, or tell me to use music instead.",
    )


def test_a_guided_plan_on_an_audio_led_format_asks_which_one_to_keep(monkeypatch) -> None:
    run = _guided_cloud(
        monkeypatch, stamped=True, edit_format="talking_head", render_program="native"
    )
    job = _run_through_the_orchestrator(monkeypatch, run)
    error, content = _creator_sees(job)
    _assert_asked_not_retried(
        error, content, reason="requirement_conflict", alternative="Pick one: the guided story"
    )


def test_a_voiceover_binding_that_no_longer_holds_is_refused_with_the_repair_alternative(
    monkeypatch,
) -> None:
    run = _guided_cloud(monkeypatch, stamped=True, **_voiceover_execution_strategy())
    run.job.all_candidates["voiceover_gcs_path"] = VOICE_FILE
    job = _run_through_the_orchestrator(monkeypatch, run)
    error, content = _creator_sees(job)
    _assert_asked_not_retried(
        error, content, reason="evidence_missing", alternative="rebuild the edit"
    )


@pytest.mark.parametrize(("edit_format", "clips"), [("talking_head", 2), ("narrated_ready", 1)])
def test_a_speech_edit_with_no_speech_is_refused_not_retried(
    monkeypatch, edit_format, clips
) -> None:
    run = _cloud(monkeypatch, stamped=True, edit_format=edit_format, clips=clips)
    _no_speech(monkeypatch)
    job = _run_through_the_orchestrator(monkeypatch, run)
    error, content = _creator_sees(job)
    _assert_asked_not_retried(
        error, content, reason="evidence_missing", alternative="ask for a montage"
    )
    assert "clear speech" in content


def test_a_rollout_disabled_format_is_refused_with_the_alternative(monkeypatch) -> None:
    run = _cloud(monkeypatch, stamped=True, edit_format="subtitled", clips=1)
    monkeypatch.setattr(gb.settings, "subtitled_archetype_enabled", False, raising=False)
    job = _run_through_the_orchestrator(monkeypatch, run)
    error, content = _creator_sees(job)
    _assert_asked_not_retried(
        error, content, reason="capability_unavailable", alternative="Ask for a different format."
    )
