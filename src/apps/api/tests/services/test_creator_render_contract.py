from __future__ import annotations

import pytest

from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContract,
    CreatorRenderContractError,
    TextRequirement,
    build_render_contract,
    read_render_contract,
    verify_phone_recipe,
)


def _speech_recipe():
    from app.pipeline.phone_speech_montage_plan import (
        PhoneSpeechSection,
        compile_phone_speech_montage_plan,
    )
    from tests.services.test_phone_speech_montage_job import _binding

    return compile_phone_speech_montage_plan(
        (
            PhoneSpeechSection(
                kind="speech",
                speaker=_binding("talk"),
                source_start_s=0,
                source_end_s=7,
                visual="cutaways",
            ),
        ),
        tuple(_binding(f"b{i}", duration_s=9) for i in range(3)),
    )[0]


def test_actual_compiler_accepts_voice_picture_duration_control_and_rejects_mutations():
    recipe = _speech_recipe()
    contract = CreatorRenderContract(generation_id="g").rebind(
        duration_s=7,
        audio_source_ids=("talk",),
        original_audio="require",
        order_required=True,
        order_ids=("b0", "b1", "b2"),
        order_basis="editor",
    )
    assert read_render_contract({CONTRACT_FIELD: contract.model_dump(mode="json")}) == contract
    assert verify_phone_recipe(contract, recipe, source_audio={"talk": True})
    for change in (
        {"duration_s": 30},
        {"audio_source_ids": ("b0",)},
        {"original_audio": "forbid"},
        {"order_ids": ("b2", "b1", "b0")},
        {"order_ids": ()},
    ):
        with pytest.raises(CreatorRenderContractError):
            verify_phone_recipe(contract.rebind(**change), recipe, source_audio={"talk": True})


def test_partial_mute_does_not_prove_camera_audio_disabled():
    from app.kria.recipes import AudioMuteWindow

    recipe = _speech_recipe()
    clip = next(t for t in recipe.tracks if t.kind == "audio").clips[0]
    recipe.audio.mute_windows = [AudioMuteWindow(start=0, end=0.1, clip_ids=[clip.id])]
    contract = CreatorRenderContract(generation_id="g").rebind(original_audio="forbid")
    with pytest.raises(CreatorRenderContractError, match="camera audio"):
        verify_phone_recipe(contract, recipe)
    recipe.audio.mute_windows = [AudioMuteWindow(start=0, end=7, clip_ids=[clip.id])]
    assert verify_phone_recipe(contract, recipe)


@pytest.mark.parametrize(
    "role,media_id,start,end",
    [
        ("opening", None, 1, 2),
        ("closing", None, 0, 1),
        ("clip", "absent", 0, 1),
    ],
)
def test_exact_words_at_wrong_position_do_not_satisfy_text(role, media_id, start, end):
    from app.kria.recipes_v2 import EditRecipeV2
    from tests.kria.test_portable_text import text_document

    doc = text_document()
    doc["text_layers"][0].update(start=start, end=end)
    recipe = EditRecipeV2.model_validate(doc)
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role=role, media_id=media_id, text="Hello world"),)
    )
    with pytest.raises(CreatorRenderContractError, match="text"):
        verify_phone_recipe(contract, recipe)


def test_chronology_cannot_be_proven_by_upload_order_or_partial_timestamps():
    for snapshot in (
        None,
        {
            "clip_assignments": [
                {"media_id": "new", "capture": {"captured_at": "2026-10-06T12:00:00Z"}},
                {"media_id": "old", "capture": None},
            ]
        },
    ):
        contract = build_render_contract(
            {"ordering_choice": "chronological"},
            generation_id="g",
            clip_order=("new", "old"),
            media_snapshot=snapshot,
        )
        assert contract.unresolved


def test_chronology_uses_complete_capture_evidence_in_utc():
    contract = build_render_contract(
        {"ordering_choice": "chronological"},
        generation_id="g",
        media_snapshot={
            "clip_assignments": [
                {"media_id": "later", "capture": {"capture_time": "2026-10-06T10:00:00Z"}},
                {"media_id": "earlier", "capture": {"capture_time": "2026-10-06T11:00:00+02:00"}},
            ]
        },
    )
    assert contract.order_ids == ("earlier", "later")
    assert contract.order_basis == "capture_time"
    assert not contract.unresolved


