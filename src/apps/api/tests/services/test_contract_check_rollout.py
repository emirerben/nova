import pytest

from app.services.contract_check_rollout import (
    ENFORCE,
    LOG_ONLY,
    ContractCheckRolloutError,
    GenerationCheckRollout,
    run_contract_check,
    validate_promotion,
)


class ExpectedDecline(ValueError):
    pass


def test_generation_bound_mode_never_reads_a_live_default() -> None:
    rollout = GenerationCheckRollout("approved-17", True, {"duration_ownership": LOG_ONLY})
    result = run_contract_check(
        "duration_ownership",
        lambda: (_ for _ in ()).throw(ExpectedDecline("wrong owner")),
        rollout=rollout,
        decline_exceptions=ExpectedDecline,
    )
    assert result.mode == LOG_ONLY
    assert result.blocking is False
    assert "duration ownership" in result.decline_message
    with pytest.raises(ContractCheckRolloutError, match="no stamped rollout mode"):
        run_contract_check(
            "new_check", lambda: None, rollout=rollout, decline_exceptions=ExpectedDecline
        )


def test_enforced_stamp_makes_the_same_decline_blocking() -> None:
    rollout = GenerationCheckRollout("approved-18", True, {"recipe_integrity": ENFORCE})
    result = run_contract_check(
        "recipe_integrity",
        lambda: (_ for _ in ()).throw(ExpectedDecline("recipe is incomplete")),
        rollout=rollout,
        decline_exceptions=ExpectedDecline,
    )
    assert result.mode == ENFORCE
    assert result.blocking is True


def _evidence(**overrides):
    base = "a" * 40
    candidate = "b" * 40
    clean_since = 100
    value = {
        "schema_version": 1,
        "base_sha": base,
        "candidate_sha": candidate,
        "replay": {
            "complete": True,
            "successful": True,
            "base_sha": base,
            "candidate_sha": candidate,
            "available_count": 2,
            "input_count": 2,
            "replayed_count": 2,
        },
        "newly_blocking_declines": 0,
        "affected_supported_paths": ["phone", "cloud"],
        "canary_paths": ["phone", "cloud"],
        "clean_since_epoch_s": clean_since,
        "deployment_started_epoch_s": 99,
        "clean_observations": [
            {
                "interval_start_epoch_s": clean_since,
                "interval_end_epoch_s": clean_since + 12 * 60 * 60,
                "stream_complete": True,
                "check_mode": "log_only",
                "check_observations": 1,
                "check_declines": 0,
                "api_revision": candidate,
                "worker_revision": candidate,
            },
            {
                "interval_start_epoch_s": clean_since + 12 * 60 * 60,
                "interval_end_epoch_s": clean_since + 24 * 60 * 60,
                "stream_complete": True,
                "check_mode": "log_only",
                "check_observations": 1,
                "check_declines": 0,
                "api_revision": candidate,
                "worker_revision": candidate,
            },
        ],
        "canary_evidence": [
            {
                "path": "phone",
                "measured_export_success": True,
                "api_revision": candidate,
                "worker_revision": candidate,
                "export_duration_s": 24.0,
                "frame_count": 720,
                "command": "xcodebuild test",
                "app_build": "Kria 1",
                "source_fixture_sha256": "c" * 64,
                "source_provenance": {"capture": "offline"},
                "timestamp_epoch_s": clean_since,
            },
            {
                "path": "cloud",
                "measured_export_success": True,
                "api_revision": candidate,
                "worker_revision": candidate,
                "export_duration_s": 24.0,
                "frame_count": 720,
                "command": "cloud verifier replay",
                "app_build": "api build",
                "source_fixture_sha256": "d" * 64,
                "source_provenance": {"capture": "offline"},
                "timestamp_epoch_s": clean_since,
            },
        ],
    }
    value.update(overrides)
    return value


def test_promotion_requires_24_clean_hours_and_every_affected_path() -> None:
    report = validate_promotion(_evidence(), now_epoch_s=100 + 24 * 60 * 60 - 1)
    assert report.status == "pending"
    assert "fewer than 24 clean hours" in report.reasons
    report = validate_promotion(
        _evidence(canary_evidence=[_evidence()["canary_evidence"][0]]),
        now_epoch_s=100 + 24 * 60 * 60,
    )
    assert report.status == "pending"
    assert "canary lacks measured export success at candidate revisions" in report.reasons
    assert validate_promotion(_evidence(), now_epoch_s=100 + 24 * 60 * 60).status == "passed"


