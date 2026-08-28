"""Replay eval gate for nova.edit.copilot.

Structural-only in CI: no network, no renderer/Skia imports. Live mode follows
the shared eval harness conventions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "edit_copilot"
AGENT_NAME = "nova.edit.copilot"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


def test_story_native_goldens_pin_the_current_prompt_version() -> None:
    """New story operations must remain attributable to the prompt that authored them."""
    from app.agents.edit_copilot import EDIT_COPILOT_PROMPT_VERSION

    story_fixtures = [path for path in FIXTURE_PATHS if path.stem.startswith("story_")]
    assert story_fixtures, "story-native replay goldens are missing"
    assert all(
        load_fixture(path).prompt_version == EDIT_COPILOT_PROMPT_VERSION for path in story_fixtures
    )


def _kria_fixture(name: str):
    path = next(path for path in FIXTURE_PATHS if path.stem == name)
    return load_fixture(path)


def test_kria_bulk_followup_replays_typed_image_selector() -> None:
    """The fourth turn must preserve the image referent and stay bulk/atomic."""
    fixture = _kria_fixture("kria_bulk_followup_satisfiable")
    result = run_eval(fixture)
    assert result.passed, f"{result.fixture_id}: {result.summary()} {result.error}"
    assert result.output is not None

    ops = result.output["ops"]
    assert {op["op"] for op in ops} == {
        "add_unused_sources",
        "set_media_duration",
        "stack_images",
    }
    duration_ops = [op for op in ops if op["op"] == "set_media_duration"]
    assert len(duration_ops) == 1
    assert duration_ops[0]["selector"] == {
        "scope": "timeline",
        "media_kind": "image",
        "quantifier": "all",
    }
    assert duration_ops[0]["duration_s"] == 0.2

    stack = next(op for op in ops if op["op"] == "stack_images")
    assert stack["selector"] == {
        "scope": "timeline",
        "media_kind": "image",
        "quantifier": "all",
    }
    assert "groups" not in stack
    assert "asset_ids" not in stack
    assert "assets" not in stack

    input_slots = fixture.input["variant_snapshot"]["slots"]
    assert len(input_slots) == 17
    assert sum(slot["media_kind"] == "video" for slot in input_slots) == 9
    assert sum(slot["media_kind"] == "image" for slot in input_slots) == 8
    source_summary = fixture.input["variant_snapshot"]["source_pool_summary"]
    assert source_summary["total_count"] == 104
    assert source_summary["ready_unused_count"] == 86
    assert source_summary["ready_unused_by_kind"] == {"image": 50, "video": 36}
    video_durations = {
        slot["index"]: slot["duration_s"] for slot in input_slots if slot["media_kind"] == "video"
    }
    assert video_durations  # the regression is specifically mixed media
    assert all(
        op.get("selector") != {"scope": "timeline", "media_kind": "video", "quantifier": "all"}
        for op in duration_ops
    )
    pending = fixture.input["prior_turns"][-1]["pending_actions"]
    assert {action["op"] for action in pending} == {
        "add_unused_sources",
        "stack_images",
        "set_media_duration",
    }


def test_kria_bulk_followup_replays_honest_impossible_all() -> None:
    """An unrepresentable all-source request must be a zero-op clarification."""
    fixture = _kria_fixture("kria_bulk_followup_impossible_all")
    assert fixture.input["variant_snapshot"]["motion"]["unused_ready_source_count"] > 100
    clarification = fixture.input["prior_turns"][-1]
    assert clarification["clarification_context"]["selector"] == {
        "scope": "timeline",
        "media_kind": "image",
        "quantifier": "all",
    }

    result = run_eval(fixture)
    assert result.passed, f"{result.fixture_id}: {result.summary()} {result.error}"
    assert result.output is not None
    assert result.output["ops"] == []
    assert result.output["needs_clarification"] is True
    assert result.output["outcome"] == "clarification"
    assert "104" in result.output["reply"]
    assert "103 additional" in result.output["reply"]
    assert "120-slot Save limit" in result.output["reply"]
    assert "10 Card Stacks" not in result.output["reply"]
    assert "eight-block limit" not in result.output["reply"]
    assert "8-second motion" not in result.output["reply"]


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=(
        f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/ — "
        "add hand-authored golden fixtures"
    ),
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_edit_copilot_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
    if fixture.agent != AGENT_NAME:
        pytest.skip(f"fixture is for {fixture.agent}, not {AGENT_NAME}")

    judge = judge_for(fixture.agent) if with_judge else None
    client = live_model_client if eval_mode == "live" else None

    result = run_eval(
        fixture,
        model_client=client,
        judge=judge,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
    )

    assert result.passed, (
        f"\n{result.fixture_id}: {result.summary()}\n"
        f"  failures: {result.structural_failures}\n"
        f"  error: {result.error}"
    )