def test_shot_specific_brief_does_not_turn_one_label_into_a_label_on_every_clip():
    from app.kria.brief import BriefRequirement, CreativeBrief

    contract = build_render_contract(
        {"shot_labels": ["First place", "Second place"]},
        generation_id="g",
        brief=CreativeBrief(
            requirements=[
                BriefRequirement(
                    id="r1",
                    kind="text",
                    scope="per_clip",
                    literal="First place",
                    description="on the bookshop photo",
                )
            ]
        ),
    )
    assert not contract.unresolved
    assert all(t.shot_index == 0 for t in contract.exact_texts if t.text == "First place")


def test_recording_inventory_does_not_override_approved_soundtrack():
    contract = build_render_contract(
        {"audio_strategy": "original_audio"}, generation_id="g", has_voiceover=True
    )
    assert contract.require_voiceover is False


# --- KRI-470 PR-A: field matrix -------------------------------------------------
#
# Failure modes the guard must catch (written before the code):
#   1. a new top-level CreativeStrategy field with no matrix entry;
#   2. a new NESTED field (inside a list/optional/union-typed sub-model) with no entry;
#   3. a stale entry for a path the schema no longer has;
#   4. an entry with an unknown disposition or an empty owner;
#   5. the schema walk silently skipping nested models behind Optional/list/Annotated.


def test_every_strategy_path_is_assigned():
    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.services.creator_render_contract import FIELD_MATRIX, schema_field_paths

    required = schema_field_paths(CreativeStrategy)
    assert set(FIELD_MATRIX) == required, (
        f"unassigned: {sorted(required - set(FIELD_MATRIX))}; "
        f"stale: {sorted(set(FIELD_MATRIX) - required)}"
    )


def test_schema_walk_reaches_nested_models_behind_optional_list_and_annotated():
    from app.services.creator_render_contract import schema_field_paths

    paths = schema_field_paths()
    for expected in (
        "target_duration_s",
        "shot_labels[]",
        "clip_intents[].op",
        "resolved_clip_intents[].assignments[].media_id",
        "reaction_beats[].trigger",
        "closing_media.visual_id",
        "montage_audio.source_media_ids[]",
        "mixed_media_timing.image_hold_s",
        "licensed_sfx.effect_id",
    ):
        assert expected in paths


def test_a_new_top_level_field_fails_the_guard_for_exactly_that_path():
    from pydantic import create_model

    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.services.creator_render_contract import FIELD_MATRIX, schema_field_paths

    mutated = create_model(
        "MutatedStrategy", __base__=CreativeStrategy, brand_new_field=(int | None, None)
    )
    assert schema_field_paths(mutated) - set(FIELD_MATRIX) == {"brand_new_field"}


def test_a_new_nested_field_fails_the_guard_for_exactly_that_path():
    from pydantic import create_model

    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.schemas.edit_proposal import MontageAudioPlan
    from app.services.creator_render_contract import FIELD_MATRIX, schema_field_paths

    audio = create_model("MutatedAudio", __base__=MontageAudioPlan, brand_new=(int, 0))
    mutated = create_model(
        "MutatedStrategy", __base__=CreativeStrategy, montage_audio=(audio | None, None)
    )
    assert schema_field_paths(mutated) - set(FIELD_MATRIX) == {"montage_audio.brand_new"}


def test_matrix_dispositions_are_valid_and_owned():
    from typing import get_args

    from app.services.creator_render_contract import FIELD_MATRIX, Disposition

    valid = set(get_args(Disposition))
    for path, rule in FIELD_MATRIX.items():
        assert rule.disposition in valid, path
        assert rule.owner.strip(), path


# strategy fragment that sets each `supported` path -> it must change the contract.
_SUPPORTED_FRAGMENTS = {
    "target_duration_s": {"target_duration_s": 30, "target_duration_requested": True},
    "target_duration_requested": {"target_duration_s": 24, "target_duration_requested": True},
    "audio_strategy": {"audio_strategy": "voiceover"},
    "montage_audio": {"montage_audio": {"preserve_source_audio": False}},
    "montage_audio.preserve_source_audio": {"montage_audio": {"preserve_source_audio": False}},
    "montage_audio.source_media_ids[]": {
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": ["a"]}
    },
    "opening_title": {"opening_title": "Title"},
    "opening_title_duration_s": {"opening_title": "Title", "opening_title_duration_s": 3},
    "shot_labels[]": {"shot_labels": ["One"]},
    "closing_title": {"closing_title": "End"},
    "ordering_choice": {"ordering_choice": "chronological"},
}
# fragments for fields that are NOT supported -> the contract must ignore them.
_UNSUPPORTED_FRAGMENTS = {
    "pacing": {"pacing": "fast"},
    "caption_style": {"caption_style": "kinetic"},
    "intro_hook": {"intro_hook": "A hook"},
    "rationale": {"rationale": "why"},
    "story_structure[]": {"story_structure": ["a"]},
    "optional_treatments[]": {"optional_treatments": ["sfx"]},
    "overlay_display": {"overlay_display": "fullscreen"},
    "selected_media_ids[]": {"selected_media_ids": ["a", "b"]},
    "hero_media_id": {"hero_media_id": "a"},
    "licensed_sfx.effect_id": {"licensed_sfx": {"effect_id": "sfx-1"}},
}


