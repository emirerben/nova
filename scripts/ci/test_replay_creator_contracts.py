import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).with_name("replay-creator-contracts.py")
FIXTURE = SCRIPT.parent / "fixtures/kri492-creator-contracts.json"
spec = importlib.util.spec_from_file_location("creator_replay", SCRIPT)
replay_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay_script)


def snapshot():
    return {
        "schema_version": 1,
        "base_sha": "a" * 40,
        "available_count": 1,
        "missing_required_inputs_count": 0,
        "captures": [{
            "id": "captured-success",
            "source_provenance": {"capture": "redacted production replay"},
            "contract": {"requirements": ["duration"]},
            "variant": {"duration_s": 12.0, "render_status": "success", "render_destination": "native"},
            "candidates": {"creator_strategy": {"edit_format": "montage"}},
            "phone_recipe": {"tracks": []},
            "source_audio": {"has_audio": True},
            "composition": {"selected_media_count": 1},
        }],
    }


def baseline():
    return {"schema_version": 1, "base_sha": "a" * 40, "evaluator_sha": "a" * 40, "outcomes": {"captured-success": {"declined": False}}}


def test_missing_required_capture_inputs_cannot_pass() -> None:
    value = snapshot()
    del value["captures"][0]["source_provenance"]
    with pytest.raises(replay_script.ReplayError, match="source provenance"):
        replay_script.replay(value, baseline(), lambda _: {"declined": False})


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("available_count", "available_count"),
        ("missing_required_inputs_count", "missing required inputs"),
    ],
)
def test_snapshot_level_completeness_fields_fail_closed(field, message) -> None:
    value = snapshot()
    if field == "missing_required_inputs_count":
        value[field] = 1
    else:
        del value[field]
    with pytest.raises(replay_script.ReplayError, match=message):
        replay_script.replay(value, baseline(), lambda _: {"declined": False})


def test_phone_recipe_cannot_be_replayed_as_a_cloud_destination() -> None:
    value = snapshot()
    value["captures"][0]["variant"]["render_destination"] = "cloud"
    with pytest.raises(replay_script.ReplayError, match="device destination"):
        replay_script.replay(value, baseline(), lambda _: {"declined": False})


def test_candidate_newly_declining_successful_capture_fails_replay() -> None:
    report = replay_script.replay(snapshot(), baseline(), lambda _: {"declined": True})
    assert report["successful"] is False
    assert report["input_count"] == report["available_count"] == 1
    assert report["newly_blocking_capture_ids"] == ["captured-success"]


def test_baseline_must_be_bound_to_capture_base_sha() -> None:
    value = baseline()
    value["base_sha"] = "b" * 40
    with pytest.raises(replay_script.ReplayError, match="base SHAs"):
        replay_script.replay(snapshot(), value, lambda _: {"declined": False})


def test_historical_capture_can_compare_a_new_explicit_base(monkeypatch) -> None:
    comparison_base = "b" * 40
    monkeypatch.setattr(replay_script, "evaluate_candidate", lambda _: {"declined": False})
    captured_snapshot = replay_script.load_json(FIXTURE)
    exported = replay_script.export_outcomes(captured_snapshot, evaluator_sha=comparison_base)

    report = replay_script.replay(
        captured_snapshot, exported, lambda _: {"declined": False}, base_sha=comparison_base
    )

    assert report["base_sha"] == comparison_base
    assert report["capture_base_sha"] == captured_snapshot["base_sha"]
    assert exported["base_sha"] == comparison_base
    assert exported["capture_base_sha"] == captured_snapshot["base_sha"]


def test_explicit_comparison_base_rejects_a_mismatched_evaluator(monkeypatch) -> None:
    monkeypatch.setattr(replay_script, "evaluate_candidate", lambda _: {"declined": False})
    value = replay_script.export_outcomes(snapshot(), evaluator_sha="b" * 40)
    with pytest.raises(replay_script.ReplayError, match="comparison"):
        replay_script.replay(snapshot(), value, lambda _: {"declined": False}, base_sha="c" * 40)


def test_legacy_snapshot_base_remains_the_default_comparison_base() -> None:
    report = replay_script.replay(snapshot(), baseline(), lambda _: {"declined": False})
    assert report["base_sha"] == "a" * 40
    assert report["capture_base_sha"] == "a" * 40
