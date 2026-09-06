"""Safety and archival contracts for the chat speech-cleanup rollout gate."""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.speech_cleanup_rollout import (
    MAX_EVIDENCE_BYTES,
    RolloutEvidenceError,
    audit_rollout_evidence,
    project_admin_speech_cleanup_trace,
    sign_rollout_receipt,
    verify_rollout_receipt,
)

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "audit_chat_speech_cleanup_rollout.py"
)
spec = importlib.util.spec_from_file_location("audit_chat_speech_cleanup_rollout", _SCRIPT_PATH)
assert spec is not None and spec.loader is not None
script = importlib.util.module_from_spec(spec)
sys.modules["audit_chat_speech_cleanup_rollout"] = script
spec.loader.exec_module(script)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
SECRET = "rollout-receipt-secret-that-is-at-least-thirty-two-bytes"


def _evidence(**overrides):
    value = {
        "schema_version": 1,
        "observed_mode": "enforce",
        "observed_rollout_percent": 10,
        "observed_mixed_gap_mode": "apply",
        "observed_mixed_gap_rollout_percent": 10,
        "window_started_at": (NOW - timedelta(minutes=10)).isoformat(),
        "window_ended_at": (NOW - timedelta(minutes=1)).isoformat(),
        "worker_canary": {
            "queue": "speech-analysis",
            "consumer_count": 1,
            "published_count": 1,
            "completed_count": 1,
            "api_revision": "sha-a",
            "worker_revision": "sha-a",
        },
        "metrics": {
            "analysis_count": 100,
            "unexpected_failure_count": 1,
            "stuck_count": 0,
            "snapshot_mismatch_count": 0,
            "invalidation_error_count": 0,
            "duplicate_dispatch_count": 0,
            "private_payload_leak_count": 0,
            "queue_latency_p95_ms": 20_000,
            "run_latency_p95_ms": 120_000,
        },
        "source_receipts": {
            "embedded_video": 1,
            "narrated_upload_or_recording": 1,
            "audio_only_then_video": 1,
        },
        "outcome_receipts": {
            "applied": 1,
            "checked_no_change": 1,
            "declined": 1,
            "bypassed_unchecked": 1,
            "failed": 1,
        },
        "rollback_drill": {
            "completed": False,
            "completed_at": None,
            "restored_preflight_mode": None,
            "restored_preflight_percent": 0,
            "restored_mixed_gap_mode": None,
            "restored_mixed_gap_percent": 0,
            "inflight_contracts_preserved": False,
        },
    }
    value.update(overrides)
    return value


def test_adjacent_stage_with_worker_sources_outcomes_and_thresholds_passes() -> None:
    audit = audit_rollout_evidence(_evidence(), target_percent=25, now=NOW)

    assert audit.promotion_allowed is True
    assert audit.halt_reasons == ()
    assert audit.receipt["promotion_allowed"] is True
    assert audit.receipt["metrics"]["unexpected_failure_rate"] == 0.01


@pytest.mark.parametrize(
    ("target", "mode", "percent", "mixed_mode", "mixed_percent", "allowed"),
    [
        (1, "shadow", 100, "shadow", 100, True),
        (10, "enforce", 1, "apply", 1, True),
        (25, "enforce", 10, "apply", 10, True),
        (50, "enforce", 25, "apply", 25, True),
        (100, "enforce", 50, "apply", 50, False),
    ],
)
def test_only_adjacent_ramp_transitions_pass_before_ga_rollback_gate(
    target, mode, percent, mixed_mode, mixed_percent, allowed
) -> None:
    evidence = _evidence(
        observed_mode=mode,
        observed_rollout_percent=percent,
        observed_mixed_gap_mode=mixed_mode,
        observed_mixed_gap_rollout_percent=mixed_percent,
    )
    audit = audit_rollout_evidence(evidence, target_percent=target, now=NOW)

    assert audit.promotion_allowed is allowed
    if target == 100:
        assert audit.halt_reasons == ("rollback_drill_missing",)


def test_ga_requires_off_zero_rollback_drill_inside_evidence_window() -> None:
    evidence = _evidence(
        observed_rollout_percent=50,
        observed_mixed_gap_rollout_percent=50,
        rollback_drill={
            "completed": True,
            "completed_at": (NOW - timedelta(minutes=2)).isoformat(),
            "restored_preflight_mode": "off",
            "restored_preflight_percent": 0,
            "restored_mixed_gap_mode": "off",
            "restored_mixed_gap_percent": 0,
            "inflight_contracts_preserved": True,
        },
    )

    audit = audit_rollout_evidence(evidence, target_percent=100, now=NOW)

    assert audit.promotion_allowed is True


def test_threshold_or_safety_regression_halts_with_stable_reasons() -> None:
    metrics = dict(_evidence()["metrics"])
    metrics.update(
        {
            "unexpected_failure_count": 6,
            "stuck_count": 1,
            "snapshot_mismatch_count": 1,
            "invalidation_error_count": 1,
            "duplicate_dispatch_count": 1,
            "private_payload_leak_count": 1,
            "queue_latency_p95_ms": 120_001,
            "run_latency_p95_ms": 900_001,
        }
    )

    audit = audit_rollout_evidence(_evidence(metrics=metrics), target_percent=25, now=NOW)

    assert audit.promotion_allowed is False
    assert set(audit.halt_reasons) >= {
        "unexpected_failure_rate_exceeded",
        "queue_latency_p95_exceeded",
        "run_latency_p95_exceeded",
        "stuck_analyses_present",
        "snapshot_mismatches_present",
        "invalidation_errors_present",
        "duplicate_dispatches_present",
        "private_payload_leaks_present",
    }