def _projection(strategy):
    contract = build_render_contract(strategy, generation_id="g")
    data = contract.model_dump(mode="json")
    data.pop("strategy_digest")
    data.pop("digest")
    return data


def test_supported_paths_are_exactly_the_ones_that_change_the_contract():
    """`supported` is behaviour: the projection reacts to it. Accounting is not enforcement."""
    from app.services.creator_render_contract import FIELD_MATRIX

    supported = {p for p, r in FIELD_MATRIX.items() if r.disposition == "supported"}
    assert supported == set(_SUPPORTED_FRAGMENTS)
    baseline = _projection({})
    for path, fragment in _SUPPORTED_FRAGMENTS.items():
        base = (
            _projection({"target_duration_s": 24, "target_duration_requested": False})
            if path == "target_duration_requested"
            else baseline
        )
        assert _projection(fragment) != base, path


def test_supported_fields_pin_only_the_values_their_note_names():
    baseline = _projection({})
    assert _projection({"audio_strategy": "licensed_music"}) == baseline
    assert _projection({"ordering_choice": "group_first"}) == baseline
    assert _projection({"target_duration_s": 30, "target_duration_requested": False}) == baseline


def test_unsupported_preference_and_upstream_fields_do_not_reach_the_contract():
    from app.services.creator_render_contract import FIELD_MATRIX

    baseline = _projection({})
    for path, fragment in _UNSUPPORTED_FRAGMENTS.items():
        assert FIELD_MATRIX[path].disposition != "supported", path
        assert _projection(fragment) == baseline, path


# --- KRI-470 PR-A: stored-v1 digest compatibility ---------------------------------

# A real stored v1 contract, captured from build_render_contract before any
# post-v1 field existed. It must keep reading no matter what fields are added.
STORED_V1_CONTRACT = {
    "version": 1,
    "generation_id": "gen-golden",
    "strategy_digest": "71b386b74b1177a7d3ce243f32c04d4109d64cbd1485481964f3cefd24d8261c",
    "brief_digest": None,
    "duration_s": 30.0,
    "audio_source_ids": ["talk"],
    "original_audio": "require",
    "require_voiceover": False,
    "exact_texts": [
        {
            "role": "opening",
            "text": "Exact title",
            "media_id": None,
            "shot_index": None,
            "duration_s": None,
        },
        {
            "role": "closing",
            "text": "The end",
            "media_id": None,
            "shot_index": None,
            "duration_s": None,
        },
        {"role": "clip", "text": "One", "media_id": None, "shot_index": 0, "duration_s": None},
        {"role": "clip", "text": "Two", "media_id": None, "shot_index": 1, "duration_s": None},
    ],
    "order_ids": ["b", "a"],
    "order_required": True,
    "order_basis": "capture_time",
    "unresolved": [],
    "digest": "d4873da9b130cd602146440cbc93c52b9c539feddf0ea2111d1638bdcdf8a6e3",
}


def test_stored_v1_contract_still_reads():
    contract = read_render_contract({CONTRACT_FIELD: dict(STORED_V1_CONTRACT)})
    assert contract is not None
    assert contract.digest == STORED_V1_CONTRACT["digest"]
    assert contract.order_ids == ("b", "a")


