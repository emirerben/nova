"""Replay/live eval gate for the word→visual scene matcher."""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "scene_matcher"
AGENT_NAME = "nova.compose.scene_matcher"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(not FIXTURE_PATHS, reason="no scene matcher fixtures")
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda path: path.stem)
def test_scene_matcher_eval(
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

    result = run_eval(
        fixture,
        model_client=live_model_client if eval_mode == "live" else None,
        judge=judge_for(fixture.agent) if with_judge else None,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
    )
    assert result.passed, (
        f"\n{result.fixture_id}: {result.summary()}\n"
        f"  failures: {result.structural_failures}\n  error: {result.error}"
    )
    if fixture.meta.get("source") == "hand_authored_golden" and result.output:
        matches = result.output.get("matches") or []
        by_asset = {match["asset_id"]: match["anchor_word_id"] for match in matches}
        words_by_id = {word["word_id"]: word["text"] for word in fixture.input["words"]}
        # The contract of the brain: the country flag anchors ON the spoken
        # country name — never on a generic "flag" token, never cross-matched.
        for asset_id, spoken in (
            ("a-spain", "Spain"),
            ("a-arg", "Argentina"),
            ("a-eng", "England"),
            ("a-fra", "France"),
            ("a-messi", "Messi"),
        ):
            assert asset_id in by_asset, f"{asset_id} must be matched"
            assert words_by_id[by_asset[asset_id]] == spoken, (
                f"{asset_id} anchored on {words_by_id[by_asset[asset_id]]!r}, expected {spoken!r}"
            )
        # English chapter numbers must be tagged without Turkish vocab.
        tags = result.output.get("cue_tags") or []
        sequences = sorted(
            tag["sequence_number"]
            for tag in tags
            if tag["role"] == "list_item" and tag.get("sequence_number")
        )
        assert sequences == [1, 2], f"expected chapters [1, 2], got {sequences}"
