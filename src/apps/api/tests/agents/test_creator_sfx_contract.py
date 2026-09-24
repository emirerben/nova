"""Focused contracts for the typed licensed-SFX Creator lane."""

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.agents._runtime import SchemaError
from app.agents._schemas.creator_agent import (
    CreativeStrategy,
    CreatorRenderIntentEvidence,
    ResolvedCreatorManifest,
)
from app.agents.sfx_placement import SfxPlacementAgent, SfxPlacementInput, SfxPlacementOutput
from app.routes.creator_agent import (
    _apply_explicit_render_intent,
    _creator_sources,
    _explicit_sfx_lookup_names,
    _explicit_sfx_name,
    _model_sfx_lookup_name,
    _resolve_described_sfx,
    _resolve_explicit_sfx_outside_manifest,
)
from app.services.creator_capabilities import (
    CreatorSfxUnavailableError,
    compile_strategy_to_plan,
    resolve_creator_sfx_catalog_ref,
)
from app.services.sfx_catalog import SFX_CATEGORY_TERMS


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


@pytest.mark.parametrize(
    "creator_text",
    [
        "Include no sound effect.",
        "Don't add a sound effect.",
        "I don't want you to add a sound effect.",
        "Don't add the kids' sound effect.",  # a possessive is not a quote
        "Add a sound effect.",
        "Add any sound effect at the end.",
        "Use another sound effect.",
        "Use a different sound effect.",
        "Add a random sound effect.",
        "Add just one sound effect.",
    ],
)
def test_unnamed_sfx_wording_requests_no_named_effect(creator_text: str) -> None:
    """A stray named effect fails the whole session (on a phone, every one does)."""

    strategy = _apply_explicit_render_intent(
        CreativeStrategy(), creator_text, manifest=_manifest(effects=[])
    )
    assert strategy.licensed_sfx is None


@pytest.mark.parametrize(
    "creator_text",
    [
        # The trailing "no" is what the creator says on camera, not a negation.
        "Add a rejection sound effect when I say no.",
        "No music, add the rejection sound effect.",
        "Make it not boring and add a rejection sound effect.",
        "Please don't use any sound effect, add the Rejection sound effect later.",
    ],
)
def test_named_effect_survives_articles_and_other_clauses(creator_text: str) -> None:
    manifest = _manifest(
        effects=[{"catalog_id": "sfx-rejection", "kind": "sound_effect", "label": "Rejection"}]
    )
    strategy = _apply_explicit_render_intent(CreativeStrategy(), creator_text, manifest=manifest)
    plan = compile_strategy_to_plan(manifest, strategy)
    assert plan.strategy.licensed_sfx.effect_id == "sfx-rejection"


def _fah_and_rejection() -> ResolvedCreatorManifest:
    return _manifest(
        effects=[
            {"catalog_id": "sfx-fah", "kind": "sound_effect", "label": "Fah"},
            {"catalog_id": "sfx-rejection", "kind": "sound_effect", "label": "Rejection"},
        ]
    )


def _phone_manifest() -> ResolvedCreatorManifest:
    manifest = _fah_and_rejection().model_dump(mode="json")
    manifest["capabilities"]["sound_effects"] = {
        "available": False,
        "reason_code": "unsupported_on_phone",
        "reason": "sound_effects cannot render on the iPhone yet",
    }
    return ResolvedCreatorManifest.model_validate(manifest)


@pytest.mark.parametrize(
    "creator_text",
    [
        "Add the sound effect named 'Fah'.",
        "Use the sfx called “Fah” at funny moments.",  # iOS smart quotes
        "Sound effect: ‘Fah’",
        'sfx = "Fah"',
        "Add the ‘Fah’ sound effect.",
        "Add the kids' 'Fah' sound effect.",  # possessive before a real quote
    ],
)
def test_quoted_effect_grammar_names_the_effect(creator_text: str) -> None:
    manifest = _fah_and_rejection()
    strategy = _apply_explicit_render_intent(CreativeStrategy(), creator_text, manifest=manifest)
    plan = compile_strategy_to_plan(manifest, strategy)
    assert plan.strategy.licensed_sfx.effect_id == "sfx-fah"