def test_a_post_v1_defaulted_field_does_not_change_a_stored_v1_digest(monkeypatch):
    """Throwaway added field: the skip mechanism keeps stored contracts readable."""
    from app.services import creator_render_contract as module

    class WithNewField(CreatorRenderContract):
        plan_marker: str | None = None

    monkeypatch.setattr(module, "CreatorRenderContract", WithNewField)
    monkeypatch.setitem(module._POST_V1_FIELD_DEFAULTS, "plan_marker", None)

    contract = read_render_contract({CONTRACT_FIELD: dict(STORED_V1_CONTRACT)})
    assert isinstance(contract, WithNewField)
    assert contract.digest == STORED_V1_CONTRACT["digest"]
    # A contract written after the field exists and left at its default keeps the
    # v1 digest; setting it is covered by the integrity digest.
    assert contract.rebind().digest == STORED_V1_CONTRACT["digest"]
    marked = contract.rebind(plan_marker="authority-1")
    assert marked.digest != STORED_V1_CONTRACT["digest"]
    assert read_render_contract({CONTRACT_FIELD: marked.model_dump(mode="json")}) == marked
    tampered = {**marked.model_dump(mode="json"), "plan_marker": "other"}
    with pytest.raises(CreatorRenderContractError, match="changed"):
        read_render_contract({CONTRACT_FIELD: tampered})


def test_without_the_skip_registration_a_new_field_would_break_stored_contracts(monkeypatch):
    """Guards the guard: an unregistered added field is exactly the in-flight-job break."""
    from app.services import creator_render_contract as module

    class WithNewField(CreatorRenderContract):
        plan_marker: str | None = None

    monkeypatch.setattr(module, "CreatorRenderContract", WithNewField)
    with pytest.raises(CreatorRenderContractError, match="changed"):
        read_render_contract({CONTRACT_FIELD: dict(STORED_V1_CONTRACT)})


# --- KRI-470 PR-A: typed declines through the real entry points ---------------------
#
# Failure modes: (a) a verifier refusal loses its reason/field path; (b) the declared
# adapter table says one reason while the real entry point raises another; (c) an
# adapter leaves a contract requirement unaccounted for; (d) an untyped error is
# mistaken for a typed one.


def _typed(exc):
    return exc.value.decline_reason, exc.value.field_path


def _phone_cases():
    """requirement -> (contract changes, recipe mutator or None, expected field path)."""

    def no_text(_recipe):
        return None

    return {
        "duration_s": ({"duration_s": 30}, "target_duration_s"),
        "require_voiceover": ({"require_voiceover": True}, "audio_strategy"),
        "audio_source_ids": ({"audio_source_ids": ("b0",)}, "montage_audio.source_media_ids[]"),
        "original_audio": ({"original_audio": "forbid"}, "montage_audio.preserve_source_audio"),
        "exact_texts": (
            {"exact_texts": (TextRequirement(role="opening", text="Not on screen"),)},
            "opening_title",
        ),
        "order_required": (
            {"order_required": True, "order_ids": ("b2", "b1", "b0")},
            "ordering_choice",
        ),
    }


@pytest.mark.parametrize("requirement", sorted(_phone_cases()))
def test_phone_verifier_refuses_with_typed_missing_evidence(requirement):
    changes, path = _phone_cases()[requirement]
    contract = CreatorRenderContract(generation_id="g").rebind(**changes)
    with pytest.raises(CreatorRenderContractError) as exc:
        verify_phone_recipe(contract, _speech_recipe(), source_audio={"talk": True})
    assert _typed(exc) == ("evidence_missing", path)
    assert exc.value.alternative


@pytest.mark.parametrize(
    "role,path",
    [
        ("opening", "opening_title"),
        ("closing", "closing_title"),
        ("clip", "shot_labels[]"),
        ("any", "brief:text"),
    ],
)
def test_text_declines_name_the_field_the_text_came_from(role, path):
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role=role, text="Not on screen"),)
    )
    with pytest.raises(CreatorRenderContractError) as exc:
        verify_phone_recipe(contract, _speech_recipe())
    assert _typed(exc) == ("evidence_missing", path)


def test_unresolved_requirements_decline_as_a_choice():
    contract = CreatorRenderContract(generation_id="g").rebind(unresolved=("Pick an order.",))
    with pytest.raises(CreatorRenderContractError) as exc:
        verify_phone_recipe(contract, _speech_recipe())
    assert exc.value.decline_reason == "needs_choice"
    assert str(exc.value) == "Pick an order."


def test_conflicting_confirmed_durations_are_a_requirement_conflict():
    from app.kria.brief import BriefRequirement, CreativeBrief

    brief = CreativeBrief(
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="whole_video",
                facts={"duration_s": 15},
                description="15 seconds",
            )
        ]
    )
    with pytest.raises(CreatorRenderContractError) as exc:
        build_render_contract(
            {"target_duration_s": 30, "target_duration_requested": True},
            generation_id="g",
            brief=brief,
        )
    assert _typed(exc) == ("requirement_conflict", "target_duration_s")


