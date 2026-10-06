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


def test_every_strategy_field_has_an_explicit_accounting_note():
    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.services.creator_render_contract import FIELD_ACCOUNTING

    assert set(FIELD_ACCOUNTING) == set(CreativeStrategy.model_fields)


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


def test_rebind_is_the_only_supported_integrity_preserving_change() -> None:
    contract = build_render_contract({"opening_title": "Hello"}, generation_id="gen-1")
    assert contract is not None
    changed = contract.rebind(generation_id="gen-2")
    assert changed.generation_id == "gen-2"
    assert read_render_contract({CONTRACT_FIELD: changed.model_dump(mode="json")}) == changed