@pytest.mark.parametrize(
    "creator_text,expected",
    [
        # A blank quoted name is skipped, not returned as an empty request.
        ('Sound effect named " ", add the Fah sfx.', "sfx-fah"),
        # An unnamed request does not hide a later named one.
        ("Add a sound effect. Use the Fah sound effect at the end.", "sfx-fah"),
        ("Use a sound effect, then add the Rejection sound effect.", "sfx-rejection"),
    ],
)
def test_later_named_request_follows_an_unnamed_or_blank_one(
    creator_text: str, expected: str
) -> None:
    manifest = _fah_and_rejection()
    strategy = _apply_explicit_render_intent(CreativeStrategy(), creator_text, manifest=manifest)
    plan = compile_strategy_to_plan(manifest, strategy)
    assert plan.strategy.licensed_sfx.effect_id == expected


@pytest.mark.parametrize(
    "creator_text",
    [
        "Add a sound effect but not Fah.",
        "Use a sound effect, not the Fah sound effect.",
        "Use a sfx, no Fah.",
        "I don't like the Fah, use a different sound effect.",
    ],
)
def test_unnamed_request_beside_a_catalog_name_fails_visibly(creator_text: str) -> None:
    """Never render a declined effect, never drop a named one silently."""

    manifest = _fah_and_rejection()
    strategy = _apply_explicit_render_intent(CreativeStrategy(), creator_text, manifest=manifest)
    assert strategy.licensed_sfx is not None
    assert strategy.licensed_sfx.effect_id != "sfx-fah"
    with pytest.raises(CreatorSfxUnavailableError):
        compile_strategy_to_plan(manifest, strategy)


@pytest.mark.parametrize(
    "creator_text",
    [
        "Include no Fah sound effect.",
        "Add no Fah sound effect please.",
        "Use not the Fah sound effect.",
    ],
)
def test_no_directly_before_a_name_declines_it(creator_text: str) -> None:
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(), creator_text, manifest=_fah_and_rejection()
    )
    assert strategy.licensed_sfx is None


@pytest.mark.parametrize(
    "creator_text",
    [
        "Add the sound effect named Fah.",
        "Add a sound effect called Fah on the punchline.",
        "Add a sound effect: Fah.",
    ],
)
def test_unquoted_catalog_name_after_naming_grammar_names_it(creator_text: str) -> None:
    manifest = _fah_and_rejection()
    strategy = _apply_explicit_render_intent(CreativeStrategy(), creator_text, manifest=manifest)
    plan = compile_strategy_to_plan(manifest, strategy)
    assert plan.strategy.licensed_sfx.effect_id == "sfx-fah"


@pytest.mark.parametrize(
    "creator_text,literal",
    [
        ("Add another one sound effect.", "another one"),
        ("Add a few moments later sound effect.", "a few moments later"),
    ],
)
def test_live_db_lookup_also_tries_the_phrase_as_typed(creator_text: str, literal: str) -> None:
    assert literal in _explicit_sfx_lookup_names(creator_text, manifest=_manifest(effects=[]))


@pytest.mark.asyncio
async def test_generic_word_name_outside_the_prompt_catalog_resolves_from_live_db() -> None:
    """ "Another One" is all generic words; only the live DB can tell it is a name."""

    manifest = _manifest(effects=[])
    effect = SimpleNamespace(
        id="sfx-another-one",
        name="Another One",
        status="ready",
        published_at=datetime.now(UTC),
        archived_at=None,
        audio_gcs_path="sound-effects/another-one.mp3",
    )
    scalar_result = SimpleNamespace(all=lambda: [effect])
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: scalar_result))
    )
    creator_text = "Add another one sound effect when he falls."

    planning_manifest = manifest
    for requested in _explicit_sfx_lookup_names(creator_text, manifest=manifest):
        planning_manifest = await _resolve_explicit_sfx_outside_manifest(
            db, requested, manifest=planning_manifest
        )
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(), creator_text, manifest=planning_manifest
    )
    plan = compile_strategy_to_plan(planning_manifest, strategy)

    assert plan.strategy.licensed_sfx.effect_id == "sfx-another-one"


@pytest.mark.parametrize(
    "creator_text",
    [
        "Don't add music but add the whoosh sound effect.",
        "Add music and add the whoosh sound effect.",
    ],
)
def test_effect_phrase_never_runs_into_the_next_request(creator_text: str) -> None:
    assert _explicit_sfx_name(creator_text, manifest=_manifest(effects=[])) == "whoosh"


