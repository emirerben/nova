"""Sealed evaluator boundaries fixed before implementation; no paid providers."""

from dataclasses import asdict

import pytest

from app.services.creator_comparison_holdout import register_holdout, validate_registration
from app.services.creator_model_comparison import Arm, prepare_packet
from app.services.request_following_cases import digest


def packet(case):
    value = {
        "schema_version": 1,
        "cases": [case],
        "arms": [asdict(Arm("O", "gpt-6.1-sol", "medium"))],
        "seed": 727,
        "repetitions": 1,
    }
    value["sha256"] = digest(value)
    return value


def test_seal_pins_source_groups_and_arm_configuration():
    case = {
        "id": "sealed",
        "split": "holdout",
        "creator_id": "creator-a",
        "event_group_ids": ["event-a"],
        "source_kind": "synthetic_control",
        "input": {},
        "agent": "main_creator",
    }
    sealed = packet(case)
    registration = register_holdout(
        sealed, development_groups=["creator:creator-b", "event:event-b"]
    )
    validate_registration(sealed, registration)
    modified = {**sealed, "seed": 9}
    with pytest.raises(ValueError, match="registration"):
        validate_registration(modified, registration)
    with pytest.raises(ValueError, match="overlap"):
        register_holdout(sealed, development_groups=["event:event-a"])
    with pytest.raises(ValueError, match="holdout"):
        prepare_packet(sealed)


def test_empty_or_unreviewed_real_holdout_cannot_register():
    with pytest.raises(ValueError):
        register_holdout(
            packet(
                {
                    "id": "bad",
                    "split": "holdout",
                    "agent": "main_creator",
                    "source_kind": "exact_invocation",
                    "input": {},
                }
            ),
            development_groups=[],
        )


def test_sealed_replay_executes_without_exposing_body_or_weakening_development(tmp_path):
    import json
    from pathlib import Path

    from app.cli.compare_creator_models import _execute

    fixture = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/agent_evals/main_creator/montage_strategy.json"
        ).read_text()
    )
    case = {
        "id": "opaque",
        "agent": "main_creator",
        "split": "holdout",
        "creator_id": "heldout-owner",
        "event_group_ids": ["never-seen-media"],
        "source_kind": "synthetic_control",
        "input": fixture["input"],
        "raw_text": fixture["raw_text"],
    }
    sealed = packet(case)
    registration = register_holdout(sealed, development_groups=["creator:development"])
    tmp_path.chmod(0o700)
    with pytest.raises(ValueError, match="holdout"):
        _execute(sealed, tmp_path, live=False)
    result = _execute(sealed, tmp_path, live=False, registration=registration)
    assert result["parsed"] == 1 and result["split"] == "holdout"
    assert "private_journal" not in result
    assert "input" not in result and "output" not in result