def test_promotion_missing_complete_replay_stays_pending() -> None:
    report = validate_promotion(
        _evidence(replay={"complete": False, "successful": True}), now_epoch_s=100 + 24 * 60 * 60
    )
    assert report.status == "pending"
    assert "missing complete successful replay" in report.reasons


def test_promotion_rejects_fake_shas_and_idle_or_untimestamped_observations() -> None:
    report = validate_promotion(
        _evidence(
            base_sha="base",
            clean_observations=[{"check_declines": 0}],
        ),
        now_epoch_s=100 + 24 * 60 * 60,
    )
    assert report.status == "pending"
    assert "base_sha must be a 40-character lowercase hex SHA" in report.reasons
    assert "clean observations are not log_only contract checks" in report.reasons


def test_promotion_requires_one_continuous_complete_observation_stream() -> None:
    evidence = _evidence()
    evidence["clean_observations"][1]["interval_start_epoch_s"] += 1
    report = validate_promotion(evidence, now_epoch_s=100 + 24 * 60 * 60)
    assert report.status == "pending"
    assert "clean observations do not cover one continuous 24-hour stream" in report.reasons

    evidence = _evidence()
    del evidence["clean_observations"][0]["stream_complete"]
    report = validate_promotion(evidence, now_epoch_s=100 + 24 * 60 * 60)
    assert report.status == "pending"
    assert "clean observations are not log_only contract checks" in report.reasons


def test_promotion_rejects_endpoint_only_or_non_finite_observations() -> None:
    evidence = _evidence(
        clean_observations=[
            {
                "timestamp_epoch_s": 100,
                "check_mode": "log_only",
                "check_observations": 1,
                "check_declines": 0,
                "api_revision": "b" * 40,
                "worker_revision": "b" * 40,
            }
        ]
    )
    report = validate_promotion(evidence, now_epoch_s=100 + 24 * 60 * 60)
    assert report.status == "pending"
    assert "clean observations are not log_only contract checks" in report.reasons

    evidence = _evidence()
    evidence["clean_observations"][0]["interval_end_epoch_s"] = float("inf")
    report = validate_promotion(evidence, now_epoch_s=100 + 24 * 60 * 60)
    assert report.status == "pending"
    assert "clean observations lack valid contiguous interval check counts" in report.reasons


def test_promotion_keeps_malformed_clean_evidence_pending_without_raising() -> None:
    evidence = _evidence(clean_since_epoch_s="not-a-timestamp")
    report = validate_promotion(evidence, now_epoch_s=100 + 24 * 60 * 60)
    assert report.status == "pending"
    assert "missing clean-hour evidence" in report.reasons

    evidence = _evidence()
    evidence["clean_observations"][0]["check_declines"] = False
    report = validate_promotion(evidence, now_epoch_s=100 + 24 * 60 * 60)
    assert report.status == "pending"
    assert "clean observations lack valid contract-check decline counts" in report.reasons


def test_promotion_rejects_invalid_or_duplicate_affected_paths_without_raising() -> None:
    report = validate_promotion(
        _evidence(affected_supported_paths=["phone", {"unhashable": "path"}]),
        now_epoch_s=100 + 24 * 60 * 60,
    )
    assert report.status == "pending"
    assert "affected supported paths must be unique nonempty strings" in report.reasons

    report = validate_promotion(
        _evidence(affected_supported_paths=["phone", "phone"]),
        now_epoch_s=100 + 24 * 60 * 60,
    )
    assert report.status == "pending"
    assert "affected supported paths must be unique nonempty strings" in report.reasons


def test_promotion_requires_bounded_replay_and_measured_canary_fields() -> None:
    report = validate_promotion(
        _evidence(
            replay={
                "complete": True,
                "successful": True,
                "base_sha": "a" * 40,
                "candidate_sha": "b" * 40,
                "available_count": 101,
                "input_count": 101,
                "replayed_count": 101,
            },
            canary_evidence=[{"path": "phone", "measured_export_success": True}],
        ),
        now_epoch_s=100 + 24 * 60 * 60,
    )
    assert report.status == "pending"
    assert (
        "replay is not base/candidate bound or does not cover its bounded inputs" in report.reasons
    )
    assert "canary lacks measured export success at candidate revisions" in report.reasons


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), float("-inf")])
def test_promotion_rejects_non_finite_export_measurements(duration) -> None:
    evidence = _evidence()
    evidence["canary_evidence"][0]["export_duration_s"] = duration
    report = validate_promotion(evidence, now_epoch_s=100 + 24 * 60 * 60)
    assert report.status == "pending"
    assert "canary lacks measured export success at candidate revisions" in report.reasons