@pytest.mark.parametrize(
    "creator_text",
    [
        "Don't forget to add the Fah sound effect.",
        "Don’t forget the Fah sound effect.",
        "Every time I say no add the Fah sound effect.",
        "Why don't you add the Fah sound effect when he falls?",
        "Don't use the Fah sound effect too much.",
        "Drop the Fah sound effect on every punchline.",
        "No music\nAdd the Fah sound effect",
        "Remove my ums add the Fah sound effect at funny moments.",
        "Without music add the Fah sfx.",
        # A generic word before a catalog name is not part of the name.
        "Add that Fah sound effect again.",
        "Use the same Fah sound effect.",
        "Add my Fah sound effect at the end.",
    ],
)
def test_ordinary_wording_around_a_named_effect_keeps_it(creator_text: str) -> None:
    """Without grounding evidence the regex decides: a missed name silently disappears."""

    manifest = _fah_and_rejection()
    strategy = _apply_explicit_render_intent(CreativeStrategy(), creator_text, manifest=manifest)
    plan = compile_strategy_to_plan(manifest, strategy)
    assert plan.strategy.licensed_sfx.effect_id == "sfx-fah"


@pytest.mark.parametrize("label", ["More Cowbell", "Another One", "This Is Fine", "Extra Life"])
def test_label_starting_with_a_generic_word_keeps_its_name(label: str) -> None:
    manifest = _manifest(effects=[{"catalog_id": "sfx-x", "kind": "sound_effect", "label": label}])
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(), f"Add the {label} sound effect.", manifest=manifest
    )
    plan = compile_strategy_to_plan(manifest, strategy)
    assert plan.strategy.licensed_sfx.effect_id == "sfx-x"


def test_catalog_tail_never_shortens_a_specific_name() -> None:
    """ "Vine Boom" stays whole for the live-DB lookup instead of becoming "Boom"."""

    manifest = _manifest(
        effects=[{"catalog_id": "sfx-boom", "kind": "sound_effect", "label": "Boom"}]
    )
    assert _explicit_sfx_name("Add the Vine Boom sound effect.", manifest=manifest) == "Vine Boom"


@pytest.mark.parametrize("name", ["Rock and Roll", "Plus One", "Once More", "So Good"])
def test_uncatalogued_multiword_name_reaches_the_live_db_lookup(name: str) -> None:
    creator_text = f"Add the {name} sound effect."
    assert _explicit_sfx_name(creator_text, manifest=_manifest(effects=[])) == name


@pytest.mark.parametrize(
    "creator_request",
    [
        ("," * 6000 + "no " + ("use " * 38 + "x sfx ") * 200)[:12000],
        ("don't add a sound effect " * 600)[:12000],
        ("don't use the sound effect named 'x' " * 400)[:12000],
    ],
)
def test_sfx_extraction_stays_fast_on_a_capped_history(creator_request: str) -> None:
    manifest = _manifest(
        effects=[
            {"catalog_id": f"sfx-{i}", "kind": "sound_effect", "label": f"Label{i}"}
            for i in range(50)
        ]
    )
    started = time.perf_counter()
    _explicit_sfx_name(creator_request, manifest=manifest)
    assert time.perf_counter() - started < 0.75


@pytest.mark.parametrize(
    "creator_text",
    [
        "The Fah sound effect at funny moments, please.",
        "Sound effect Fah on every punchline.",
    ],
)
def test_catalog_label_near_effect_wording_names_the_effect(creator_text: str) -> None:
    manifest = _fah_and_rejection()
    strategy = _apply_explicit_render_intent(CreativeStrategy(), creator_text, manifest=manifest)
    plan = compile_strategy_to_plan(manifest, strategy)
    assert plan.strategy.licensed_sfx.effect_id == "sfx-fah"


@pytest.mark.parametrize(
    "creator_text",
    [
        "Don't add a sound effect.",
        "Add a sound effect if it fits.",
        "Keep my voice clear and include no sound effect.",
    ],
)
def test_unnamed_effect_wording_compiles_on_phone(creator_text: str) -> None:
    phone_manifest = _phone_manifest()
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(), creator_text, manifest=phone_manifest
    )
    plan = compile_strategy_to_plan(phone_manifest, strategy)
    assert plan.strategy.licensed_sfx is None


