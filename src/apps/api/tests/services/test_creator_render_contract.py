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


@pytest.mark.parametrize(
    "edit_format", ["subtitled", "talking_head", "narrated", "narrated_planned"]
)
def test_a_take_length_format_never_pins_a_requested_length(edit_format: str) -> None:
    """Job e1c5f89e (2026-10-06): a 68 s Talking take approved with "keep it under
    45 seconds" was refused at the pin step. A Talking or voiceover edit runs as
    long as the take or the voiceover, so the length is not a provable fact."""
    from app.kria.brief import BriefRequirement, CreativeBrief

    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r3",
                kind="timing",
                scope="global",
                description="cut out the long pauses",
                facts={"duration_s": 45},
            )
        ],
    )
    contract = build_render_contract(
        {
            "edit_format": edit_format,
            "opening_title": "3 sourdough mistakes",
            "target_duration_s": 40,
            "target_duration_requested": True,
        },
        generation_id="gen-1",
        brief=brief,
    )
    assert contract is not None
    assert contract.duration_s is None
    assert [item.text for item in contract.exact_texts] == ["3 sourdough mistakes"]


def test_a_montage_still_pins_its_requested_length() -> None:
    contract = build_render_contract(
        {"edit_format": "montage", "target_duration_s": 30, "target_duration_requested": True},
        generation_id="gen-1",
    )
    assert contract is not None
    assert contract.duration_s == 30


def test_the_sourdough_talking_recipe_passes_its_pinned_contract() -> None:
    """The exact prod shape: a 68 s single-clip Talking recipe, strategy target 40 s."""
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
    from app.pipeline.phone_subtitled_title import talking_title_element
    from tests.pipeline.test_phone_subtitled_plan import _binding

    binding = _binding(duration_s=68.0)
    title = talking_title_element(
        "3 sourdough mistakes",
        duration_s=2.0,
        first_word_end_s=None,
        timeline_duration_s=68.0,
        canvas=type("Canvas", (), {"width": 1080, "height": 1920})(),
    )
    recipe = compile_phone_subtitled_plan(
        (binding,),
        caption_cues=[{"text": "Hello", "start_s": 0.2, "end_s": 1.0}],
        text_elements=[title] if title else [],
        text_elements_user_edited=True,
    )
    contract = build_render_contract(
        {
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "render_program": "native",
            "caption_style": "karaoke",
            "opening_title": "3 sourdough mistakes",
            "opening_title_duration_s": 2.0,
            "target_duration_s": 40,
            "target_duration_requested": True,
            "selected_media_ids": [binding.media_id],
        },
        generation_id="gen-1",
    )
    assert contract is not None
    assert contract.duration_s is None
    assert recipe.duration == pytest.approx(68.0)
    assert verify_phone_recipe(contract, recipe, source_audio={binding.media_id: True})


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