def test_dispatch_gates_are_typed_and_return_the_contract_voice_route():
    from app.services.creator_render_contract import check_phone_dispatch_contract

    base = CreatorRenderContract(generation_id="g").rebind()
    assert (
        check_phone_dispatch_contract(
            base, snapshot_generation_id="g", has_voiceover_candidate=True
        )
        is False
    ), "a recording's presence never overrides the approved soundtrack"
    voice = base.rebind(require_voiceover=True)
    assert check_phone_dispatch_contract(
        voice, snapshot_generation_id="g", has_voiceover_candidate=True
    )
    cases = [
        (base, {"snapshot_generation_id": "other"}, "requirement_conflict", None),
        (
            base.rebind(unresolved=("Pick one.",)),
            {"snapshot_generation_id": "g"},
            "needs_choice",
            None,
        ),
        (voice, {"snapshot_generation_id": "g"}, "needs_choice", "audio_strategy"),
        (
            voice.rebind(audio_source_ids=("talk",)),
            {"snapshot_generation_id": "g", "has_voiceover_candidate": True},
            "requirement_conflict",
            "montage_audio.source_media_ids[]",
        ),
        (
            base.rebind(audio_source_ids=("talk",)),
            {"snapshot_generation_id": "g", "user_song": {"gcs_path": "x"}},
            "requirement_conflict",
            "montage_audio.source_media_ids[]",
        ),
    ]
    for contract, kwargs, reason, path in cases:
        kwargs = {"has_voiceover_candidate": False, **kwargs}
        with pytest.raises(CreatorRenderContractError) as exc:
            check_phone_dispatch_contract(contract, **kwargs)
        assert _typed(exc) == (reason, path)


def test_untyped_errors_carry_no_decline_payload():
    from app.services.creator_render_contract import decline_payload

    assert decline_payload(CreatorRenderContractError("plain")) == {}
    assert decline_payload(ValueError("plain")) == {}
    assert decline_payload(
        CreatorRenderContractError("x", decline_reason="needs_choice", field_path="a.b")
    ) == {"decline_reason": "needs_choice", "field_path": "a.b"}


# --- KRI-470 PR-A: adapter declarations --------------------------------------------


def test_every_adapter_accounts_for_every_requirement_exactly_once():
    from app.services.cloud_render_contract import CLOUD_ADAPTER_DECLARATIONS
    from app.services.creator_render_contract import (
        ADAPTER_DECLARATIONS,
        CONTRACT_REQUIREMENTS,
        AdapterDeclaration,
        Decline,
    )

    names = set(ADAPTER_DECLARATIONS) | set(CLOUD_ADAPTER_DECLARATIONS)
    assert names == {
        "phone_speech_montage",
        "phone_guided_unified_montage",
        "phone_voiceover_montage",
        "phone_subtitled",
        "phone_narrated",
        "cloud_guided_story",
        "cloud_classic",
        "cloud_slides",
    }
    for declaration in {**ADAPTER_DECLARATIONS, **CLOUD_ADAPTER_DECLARATIONS}.values():
        assert set(declaration.consumes) | set(declaration.declines) == set(CONTRACT_REQUIREMENTS)
        assert not set(declaration.consumes) & set(declaration.declines)
    with pytest.raises(ValueError, match="consumed or declined"):
        AdapterDeclaration(
            "partial", frozenset({"duration_s"}), {"unresolved": Decline("needs_choice")}
        )


def test_speech_montage_is_the_adapter_that_consumes_the_audio_contract():
    from app.services.creator_render_contract import ADAPTER_DECLARATIONS

    speech = ADAPTER_DECLARATIONS["phone_speech_montage"]
    assert {"duration_s", "audio_source_ids", "original_audio", "order_required"} <= set(
        speech.consumes
    )
    for other in ("phone_guided_unified_montage", "phone_subtitled"):
        assert "audio_source_ids" not in ADAPTER_DECLARATIONS[other].consumes