@pytest.mark.asyncio
async def test_unnamed_effect_wording_skips_the_live_db_lookup() -> None:
    manifest = _manifest(effects=[])
    db = SimpleNamespace(execute=AsyncMock())

    requested = _explicit_sfx_name("Add a sound effect at the end.", manifest=manifest)
    planning_manifest = await _resolve_explicit_sfx_outside_manifest(
        db, requested, manifest=manifest
    )

    assert requested is None
    assert planning_manifest is manifest
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_determiner_led_effect_name_resolves_from_live_db() -> None:
    """The route passes the extracted name to the DB: "a Fah" never matched."""

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
    creator_text = "Add a Fah sound effect at funny moments."

    requested = _explicit_sfx_name(creator_text, manifest=manifest)
    planning_manifest = await _resolve_explicit_sfx_outside_manifest(
        db, requested, manifest=manifest
    )
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(), creator_text, manifest=planning_manifest
    )
    plan = compile_strategy_to_plan(planning_manifest, strategy)

    assert requested == "Fah"
    assert plan.strategy.licensed_sfx.effect_id == "sfx-fah"


def _model_sfx(effect_id: str | None, *, max_placements: int = 6) -> CreativeStrategy:
    if effect_id is None:
        return CreativeStrategy()
    return CreativeStrategy(licensed_sfx={"effect_id": effect_id, "max_placements": max_placements})


@pytest.mark.parametrize(
    "creator_text,model_effect,max_placements,expected",
    [
        ("Don't add the Fah sound effect.", None, 6, None),
        ("Why don't you add the Fah sound effect?", "sfx-fah", 6, "sfx-fah"),
        ("Don't use the Fah sound effect too much.", "sfx-fah", 2, "sfx-fah"),
        ("I don't like the Fah, use a different sound effect.", None, 6, None),
        ("Skip the Fah sound effect.", None, 6, None),
        ("The Fah sound effect is annoying, drop it.", None, 6, None),
        ("No Fah sound effect this time.", None, 6, None),
    ],
)
def test_grounded_model_reading_decides_a_named_effect(
    creator_text: str, model_effect: str | None, max_placements: int, expected: str | None
) -> None:
    """Negation needs the whole sentence; the model's grounded reading wins over the regex."""

    manifest = _fah_and_rejection()
    strategy = _apply_explicit_render_intent(
        _model_sfx(model_effect, max_placements=max_placements),
        creator_text,
        manifest=manifest,
        render_intent_evidence=CreatorRenderIntentEvidence(licensed_sfx=creator_text),
    )
    plan = compile_strategy_to_plan(manifest, strategy)

    if expected is None:
        assert plan.strategy.licensed_sfx is None
        # A decline compiles on the phone, where every named effect fails.
        assert compile_strategy_to_plan(_phone_manifest(), strategy).strategy.licensed_sfx is None
    else:
        assert plan.strategy.licensed_sfx.effect_id == expected
        assert plan.strategy.licensed_sfx.max_placements == max_placements


@pytest.mark.parametrize(
    "creator_text",
    [
        "Skip the Fah sound effect.",
        "The Fah sound effect is annoying, drop it.",
        "No Fah sound effect this time.",
    ],
)
def test_without_evidence_the_regex_still_decides_an_indirect_decline(creator_text: str) -> None:
    """Old or failed model responses keep the refusal grammar's known limits."""

    strategy = _apply_explicit_render_intent(
        CreativeStrategy(), creator_text, manifest=_fah_and_rejection()
    )
    assert strategy.licensed_sfx.effect_id == "sfx-fah"


def test_latest_turn_evidence_declines_an_earlier_named_effect() -> None:
    latest = "Actually, drop the Fah."
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(),
        f"Add the Fah sound effect at funny moments.\n{latest}",
        manifest=_fah_and_rejection(),
        latest_user_message=latest,
        render_intent_evidence=CreatorRenderIntentEvidence(licensed_sfx=latest),
    )
    assert strategy.licensed_sfx is None


@pytest.mark.parametrize("model_effect", ["Fah", "fah", "sfx-fah"])
def test_model_may_name_the_effect_by_label_or_catalog_id(model_effect: str) -> None:
    creator_text = "Put Fah on every punchline."
    strategy = _apply_explicit_render_intent(
        _model_sfx(model_effect),
        creator_text,
        manifest=_fah_and_rejection(),
        render_intent_evidence=CreatorRenderIntentEvidence(licensed_sfx=creator_text),
    )
    assert strategy.licensed_sfx.effect_id == "sfx-fah"


