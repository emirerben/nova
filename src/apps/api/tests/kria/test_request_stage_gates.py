"""Regression coverage for runtime request preservation at stage boundaries."""

from __future__ import annotations

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.creator_policy import GUIDED_VOICEOVER_EXECUTION_CONTRACT
from app.kria.brief import BriefCoverageError
from app.kria.strategy_policy import (
    CheckedStrategy,
    RefusedStrategy,
    check_strategy_for_runtime_v2,
)
from app.services.kria_editor_ops import build_editor_snapshot
from tests.kria.test_strategy_policy import (
    CLIPS,
    PHOTOS,
    _narrated,
    _narrated_manifest,
    _talking,
    _talking_manifest,
)
from tests.services.test_kria_editor_clip_context import _job_and_variant


@pytest.mark.parametrize(
    ("manifest_factory", "strategy_factory"),
    [(_talking_manifest, _talking), (_narrated_manifest, _narrated)],
)
def test_caption_free_request_asks_before_simplifying(
    prod_profile, manifest_factory, strategy_factory
) -> None:
    """A caption-free request must not be silently changed to sentence captions."""
    refused = check_strategy_for_runtime_v2(
        manifest_factory(),
        strategy_factory(caption_style="none"),
        ask_before_simplifying=True,
    )

    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "simplification_requires_choice"
    assert "simpler" in refused.question.lower()
    assert refused.question.endswith("?")


def test_guided_voiceover_downgrade_asks_before_simplifying(prod_profile) -> None:
    strategy = CreativeStrategy(
        edit_format="narrated_planned",
        audio_strategy="voiceover",
        media_scope="all",
        execution_contract=GUIDED_VOICEOVER_EXECUTION_CONTRACT,
        render_program="guided",
        selected_media_ids=[*CLIPS, *PHOTOS],
    )

    refused = check_strategy_for_runtime_v2(
        _narrated_manifest(), strategy, ask_before_simplifying=True
    )

    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "simplification_requires_choice"
    assert "simpler" in refused.question.lower()
    assert refused.question.endswith("?")


def test_supported_talking_request_remains_unchanged_when_asking(prod_profile) -> None:
    strategy = _talking(caption_style="karaoke")

    checked = check_strategy_for_runtime_v2(
        _talking_manifest(), strategy, ask_before_simplifying=True
    )

    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy == strategy
    assert checked.notices == ()


def test_editor_snapshot_preserves_brief_longer_than_1500_chars() -> None:
    job, variant = _job_and_variant()
    brief = "x" * 2001

    snapshot = build_editor_snapshot(job, variant, clip_context={"brief": brief})

    assert snapshot["brief"] == brief


def test_editor_snapshot_rejects_brief_over_12000_chars() -> None:
    job, variant = _job_and_variant()

    with pytest.raises(BriefCoverageError, match="12,000"):
        build_editor_snapshot(job, variant, clip_context={"brief": "x" * 12001})


def test_editor_latest_request_preserves_long_complete_text() -> None:
    from app.agents.edit_copilot import EditCopilotInput, _clean_utterance
    from app.routes._copilot import CopilotTurnBody

    message = "Keep all six labels unchanged. " * 100
    body = CopilotTurnBody(message=message)
    agent_input = EditCopilotInput(utterance=body.message, snapshot={})
    assert _clean_utterance(agent_input.utterance) == message.strip()


def test_editor_request_over_limit_is_not_silently_truncated() -> None:
    from app.agents.edit_copilot import _clean_utterance

    with pytest.raises(ValueError, match="12,000"):
        _clean_utterance("x" * 12001)


def test_a_title_length_the_renderer_cannot_hold_is_asked_about_not_dropped(
    prod_profile,
) -> None:
    """KRI-476: the creator's own seconds for the title used to vanish in the plan compile
    (a narrated phone edit owns its title timing)."""
    strategy = _narrated(opening_title="My Trip", opening_title_duration_s=3.0)

    quiet = check_strategy_for_runtime_v2(_narrated_manifest(), strategy)
    assert isinstance(quiet, CheckedStrategy)  # no brief binding: unchanged, silent as before
    assert quiet.strategy.opening_title_duration_s is None

    # Brief binding on but the choice-questions flag off: identical to main (silent).
    flag_off = check_strategy_for_runtime_v2(
        _narrated_manifest(), strategy, ask_before_simplifying=True
    )
    assert isinstance(flag_off, CheckedStrategy) and flag_off == quiet

    asked = check_strategy_for_runtime_v2(
        _narrated_manifest(),
        strategy,
        ask_before_simplifying=True,
        ask_about_stated_settings=True,
    )
    assert isinstance(asked, RefusedStrategy)
    assert asked.code == "simplification_requires_choice"
    assert "exactly 3 seconds" in asked.question and asked.question.endswith("?")


def test_a_title_without_a_stated_length_is_never_asked_about(prod_profile) -> None:
    checked = check_strategy_for_runtime_v2(
        _narrated_manifest(),
        _narrated(opening_title="My Trip"),
        ask_before_simplifying=True,
        ask_about_stated_settings=True,
    )
    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.opening_title == "My Trip"


def test_a_dropped_fullscreen_ask_counts_as_a_dropped_request_and_is_worded() -> None:
    from app.kria.strategy_policy import _drops_requested_action, _repair_detail

    asked = CreativeStrategy(edit_format="montage", overlay_display="fullscreen")
    repaired = asked.model_copy(update={"overlay_display": None})
    assert _drops_requested_action(asked, repaired, stated_settings=True)
    assert not _drops_requested_action(asked, repaired)  # flag off: not counted, as on main
    assert _repair_detail(asked, repaired) == "Full-screen Visuals aren't available for this edit."
    # Nothing was asked for, so nothing was dropped.
    plain = CreativeStrategy(edit_format="montage")
    assert not _drops_requested_action(plain, plain)