@pytest.mark.parametrize(
    "adapter",
    [
        "phone_speech_montage",
        "phone_guided_unified_montage",
        "phone_voiceover_montage",
        "phone_subtitled",
        "phone_narrated",
    ],
)
def test_declared_phone_declines_match_the_real_entry_points(adapter):
    """The declaration must agree with what the real entry point raises.

    Expected reasons are literals, independent of the table the code reads.
    """
    from app.services.creator_render_contract import (
        ADAPTER_DECLARATIONS,
        check_phone_dispatch_contract,
    )

    # What only the worker dispatcher / dispatch gate raises (not the pin verifier).
    gate_declines = {
        ("phone_speech_montage", "require_voiceover"): "requirement_conflict",
        ("phone_voiceover_montage", "audio_source_ids"): "requirement_conflict",
        ("phone_narrated", "audio_source_ids"): "requirement_conflict",
        ("phone_guided_unified_montage", "audio_source_ids"): "capability_unavailable",
    }
    declaration = ADAPTER_DECLARATIONS[adapter]
    cases = _phone_cases()
    for requirement, decline in declaration.declines.items():
        key = (adapter, requirement)
        if requirement == "unresolved":
            expected = "needs_choice"
            contract = CreatorRenderContract(generation_id="g").rebind(unresolved=("Pick.",))
            with pytest.raises(CreatorRenderContractError) as exc:
                verify_phone_recipe(contract, _speech_recipe())
            assert exc.value.decline_reason == expected
        elif key in gate_declines:
            expected = gate_declines[key]
            if expected == "requirement_conflict":
                contract = CreatorRenderContract(generation_id="g").rebind(
                    require_voiceover=True, audio_source_ids=("talk",)
                )
                with pytest.raises(CreatorRenderContractError) as exc:
                    check_phone_dispatch_contract(
                        contract, snapshot_generation_id="g", has_voiceover_candidate=True
                    )
                assert exc.value.decline_reason == expected
            # capability_unavailable fires in the worker dispatcher:
            # tests/tasks/test_unified_montage_dispatch.py
        else:
            expected = "evidence_missing"
            changes, _path = cases[requirement]
            contract = CreatorRenderContract(generation_id="g").rebind(**changes)
            with pytest.raises(CreatorRenderContractError) as exc:
                verify_phone_recipe(contract, _speech_recipe(), source_audio={"talk": True})
            assert exc.value.decline_reason == expected
        assert decline.reason == expected, (adapter, requirement)


def test_absent_strategy_and_brief_preserves_legacy() -> None:
    assert build_render_contract(None, generation_id="gen-1") is None


def test_explicit_opening_title_is_a_pinned_requirement() -> None:
    contract = build_render_contract(
        {
            "opening_title": "  Hello ",
            "target_duration_s": 30,
            "target_duration_requested": True,
        },
        generation_id="gen-1",
    )
    assert contract is not None
    assert contract.duration_s == 30
    assert [(item.role, item.text) for item in contract.exact_texts] == [("opening", "Hello")]
    assert contract.model_dump(mode="json")["digest"]


def test_default_duration_does_not_create_a_requirement() -> None:
    contract = build_render_contract(
        {"target_duration_s": 24, "target_duration_requested": False}, generation_id="gen-1"
    )
    assert contract is not None
    assert contract.duration_s is None


def test_tampered_persisted_contract_is_refused() -> None:
    contract = build_render_contract({"opening_title": "Hello"}, generation_id="gen-1")
    assert contract is not None
    assembly = {CONTRACT_FIELD: {**contract.model_dump(mode="json"), "generation_id": "other"}}
    with pytest.raises(CreatorRenderContractError, match="changed"):
        read_render_contract(assembly)


def test_multiline_and_karaoke_runs_reconstruct_confirmed_words() -> None:
    from app.kria.recipes_v2 import EditRecipeV2
    from tests.kria.test_portable_text import text_document

    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role="any", text="Hello world"),)
    )
    multiline = text_document()
    run = multiline["text_layers"][0]["runs"][0]
    multiline["text_layers"][0]["runs"] = [
        {**run, "text": "Hello", "baseline_y": 900},
        {**run, "text": "world", "baseline_y": 980},
    ]
    assert verify_phone_recipe(contract, EditRecipeV2.model_validate(multiline))

    karaoke = text_document()
    run = karaoke["text_layers"][0]["runs"][0]
    karaoke["text_layers"][0].update(
        effect="karaoke-line",
        karaoke={"starts": [0.2, 0.5], "highlight": run["fill"], "active_only": True},
        runs=[{**run, "text": "Hello"}, {**run, "text": "world", "x": 500}],
    )
    assert verify_phone_recipe(contract, EditRecipeV2.model_validate(karaoke))


