"""Offline replay of the KRI-524 post-deploy title-sequence captures.

These checks cover recorded model output through the real editor parser and
compiler. They do not claim a live provider call, phone render, or export.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.agents._runtime import ModelClient, ProviderOutcomeUnknownError
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.services.kria_editor_ops import compile_editor_ops, project_editor_draft
from tests.agents.conftest import MockModelClient
from tests.evals.runners.snapshot_variant import build_synthetic_job, build_synthetic_variant

FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures/prompt_coverage/postdeploy_title_sequence.json"
)


@pytest.fixture(scope="module")
def capture() -> dict:
    return json.loads(FIXTURE.read_text())


def _parse(capture: dict, response: dict):
    return EditCopilotAgent(ModelClient()).parse(
        json.dumps(response),
        EditCopilotInput(
            utterance=capture["input"]["utterance"],
            prior_turns=[],
            variant_snapshot=capture["input"]["variant_snapshot"],
        ),
    )


def _replay(capture: dict, response: dict):
    parsed = _parse(capture, response)
    variant = build_synthetic_variant(capture["input"]["variant_snapshot"], parsed.ops)
    job = build_synthetic_job(variant)
    payload = compile_editor_ops(job, variant, parsed.ops).payload
    return parsed, variant, payload


def _assert_success_oracle(capture: dict, parsed, variant: dict, payload) -> None:
    """Independent acceptance oracle shared by the good and bad replays."""
    rows = list(payload.text_elements)
    expected = capture["expected"]
    count = len(expected["segments"])
    source_bars = capture["input"]["variant_snapshot"]["text_bars"]
    source_sequence_count = (
        1 if source_bars[0].get("text") == " ".join(expected["segments"]) else count
    )
    assert parsed.outcome == "proposed"
    assert len(rows) == count + len(source_bars) - source_sequence_count
    assert [row["text"] for row in rows[:count]] == expected["segments"]
    windows = [(row["start_s"], row["end_s"]) for row in rows[:count]]
    for index, (start, end) in enumerate(windows):
        assert start == pytest.approx(index * expected["duration_s"] / count, abs=1e-5)
        assert end == pytest.approx((index + 1) * expected["duration_s"] / count, abs=1e-5)
    assert all(left[1] <= right[0] for left, right in zip(windows, windows[1:]))
    assert all(
        row["animation_phases"] == expected["phases"] | {"speed": 1.0} for row in rows[:count]
    )

    original_bars = source_bars[source_sequence_count:]
    for original, actual in zip(original_bars, rows[count:], strict=True):
        assert {key: actual.get(key) for key in original} == original

    projected = project_editor_draft(variant, payload.model_dump(mode="json"))
    original_slots = capture["input"]["variant_snapshot"]["slots"]
    actual_slots = projected["ai_timeline"]["slots"]
    assert len(actual_slots) == len(original_slots)
    for original, actual in zip(original_slots, actual_slots, strict=True):
        assert {key: actual.get(key) for key in original} == original
    assert projected["mix"] == capture["input"]["variant_snapshot"]["mix"]["music_level"]
    assert projected["music_track_id"] is None


def test_pro_recorded_response_compiles_13_fading_words_and_preserves_source(capture):
    assert capture["provenance"] == {
        "kind": "redacted_recorded_response_with_authored_snapshot",
        "layer": "recorded_model_replay",
        "rendered_execution": False,
    }
    parsed, variant, payload = _replay(capture, capture["responses"]["pro"])
    _assert_success_oracle(capture, parsed, variant, payload)


def test_authored_synthetic_live_faster_capture_replays_six_words_in_three_seconds(capture):
    faster = capture["synthetic_faster"]
    assert faster["provenance"] == {
        "kind": "authored_synthetic_live_capture",
        "rendered": False,
    }
    replay = {"input": faster["input"], "expected": faster["expected"]}
    parsed, variant, payload = _replay(replay, faster["response"])
    _assert_success_oracle(replay, parsed, variant, payload)
    assert [(row["start_s"], row["end_s"]) for row in payload.text_elements[:6]] == [
        (0.0, 0.5),
        (0.5, 1.0),
        (1.0, 1.5),
        (1.5, 2.0),
        (2.0, 2.5),
        (2.5, 3.0),
    ]


@pytest.mark.parametrize("name", ["flash_no_fades", "flash_wholebar_typewriter"])
def test_flash_recorded_failures_stay_negative_controls(capture, name):
    parsed, variant, payload = _replay(capture, capture["responses"][name])
    with pytest.raises(AssertionError):
        _assert_success_oracle(capture, parsed, variant, payload)


def test_provider_outcome_unknown_is_one_call_without_retry_or_fallback(monkeypatch):
    """An accepted provider call must remain fenced from replay attempts."""
    client = MockModelClient()
    client.queue("mock-editor", ProviderOutcomeUnknownError("provider outcome unknown"))
    monkeypatch.setattr(
        EditCopilotAgent,
        "spec",
        replace(EditCopilotAgent.spec, model="mock-editor"),
    )
    agent = EditCopilotAgent(client)
    input_data = EditCopilotInput(utterance="Animate the title word by word")
    with pytest.raises(ProviderOutcomeUnknownError):
        agent.run(input_data)
    assert len(client.invocations) == 1
    assert client.invocations[0]["model"] == "mock-editor"
