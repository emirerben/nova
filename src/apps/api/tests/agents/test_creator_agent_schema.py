"""Contract tests for the Main Creator Agent v1 schemas."""

import pytest
from pydantic import ValidationError

from app.agents._schemas.creator_agent import (
    CREATOR_AGENT_OUTPUT_ADAPTER,
    AskUser,
    CreativeStrategy,
    CreatorEditPlan,
    DispatchRenderCommand,
    ProposeStrategy,
    SetItemIntentCommand,
    canonical_context_hash,
    canonical_manifest_hash,
)


def _strategy() -> CreativeStrategy:
    return CreativeStrategy(direction="fast_montage", render_program="guided")


def test_media_and_catalog_contracts_reject_storage_capabilities() -> None:
    from app.agents._schemas.creator_agent import CreatorCatalogRef, CreatorMediaRef

    with pytest.raises(ValidationError):
        CreatorMediaRef(media_id="gs://bucket/clip.mp4", kind="video")
    with pytest.raises(ValidationError):
        CreatorCatalogRef(catalog_id="https://example.test/music/1", kind="music")


def test_agent_output_is_discriminated_and_strict() -> None:
    result = CREATOR_AGENT_OUTPUT_ADAPTER.validate_python(
        {"kind": "ask_user", "question": "Which cut?", "reason_code": "ambiguous_goal"}
    )
    assert isinstance(result, AskUser)

    result = CREATOR_AGENT_OUTPUT_ADAPTER.validate_python(
        {"kind": "propose_strategy", "strategy": _strategy()}
    )
    assert isinstance(result, ProposeStrategy)

    with pytest.raises(ValidationError):
        CREATOR_AGENT_OUTPUT_ADAPTER.validate_python(
            {"kind": "ask_user", "question": "Which cut?", "reason_code": "x", "url": "bad"}
        )


def test_context_hash_is_canonical_and_manifest_hash_excludes_self() -> None:
    left = canonical_context_hash({"b": 2, "a": [1, 2]})
    right = canonical_context_hash({"a": [1, 2], "b": 2})
    assert left == right

    from app.agents._schemas.creator_agent import ResolvedCreatorManifest

    manifest = ResolvedCreatorManifest(
        item_id="item-1",
        edit_format="montage",
        render_program="guided",
        context_hash=left,
        manifest_hash="0" * 64,
    )
    assert canonical_manifest_hash(manifest) == canonical_manifest_hash(
        manifest.model_copy(update={"manifest_hash": "f" * 64})
    )


def test_plan_commands_are_hash_pinned_and_bounded() -> None:
    manifest_hash = "a" * 64
    context_hash = "b" * 64
    plan = CreatorEditPlan(
        manifest_hash=manifest_hash,
        context_hash=context_hash,
        strategy=_strategy(),
        commands=[
            SetItemIntentCommand(
                command="set_item_intent",
                edit_format="montage",
                expected_manifest_hash=manifest_hash,
            ),
            DispatchRenderCommand(
                command="dispatch_render",
                expected_manifest_hash=manifest_hash,
                expected_context_hash=context_hash,
            ),
        ],
    )
    assert len(plan.commands) == 2

    with pytest.raises(ValidationError, match="pin the plan manifest_hash"):
        CreatorEditPlan(
            manifest_hash=manifest_hash,
            context_hash=context_hash,
            strategy=_strategy(),
            commands=[
                SetItemIntentCommand(
                    command="set_item_intent",
                    edit_format="montage",
                    expected_manifest_hash="c" * 64,
                )
            ],
        )


def test_strategy_carries_bounded_editorial_decisions() -> None:
    strategy = CreativeStrategy(
        edit_format="single_hero",
        archetype="single_hero",
        audio_strategy="licensed_music",
        story_structure=["hook", "demonstration", "payoff"],
        caption_style="kinetic",
        intro_hook="The result surprised me.",
        pacing="fast",
        optional_treatments=["overlays", "sfx", "transitions", "looks"],
        render_program="guided",
    )
    assert strategy.pace == "fast"
    assert strategy.story_structure[-1] == "payoff"

    with pytest.raises(ValidationError):
        CreativeStrategy(optional_treatments=["sfx", "sfx"])