def test_same_line_styled_spans_do_not_gain_an_invented_space() -> None:
    from app.kria.recipes_v2 import EditRecipeV2
    from tests.kria.test_portable_text import text_document

    doc = text_document()
    run = doc["text_layers"][0]["runs"][0]
    doc["text_layers"][0]["runs"] = [{**run, "text": "Hello"}, {**run, "text": "world", "x": 500}]
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role="any", text="Helloworld"),)
    )
    assert verify_phone_recipe(contract, EditRecipeV2.model_validate(doc))


@pytest.mark.parametrize("effect", ["static", "karaoke-line"])
def test_actual_text_compiler_preserves_exact_words_across_run_boundaries(effect):
    from app.kria.recipes_v2 import EditRecipeV2
    from app.pipeline.canvas import Canvas
    from app.pipeline.portable_text_layout import compile_text_overlay
    from tests.kria.test_portable_text import text_document

    layer, _font = compile_text_overlay(
        {
            "text": "Hello\nworld",
            "effect": effect,
            "start_s": 0.2,
            "end_s": 1,
            "font_family": "Inter",
            "text_size_px": 36,
            "text_color": "#FFFFFF",
            "word_timings": [
                {"text": "Hello", "start_s": 0, "end_s": 0.3},
                {"text": "world", "start_s": 0.3, "end_s": 0.7},
            ]
            if effect == "karaoke-line"
            else [],
        },
        layer_id="caption",
        canvas=Canvas(1080, 1920),
    )
    assert len(layer.runs) == 2
    doc = text_document()
    # Bind the real layout's font references to the fixture's admitted font.
    rendered = layer.model_dump(mode="json")
    for run in rendered["runs"]:
        run["font_asset_id"] = "font"
    doc["text_layers"] = [rendered]
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role="any", text="Hello world"),)
    )
    assert verify_phone_recipe(contract, EditRecipeV2.model_validate(doc))


def test_one_visible_word_cannot_prove_a_hidden_word():
    from app.kria.recipes_v2 import EditRecipeV2
    from tests.kria.test_portable_text import text_document

    doc = text_document()
    run = doc["text_layers"][0]["runs"][0]
    transparent = {**run["fill"], "alpha": 0}
    doc["text_layers"][0]["runs"] = [
        {**run, "text": "Hello", "baseline_y": 900},
        {
            **run,
            "text": "world",
            "baseline_y": 980,
            "fill": transparent,
            "stroke": transparent,
            "stroke_width": 0,
        },
    ]
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role="any", text="Hello world"),)
    )
    with pytest.raises(CreatorRenderContractError, match="missing"):
        verify_phone_recipe(contract, EditRecipeV2.model_validate(doc))


def test_fully_transparent_text_cannot_satisfy_exact_requirement() -> None:
    from app.kria.recipes_v2 import EditRecipeV2
    from tests.kria.test_portable_text import text_document

    doc = text_document()
    run = doc["text_layers"][0]["runs"][0]
    transparent = {**run["fill"], "alpha": 0}
    run.update(fill=transparent, stroke=transparent, stroke_width=0)
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role="any", text="Hello world"),)
    )
    with pytest.raises(CreatorRenderContractError, match="missing"):
        verify_phone_recipe(contract, EditRecipeV2.model_validate(doc))


def test_rebind_is_the_only_supported_integrity_preserving_change() -> None:
    contract = build_render_contract({"opening_title": "Hello"}, generation_id="gen-1")
    assert contract is not None
    changed = contract.rebind(generation_id="gen-2")
    assert changed.generation_id == "gen-2"
    assert read_render_contract({CONTRACT_FIELD: changed.model_dump(mode="json")}) == changed


# -- song-order answer on a user-song (lip-sync) item -------------------------

_SONG_IDS = [f"m{i}.mp4" for i in range(1, 8)]


def _song_strategy():
    return {
        "audio_strategy": "user_song",
        "song_sync": "lipsync",
        "target_duration_s": 62,
        "target_duration_requested": True,
        "resolved_song_takes": [
            {"media_id": mid, "place": "pinned", "order_index": i, "delta_s": float(i)}
            for i, mid in enumerate(_SONG_IDS)
        ],
    }


def _order_brief(literal="clips 1, 2, 3, 4, 5, 7, 6", facts=None):
    from app.kria.brief import BriefRequirement, CreativeBrief

    return CreativeBrief(
        requirements=[
            BriefRequirement(
                id="r1", kind="order", scope="global", literal=literal, facts=facts or {}
            )
        ]
    )


