from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.render_summary import (
    build_render_summary,
    build_render_summary_from_debug_payload,
)


def _event(stage: str, elapsed_ms: int, **data):
    return {
        "ts": "2026-07-24T10:00:00+00:00",
        "stage": "render_stage",
        "event": stage,
        "data": {
            "trace_id": "trace-1",
            "stage": stage,
            "elapsed_ms": elapsed_ms,
            "status": data.pop("status", "ok"),
            **data,
        },
    }


def test_render_summary_computes_totals_slowest_repeats_retries_and_cache():
    created = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
    started = created + timedelta(seconds=30)
    finished = started + timedelta(seconds=90)
    job = SimpleNamespace(
        created_at=created,
        started_at=started,
        finished_at=finished,
        assembly_plan={
            "variants": [
                {
                    "render_enqueued_at": "2026-07-24T10:03:00Z",
                    "render_started_at": "2026-07-24T10:03:05Z",
                }
            ]
        },
        pipeline_trace=[
            _event("transcription", 40_000, cache={"name": "transcript", "status": "hit"}),
            _event("upload", 8_000, counts={"artifact": "final"}),
            _event("upload", 5_000, counts={"artifact": "base"}),
            _event(
                "base_reframe_encode",
                12_000,
                status="failed",
                retry={"reason": "camera_retry_without_pulses"},
            ),
        ],
    )

    summary = build_render_summary(job, [SimpleNamespace(latency_ms=1234)])

    assert summary is not None
    assert summary["trace_id"] == "trace-1"
    assert summary["total_queue_ms"] == 35_000
    assert summary["total_processing_ms"] == 90_000
    assert summary["slowest_stages"][0]["stage"] == "transcription"
    assert summary["repeated_stages"] == [{"stage": "upload", "count": 2}]
    assert summary["retries"][0]["stage"] == "base_reframe_encode"
    assert summary["cache"] == {"transcript": {"hit": 1}}
    assert summary["agent_work_ms"] == 1234


def test_render_summary_legacy_null_trace_returns_none():
    job = SimpleNamespace(
        created_at=None,
        started_at=None,
        finished_at=None,
        assembly_plan=None,
        pipeline_trace=None,
    )

    assert build_render_summary(job) is None


def test_render_summary_from_debug_payload():
    payload = {
        "job": {
            "created_at": "2026-07-24T10:00:00Z",
            "started_at": "2026-07-24T10:00:10Z",
            "finished_at": "2026-07-24T10:00:20Z",
            "assembly_plan": None,
            "pipeline_trace": [_event("composition", 2_000)],
        },
        "agent_runs": [],
    }

    summary = build_render_summary_from_debug_payload(payload)

    assert summary is not None
    assert summary["total_queue_ms"] == 10_000
    assert summary["total_processing_ms"] == 10_000
