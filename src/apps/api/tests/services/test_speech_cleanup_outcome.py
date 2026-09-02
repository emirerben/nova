from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.speech_cleanup_outcome import (
    append_speech_cleanup_render_outcome_locked,
    build_speech_cleanup_render_outcome,
    speech_cleanup_outcome_id,
)


def _payload(**overrides):
    values = {
        "outcome": "published_candidate",
        "analysis_attempt_id": "attempt-a",
        "analysis_view": "full_clip",
        "detector_version": "mixed-gap-v1",
        "variant_id": "required-v1",
        "render_generation_id": "0123456789abcdef0123456789abcdef",
        "source_tag": "0123456789abcdef",
        "selected_plan": "candidate",
        "candidate_status": "ready",
        "output_removal_count": 2,
        "output_removed_ms": 782,
    }
    values.update(overrides)
    return build_speech_cleanup_render_outcome(**values)


@pytest.mark.parametrize(
    ("outcome", "selected_plan", "failure_phase", "failure_class"),
    [
        ("published_candidate", "candidate", None, None),
        ("published_baseline", "baseline", None, None),
        ("failed_before_publish", "candidate", "upload", "TimeoutError"),
        ("cancelled", "candidate", None, None),
        ("superseded", "candidate", None, None),
        ("restored_last_good", "candidate", "publish", "RuntimeError"),
        ("published_applied", "candidate", None, None),
        ("published_no_change", "baseline", None, None),
        ("published_baseline_fallback", "baseline", None, None),
        ("discarded_superseded", "candidate", None, None),
        ("discarded_finalization_rejected", "candidate", None, None),
        ("failed_owned", "candidate", "render", "SpineExtractionError"),
        ("cancelled_owned", "candidate", None, None),
    ],
)
def test_build_supports_every_authoritative_outcome(
    outcome: str,
    selected_plan: str,
    failure_phase: str | None,
    failure_class: str | None,
) -> None:
    payload = _payload(
        outcome=outcome,
        selected_plan=selected_plan,
        failure_phase=failure_phase,
        failure_class=failure_class,
    )

    assert payload["outcome"] == outcome
    assert payload["analysis_attempt_id"] == "attempt-a"
    assert payload["source_tag"] == "0123456789abcdef"
    assert payload["variant_id"] == "required-v1"
    assert payload["render_generation_id"] == "0123456789abcdef0123456789abcdef"
    assert all(not isinstance(value, (dict, list, tuple)) for value in payload.values())


def test_outcome_id_uses_only_the_documented_correlation_identity() -> None:
    first = _payload()
    changed_diagnostics = _payload(
        outcome="superseded",
        source_tag=None,
        selected_plan="baseline",
        candidate_status="validation_failed",
        output_removal_count=None,
        output_removed_ms=None,
    )
    changed_generation = _payload(render_generation_id="fedcba9876543210fedcba9876543210")

    assert first["outcome_id"] == changed_diagnostics["outcome_id"]
    assert first["outcome_id"] != changed_generation["outcome_id"]
    assert first["outcome_id"] == speech_cleanup_outcome_id(
        analysis_attempt_id="attempt-a",
        variant_id="required-v1",
        render_generation_id="0123456789abcdef0123456789abcdef",
        analysis_view="full_clip",
        detector_version="mixed-gap-v1",
    )


@pytest.mark.parametrize(
    "override",
    [
        {"analysis_attempt_id": "private speech here"},
        {"source_tag": "not-a-source-tag"},
        {"failure_phase": "upload failed because /private/video.mp4"},
        {"failure_class": "RuntimeError: private details"},
        {"output_removal_count": -1},
        {"output_removed_ms": 86_400_001},
    ],
)
def test_builder_rejects_content_bearing_or_unbounded_scalars(override) -> None:
    with pytest.raises(ValueError):
        _payload(**override)


def test_builder_rejects_plan_publication_mismatch() -> None:
    with pytest.raises(ValueError, match="candidate publication"):
        _payload(selected_plan="baseline")
    with pytest.raises(ValueError, match="baseline publication"):
        _payload(outcome="published_baseline", selected_plan="candidate")


def test_locked_append_persists_exact_private_event_and_replaces_json_value() -> None:
    original = [{"stage": "assembly", "event": "existing", "data": {}}]
    job = SimpleNamespace(pipeline_trace=original)
    payload = _payload()

    status = append_speech_cleanup_render_outcome_locked(job, payload)

    assert status == "persisted"
    assert job.pipeline_trace is not original
    assert original == [{"stage": "assembly", "event": "existing", "data": {}}]
    event = job.pipeline_trace[-1]
    assert event["stage"] == "silence_cut"
    assert event["event"] == "speech_cleanup_render_outcome"
    assert event["data"] == payload
    assert isinstance(event["ts"], str)


def test_locked_append_deduplicates_by_outcome_id_before_cap() -> None:
    payload = _payload()
    duplicate = {
        "ts": "2026-09-01T00:00:00Z",
        "stage": "silence_cut",
        "event": "speech_cleanup_render_outcome",
        "data": payload,
    }
    job = SimpleNamespace(pipeline_trace=[duplicate, *({} for _ in range(499))])

    assert append_speech_cleanup_render_outcome_locked(job, payload) == "persisted"
    assert len(job.pipeline_trace) == 500


def test_locked_append_reports_cap_without_mutating_job() -> None:
    original = [{} for _ in range(500)]
    job = SimpleNamespace(pipeline_trace=original)

    assert append_speech_cleanup_render_outcome_locked(job, _payload()) == "dropped_cap"
    assert job.pipeline_trace is original


def test_locked_append_is_fail_open_for_tampering_and_bad_job_state() -> None:
    payload = _payload()
    payload["outcome_id"] = "0" * 32
    job = SimpleNamespace(pipeline_trace=[])

    assert append_speech_cleanup_render_outcome_locked(job, payload) == "error"
    assert job.pipeline_trace == []
    assert (
        append_speech_cleanup_render_outcome_locked(
            SimpleNamespace(pipeline_trace={"not": "an array"}),
            _payload(),
        )
        == "error"
    )
