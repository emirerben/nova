import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).with_name("replay-creator-contracts.py")
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
    with pytest.raises(replay_script.ReplayError, match="base_sha"):
        replay_script.replay(snapshot(), value, lambda _: {"declined": False})
