"""Focused contracts for the typed licensed-SFX Creator lane."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.agents._runtime import SchemaError
from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.agents.sfx_placement import SfxPlacementAgent, SfxPlacementInput, SfxPlacementOutput
from app.routes.creator_agent import (
    _apply_explicit_render_intent,
    _resolve_explicit_sfx_outside_manifest,
)
from app.services.creator_capabilities import (
    CreatorSfxUnavailableError,
    compile_strategy_to_plan,
    resolve_creator_sfx_catalog_ref,
)


def _manifest(*, effects: list[dict]) -> ResolvedCreatorManifest:
    return ResolvedCreatorManifest(
        item_id="item-1",
        edit_format="montage",
        render_program="guided",
        catalog=effects,
        capabilities={
            "edit_format:montage": {"available": True},
            "set_item_intent": {"available": True},
            "draft_guided_proposal": {"available": True},
            "dispatch_render": {"available": True},
            "sound_effects": {"available": True},
        },
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )


def test_licensed_sfx_intent_is_typed_and_bounded() -> None:
    strategy = CreativeStrategy(
        licensed_sfx={"sound_effect_id": "Fah", "semantics": "FUNNY_MOMENTS"}
    )
    assert strategy.licensed_sfx is not None
    assert strategy.licensed_sfx.effect_id == "Fah"
    assert strategy.licensed_sfx.semantics == "funny_moments"
    assert strategy.licensed_sfx.max_placements == 6
    with pytest.raises(ValidationError):
        CreativeStrategy(
            licensed_sfx={"effect_id": "fah", "semantics": "funny_moments", "max_placements": 7}
        )


def test_named_effect_resolves_exactly_case_insensitive_and_canonicalizes() -> None:
    manifest = _manifest(
        effects=[{"catalog_id": "sfx-fah", "kind": "sound_effect", "label": "Fah"}]
    )
    assert resolve_creator_sfx_catalog_ref(manifest, "fAh").catalog_id == "sfx-fah"
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(), "Create it; add the fah sound effect.", manifest=manifest
    )
    plan = compile_strategy_to_plan(manifest, strategy)
    assert plan.strategy.licensed_sfx.effect_id == "sfx-fah"


def test_unknown_named_effect_fails_instead_of_becoming_optional_sfx() -> None:
    manifest = _manifest(effects=[])
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(optional_treatments=["sfx"]),
        "Add the impossible sound effect.",
        manifest=manifest,
    )
    with pytest.raises(CreatorSfxUnavailableError):
        compile_strategy_to_plan(manifest, strategy)


@pytest.mark.asyncio
async def test_named_effect_outside_bounded_prompt_catalog_resolves_from_live_db() -> None:
    manifest = _manifest(effects=[])
    effect = SimpleNamespace(
        id="sfx-fah",
        name="Fah",
        status="ready",
        published_at=datetime.now(UTC),
        archived_at=None,
        audio_gcs_path="sound-effects/fah.mp3",
    )
    scalar_result = SimpleNamespace(all=lambda: [effect])
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: scalar_result))
    )

    planning_manifest = await _resolve_explicit_sfx_outside_manifest(
        db,
        "fAh",
        manifest=manifest,
    )
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(),
        "Add the Fah sound effect at funny moments.",
        manifest=planning_manifest,
    )
    plan = compile_strategy_to_plan(planning_manifest, strategy)

    assert planning_manifest.manifest_hash == manifest.manifest_hash
    assert plan.strategy.licensed_sfx.effect_id == "sfx-fah"
    db.execute.assert_awaited_once()


@pytest.mark.parametrize("phrase", ["title saying", "title that says", "title which says"])
def test_title_extractor_accepts_production_wording(phrase: str) -> None:
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(), f"Create the opening {phrase} 'Emir Olympics' using Rascal font."
    )
    assert strategy.opening_title == "Emir Olympics"


def test_full_production_wording_preserves_exact_title_and_named_effect() -> None:
    manifest = _manifest(
        effects=[{"catalog_id": "sfx-fah", "kind": "sound_effect", "label": "Fah"}]
    )
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(),
        (
            "Create an edit of the best moments. Use all uploaded assets once, keeping "
            "sports grouped. Amongst the videos, add groups of photos that transition "
            "in 0.2 seconds. Add a large title saying Emir Olympics. Ann Arbor 2022. "
            "Use Rascal font and yellow text. Add the Fah sound effect at funny moments."
        ),
        manifest=manifest,
    )

    assert strategy.opening_title == "Emir Olympics. Ann Arbor 2022."
    assert strategy.licensed_sfx is not None
    assert strategy.licensed_sfx.effect_id == "sfx-fah"
    assert strategy.licensed_sfx.semantics == "funny_moments"


def test_sfx_placement_output_rejects_more_than_six_placements() -> None:
    with pytest.raises(ValidationError):
        SfxPlacementOutput(placements=[{"effect_id": str(i), "at_s": float(i)} for i in range(7)])
    assert SfxPlacementAgent.spec.prompt_version == "2026-09-05-v2"


def test_sfx_placement_input_allows_visual_only_but_not_empty_evidence() -> None:
    visual_only = SfxPlacementInput(
        moments=[{"start_s": 2.0, "end_s": 3.0, "description": "Funny stumble"}],
        effects=[{"effect_id": "sfx-fah", "name": "Fah"}],
        duration_s=10.0,
    )
    assert visual_only.words == []

    with pytest.raises(ValidationError, match="timed words or visual moments"):
        SfxPlacementInput(
            effects=[{"effect_id": "sfx-fah", "name": "Fah"}],
            duration_s=10.0,
        )


def test_sfx_parser_rejects_raw_output_over_the_placement_cap() -> None:
    agent = SfxPlacementAgent.__new__(SfxPlacementAgent)
    with pytest.raises(SchemaError, match="at most 6"):
        agent.parse(
            '{"placements": ['
            + ",".join(f'{{"effect_id":"sfx-fah","at_s":{i}}}' for i in range(7))
            + "]}",
            SfxPlacementInput(
                words=[{"word": "hi", "start_s": 0.5, "end_s": 0.9}],
                effects=[{"effect_id": "sfx-fah", "name": "Fah"}],
                duration_s=10.0,
            ),
        )