@pytest.mark.parametrize(
    "creator_text,model_effect,excerpt",
    [
        # The excerpt is not the creator's words.
        ("Don't add the Fah sound effect.", None, "The creator declined Fah."),
        # The model picked an effect the creator never named.
        ("Use a different sound effect.", "sfx-rejection", "Use a different sound effect."),
        # A catalog name that is only part of a longer word is not named.
        ("Add the Fahrenheit sound effect.", "sfx-fah", "Add the Fahrenheit sound effect."),
        # Generic words never name an uncatalogued effect.
        (
            "I don't like the Fah, use a different sound effect.",
            "different",
            "I don't like the Fah, use a different sound effect.",
        ),
    ],
)
def test_ungrounded_model_sfx_falls_back_to_the_literal_recognizer(
    creator_text: str, model_effect: str | None, excerpt: str
) -> None:
    manifest = _fah_and_rejection()
    grounded = _apply_explicit_render_intent(
        _model_sfx(model_effect),
        creator_text,
        manifest=manifest,
        render_intent_evidence=CreatorRenderIntentEvidence(licensed_sfx=excerpt),
    )
    assert grounded == _apply_explicit_render_intent(
        CreativeStrategy(), creator_text, manifest=manifest
    )


def test_grounded_uncatalogued_name_stays_inert_and_fails_visibly() -> None:
    creator_text = "Put the Kazoo sound on every punchline."
    manifest = _fah_and_rejection()
    strategy = _apply_explicit_render_intent(
        _model_sfx("Kazoo"),
        creator_text,
        manifest=manifest,
        render_intent_evidence=CreatorRenderIntentEvidence(licensed_sfx=creator_text),
    )
    assert strategy.licensed_sfx.effect_id == "Kazoo"
    with pytest.raises(CreatorSfxUnavailableError):
        compile_strategy_to_plan(manifest, strategy)


@pytest.mark.asyncio
async def test_model_read_name_outside_the_prompt_catalog_resolves_from_live_db() -> None:
    """No "sound effect" wording, so only the model's grounded name reaches the DB."""

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
    creator_text = "Put Fah on every punchline."
    model_strategy = _model_sfx("Fah")
    evidence = CreatorRenderIntentEvidence(licensed_sfx=creator_text)

    assert _explicit_sfx_lookup_names(creator_text, manifest=manifest) == []
    requested = _model_sfx_lookup_name(
        model_strategy, evidence, _creator_sources(creator_text, creator_text)
    )
    planning_manifest = await _resolve_explicit_sfx_outside_manifest(
        db, requested, manifest=manifest
    )
    strategy = _apply_explicit_render_intent(
        model_strategy,
        creator_text,
        manifest=planning_manifest,
        render_intent_evidence=evidence,
    )
    plan = compile_strategy_to_plan(planning_manifest, strategy)

    assert requested == "Fah"
    assert plan.strategy.licensed_sfx.effect_id == "sfx-fah"


def test_model_lookup_name_must_be_the_creators_words() -> None:
    creator_text = "Add a funny sound effect."
    evidence = CreatorRenderIntentEvidence(licensed_sfx=creator_text)
    sources = _creator_sources(creator_text, creator_text)

    assert _model_sfx_lookup_name(_model_sfx("Fah"), evidence, sources) is None
    assert _model_sfx_lookup_name(_model_sfx(None), evidence, sources) is None


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


def _live_effect(effect_id: str, name: str, category: str, terms: list[str]) -> SimpleNamespace:
    # Mirrors the seed script: explicit terms first, then the category-wide words.
    return SimpleNamespace(
        id=effect_id,
        name=name,
        category=category,
        search_terms=[*terms, *SFX_CATEGORY_TERMS[category], category],
        role_tags=[],
        contains_voice=False,
        quality_tier="library",
        catalog_rank=None,
        created_at=None,
        duration_s=0.4,
    )


_LIVE_LIBRARY = [
    _live_effect("buzz", "Wrong buzzer", "rejection", ["buzzer", "wrong answer", "quiz"]),
    _live_effect("buzz-long", "Wrong buzzer long", "rejection", ["buzzer", "wrong answer"]),
    _live_effect("ding", "Correct ding", "approval", ["ding", "bell", "correct answer"]),
    _live_effect("whoosh", "Whoosh fast", "transition", ["whoosh", "swish"]),
    _live_effect("pop", "Soft pop", "ui", ["pop", "appear"]),
]