@pytest.mark.parametrize("private_key", ["transcript", "source_path", "timed_words", "cuts"])
def test_evidence_schema_has_no_channel_for_private_content(private_key) -> None:
    evidence = _evidence()
    evidence[private_key] = "must not survive"

    with pytest.raises(RolloutEvidenceError, match="exact supported fields"):
        audit_rollout_evidence(evidence, target_percent=25, now=NOW)


def test_receipt_signature_detects_tampering_and_rejects_reused_credentials() -> None:
    audit = audit_rollout_evidence(_evidence(), target_percent=25, now=NOW)
    signed = sign_rollout_receipt(
        audit.receipt,
        secret=SECRET,
        key_id="rollout-2026-09",
        forbidden_secrets=("another-secret",),
    )

    assert verify_rollout_receipt(signed, secret=SECRET) is True
    signed["target_percent"] = 50
    assert verify_rollout_receipt(signed, secret=SECRET) is False
    with pytest.raises(RolloutEvidenceError, match="must not reuse"):
        sign_rollout_receipt(
            audit.receipt,
            secret=SECRET,
            key_id="rollout-2026-09",
            forbidden_secrets=(SECRET,),
        )


def test_admin_trace_projection_is_bounded_and_never_exposes_snapshot_content() -> None:
    analysis_id = "00000000-0000-4000-8000-000000000042"
    projected = project_admin_speech_cleanup_trace(
        {
            "speech_cleanup_contract": "required_v1",
            "_speech_cleanup_internal": {
                "preflight_snapshot": {
                    "analysis_id": analysis_id,
                    "detector_version": "mixed-gap-v2",
                    "source": {
                        "kind": "voiceover",
                        "storage_path": "voiceover-uploads/private/source.wav",
                        "generation": "secret-generation",
                        "source_policy_fingerprint": "secret-fingerprint",
                    },
                    "analysis": {
                        "timed_words": [{"text": "private speech"}],
                        "cut_plan": {"removed": [[1.0, 2.0]]},
                    },
                }
            },
            "speech_cleanup_outcome": {
                "status": "applied",
                "removal_count": 1,
                "removed_ms": 572,
                "job_id": "private-correlation",
                "render_generation_id": "private-generation",
            },
        }
    )

    assert projected == {
        "analysis_id": analysis_id,
        "source_kind": "voiceover",
        "detector_version": "mixed-gap-v2",
        "explicit_choice": "clean",
        "preflight_snapshot_reused": True,
        "execution_contract": "required_v1",
        "final_outcome": {
            "status": "applied",
            "removal_count": 1,
            "removed_ms": 572,
        },
    }
    encoded = json.dumps(projected)
    for private_value in (
        "private speech",
        "source.wav",
        "secret-generation",
        "secret-fingerprint",
        "1.0",
        "private-correlation",
    ):
        assert private_value not in encoded


def test_script_writes_immutable_mode_0600_signed_halt_receipt(tmp_path, monkeypatch) -> None:
    live_now = datetime.now(UTC)
    evidence = _evidence(
        window_started_at=(live_now - timedelta(minutes=10)).isoformat(),
        window_ended_at=(live_now - timedelta(minutes=1)).isoformat(),
    )
    evidence["worker_canary"] = {**evidence["worker_canary"], "consumer_count": 0}
    evidence_path = tmp_path / "evidence.json"
    output_path = tmp_path / "archive" / "receipt.json"
    evidence_path.write_text(json.dumps(evidence))
    monkeypatch.setenv("SPEECH_CLEANUP_ROLLOUT_RECEIPT_SECRET", SECRET)
    monkeypatch.setenv("SPEECH_CLEANUP_ROLLOUT_RECEIPT_KEY_ID", "rollout-2026-09")

    result = script.run(
        [
            "--evidence",
            str(evidence_path),
            "--target-percent",
            "25",
            "--output",
            str(output_path),
        ]
    )
    receipt = json.loads(output_path.read_text())

    assert result == 2
    assert receipt["promotion_allowed"] is False
    assert receipt["halt_reasons"] == ["dedicated_worker_consumer_missing"]
    assert verify_rollout_receipt(receipt, secret=SECRET) is True
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    assert (
        script.run(
            [
                "--evidence",
                str(evidence_path),
                "--target-percent",
                "25",
                "--output",
                str(output_path),
            ]
        )
        == 1
    )


def test_script_refuses_oversized_evidence_without_writing_receipt(tmp_path) -> None:
    evidence_path = tmp_path / "oversized.json"
    evidence_path.write_bytes(b"x" * (MAX_EVIDENCE_BYTES + 1))
    output_path = tmp_path / "receipt.json"

    assert (
        script.run(
            [
                "--evidence",
                str(evidence_path),
                "--target-percent",
                "25",
                "--output",
                str(output_path),
            ]
        )
        == 1
    )
    assert not output_path.exists()