def test_song_order_answer_on_lipsync_item_is_not_an_unresolved_order_rule():
    """Prod job 7deecc98: "Use this order: clips ..." became an `order` requirement and
    declined the item (needs_choice). The song placement owns that order."""
    from app.services.creator_render_contract import check_phone_dispatch_contract

    contract = build_render_contract(
        _song_strategy(), generation_id="g", brief=_order_brief(), media_snapshot={}
    )
    assert contract is not None
    assert contract.unresolved == ()
    assert contract.order_required is False
    assert contract.order_ids == ()
    # The model-emitted 62 s is not a creator request (prod jobs 887b683a / 30f1c7d2).
    assert contract.duration_s is None
    check_phone_dispatch_contract(
        contract, snapshot_generation_id="g", has_voiceover_candidate=False, user_song=object()
    )


def test_explicit_capture_order_on_song_item_is_still_enforced():
    contract = build_render_contract(
        _song_strategy(),
        generation_id="g",
        brief=_order_brief("oldest first", {"key": "capture_time"}),
        media_snapshot={},
    )
    assert contract.order_required is True
    assert contract.unresolved  # no capture times -> still needs evidence


def test_plain_order_on_non_song_item_still_declines_needs_choice():
    from app.services.creator_render_contract import check_phone_dispatch_contract

    contract = build_render_contract(
        {"target_duration_s": 30}, generation_id="g", brief=_order_brief(), media_snapshot={}
    )
    assert contract.order_required is True
    assert contract.unresolved
    with pytest.raises(CreatorRenderContractError) as exc:
        check_phone_dispatch_contract(
            contract, snapshot_generation_id="g", has_voiceover_candidate=False
        )
    assert exc.value.decline_reason == "needs_choice"


def test_song_order_on_song_item_without_resolved_takes_still_declines():
    strategy = _song_strategy()
    strategy["resolved_song_takes"] = None
    contract = build_render_contract(
        strategy, generation_id="g", brief=_order_brief(), media_snapshot={}
    )
    assert contract.unresolved


# -- model-chosen target_duration_s is not a creator-requested length ----------


def _timing_brief(seconds):
    from app.kria.brief import BriefRequirement, CreativeBrief

    return CreativeBrief(
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="whole_video",
                facts={"duration_s": seconds},
                description=f"{seconds} seconds",
            )
        ]
    )


def test_unrequested_model_duration_does_not_gate_a_lipsync_item():
    """Prod jobs 887b683a (62 s) / 30f1c7d2 (71 s): brief had no timing requirement, the
    model always emits a target, the 14.5 s / 39.6 s song-window recipe was declined."""
    contract = build_render_contract(
        _song_strategy(), generation_id="g", brief=_order_brief(), media_snapshot={}
    )
    assert contract is not None and contract.duration_s is None
    recipe = _speech_recipe()  # 7 s; would fail a 62 s pin by far more than 10%
    assert verify_phone_recipe(contract, recipe, source_audio={"talk": True}) is not None


def test_unrequested_model_duration_does_not_gate_a_subtitled_item_with_a_brief():
    strategy = {
        "edit_format": "subtitled",
        "target_duration_s": 40,
        "target_duration_requested": True,
    }
    contract = build_render_contract(
        strategy, generation_id="g", brief=_order_brief(facts={}), media_snapshot={}
    )
    assert contract is None or contract.duration_s is None


def test_explicit_brief_duration_on_non_song_item_is_still_enforced():
    contract = build_render_contract(
        {"target_duration_s": 30, "target_duration_requested": True},
        generation_id="g",
        brief=_timing_brief(30),
    )
    assert contract is not None and contract.duration_s == 30
    with pytest.raises(CreatorRenderContractError) as exc:
        verify_phone_recipe(contract, _speech_recipe(), source_audio={"talk": True})
    assert _typed(exc)[1] == "target_duration_s"


def test_brief_less_non_song_strategy_duration_keeps_legacy_behaviour():
    contract = build_render_contract(
        {"target_duration_s": 30, "target_duration_requested": True}, generation_id="g"
    )
    assert contract is not None and contract.duration_s == 30


def test_explicit_brief_duration_on_song_item_is_pinned_from_the_brief():
    strategy = {**_song_strategy(), "target_duration_s": 30}
    contract = build_render_contract(
        strategy, generation_id="g", brief=_timing_brief(30), media_snapshot={}
    )
    assert contract is not None and contract.duration_s == 30