async def _plan_requested_sfx(
    request: str,
    *,
    model: CreativeStrategy | None = None,
    evidence: CreatorRenderIntentEvidence | None = None,
):
    """Run the planning turn's SFX steps exactly as ``_run_planning_turn`` does."""

    def execute(statement):
        # Exact name/id lookups filter on lower(...); the library scan doesn't.
        rows = [] if "lower(" in str(statement) else _LIVE_LIBRARY
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    db = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    manifest = _manifest(effects=[])
    model = model or CreativeStrategy()
    model_name = _model_sfx_lookup_name(model, evidence, _creator_sources(request, request))
    planning_manifest = manifest
    for name in [*_explicit_sfx_lookup_names(request, manifest=manifest), model_name]:
        planning_manifest = await _resolve_explicit_sfx_outside_manifest(
            db, name, manifest=planning_manifest
        )
    planning_manifest, described = await _resolve_described_sfx(
        db, _explicit_sfx_name(request, manifest=planning_manifest), manifest=planning_manifest
    )
    planning_manifest, model_described = await _resolve_described_sfx(
        db, model_name, manifest=planning_manifest
    )
    strategy = _apply_explicit_render_intent(
        model,
        request,
        manifest=planning_manifest,
        render_intent_evidence=evidence,
        resolved_sfx=described,
        model_resolved_sfx=model_described,
    )
    assert planning_manifest.manifest_hash == manifest.manifest_hash
    return planning_manifest, strategy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_text", "effect_id"),
    [
        # KRI-173: a described effect reaches the whole library, outside the prompt catalog.
        ("Add a buzzer sound effect when I say the wrong player.", "buzz"),
        ("Use the wrong answer buzzer sound effect.", "buzz"),
        ("Don't use the whoosh sound effect, use a pop sound effect instead.", "pop"),
        ("Don't forget to add a buzzer sound effect.", "buzz"),
    ],
)
async def test_described_effect_resolves_from_the_whole_library(
    request_text: str, effect_id: str
) -> None:
    planning_manifest, strategy = await _plan_requested_sfx(request_text)
    plan = compile_strategy_to_plan(planning_manifest, strategy)
    assert plan.strategy.licensed_sfx.effect_id == effect_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_text",
    [
        "Use no sound effect, just the music.",
        "Don't use the whoosh sound effect.",
        "Don\u2019t add a buzzer sound effect.",
        "Never add a whoosh sound effect.",
        "Please do not include the buzzer sound effect.",
    ],
)
async def test_refused_effect_is_never_placed(request_text: str) -> None:
    _, strategy = await _plan_requested_sfx(request_text)
    assert strategy.licensed_sfx is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_text",
    [
        "Add a trumpet sound effect.",
        # Category-wide words describe a mood, not one effect.
        "Add the right sound effect for each funny moment.",
        "Add a text sound effect.",
    ],
)
async def test_unmatched_description_still_fails_visibly(request_text: str) -> None:
    planning_manifest, strategy = await _plan_requested_sfx(request_text)
    assert strategy.licensed_sfx is not None
    assert strategy.licensed_sfx.effect_id not in {e.id for e in _LIVE_LIBRARY}
    with pytest.raises(CreatorSfxUnavailableError):
        compile_strategy_to_plan(planning_manifest, strategy)


@pytest.mark.asyncio
async def test_model_read_described_effect_resolves_from_the_whole_library() -> None:
    """No "sound effect" wording, so only the model's grounded name is described."""

    request = "Put a buzzer on every miss."
    planning_manifest, strategy = await _plan_requested_sfx(
        request,
        model=_model_sfx("buzzer"),
        evidence=CreatorRenderIntentEvidence(licensed_sfx=request),
    )
    plan = compile_strategy_to_plan(planning_manifest, strategy)
    assert plan.strategy.licensed_sfx.effect_id == "buzz"


@pytest.mark.asyncio
@pytest.mark.parametrize("grounded", [True, False])
async def test_grounded_decline_of_a_described_effect_wins(grounded: bool) -> None:
    request = "I didn't ask you to add a buzzer sound effect."
    _, strategy = await _plan_requested_sfx(
        request,
        evidence=CreatorRenderIntentEvidence(licensed_sfx=request) if grounded else None,
    )
    if grounded:
        assert strategy.licensed_sfx is None
    else:
        # The refusal grammar can't see this decline; the model's reading can.
        assert strategy.licensed_sfx.effect_id == "buzz"
