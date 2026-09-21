import uuid

import pytest

from app.config import settings
from app.pipeline.canvas import PORTRAIT
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.portable_text_layout import compile_text_overlay
from app.services.phone_rollout import validate_phone_pilot_recipe
from tests.pipeline.test_phone_guided_plan import fixture


def _default_font_recipe(family, *, giant=False):
    from app.agents._schemas.text_element import TextElement

    plan, bindings = fixture()
    plan.text_elements = [
        TextElement(
            id="title",
            text="This view",
            start_s=0.5,
            end_s=2.5,
            font_family=family,
            effect="fade-in",
            size_px=64,
            theme_transition={"type": "giant-title-wipe"} if giant else None,
        )
    ]
    recipe = compile_phone_guided_plan(plan, bindings)
    # CoreText on macOS chooses opsz=12; production Linux chooses opsz=9.
    # Linux CI checks the actual compiler output before this platform adaptation.
    import sys

    for run in recipe.text_layers[0].runs:
        if sys.platform == "linux":
            assert run.font_variations["opsz"] == 9
        else:
            run.font_variations["opsz"] = 9
    return recipe


def test_qualified_font_does_not_qualify_giant_title(monkeypatch):
    # Strict-mode pin: the narrow per-instance gate never qualified a giant
    # title (any effect combined with `giant_title` was unqualified). In
    # default mode `giant-title-wipe` has native support for every effect but
    # handwriting, so this exact combination is no longer rejected there --
    # see test_default_mode_allows_giant_title_on_non_handwriting_effects.
    monkeypatch.setattr(settings, "phone_font_qualification_strict", True)
    recipe = _default_font_recipe("Fraunces", giant=True)
    assert recipe.text_layers[0].giant_title is not None
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    with pytest.raises(ValueError, match="font instance"):
        validate_phone_pilot_recipe(recipe)


@pytest.mark.parametrize("family", ["Fraunces", "DM Sans"])
def test_default_variable_fonts_require_exact_qualification_and_capability(family, monkeypatch):
    monkeypatch.setattr(settings, "phone_font_qualification_strict", True)
    recipe = _default_font_recipe(family)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        list(recipe.required_capabilities - {"authoredText"}),
    )
    with pytest.raises(ValueError, match="capability"):
        validate_phone_pilot_recipe(recipe)


@pytest.mark.parametrize(
    "change",
    [
        "axes",
        "empty_axes",
        "extra_axis",
        "missing_axis",
        "hash",
        "size",
        "catalog",
        "family",
        "effect",
    ],
)
def test_default_font_qualification_cannot_expand_by_alias_or_coordinates(change, monkeypatch):
    # Strict-mode pin: exact byte/coordinate qualification. In default mode
    # most of these mutations (e.g. a different opsz, or a differently-cased
    # bundled font) would qualify instead -- that's the point of the
    # relaxation. See test_default_mode_rejects_* for the default-mode
    # equivalents that still must fail.
    monkeypatch.setattr(settings, "phone_font_qualification_strict", True)
    recipe = _default_font_recipe("Fraunces")
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    run = recipe.text_layers[0].runs[0]
    if change == "axes":
        run.font_variations["opsz"] = 12
    elif change == "empty_axes":
        run.font_variations.clear()
    elif change == "extra_axis":
        run.font_variations["FAKE"] = 0
    elif change == "missing_axis":
        run.font_variations.pop("SOFT")
    elif change == "effect":
        recipe.text_layers[0].effect = "static"
    else:
        asset = next(a for a in recipe.asset_manifest.assets if a.id == run.font_asset_id)
        if change in {"hash", "size"}:
            fingerprint = asset.fingerprint.model_copy(
                update={
                    "sha256" if change == "hash" else "byte_count": "f" * 64
                    if change == "hash"
                    else 1
                }
            )
            replacement = asset.model_copy(update={"fingerprint": fingerprint})
        else:
            replacement = asset.model_copy(
                update={
                    "catalog" if change == "catalog" else "catalog_id": "music"
                    if change == "catalog"
                    else "Other.ttf"
                }
            )
        recipe.asset_manifest = recipe.asset_manifest.model_copy(
            update={
                "assets": tuple(
                    replacement if a.id == asset.id else a for a in recipe.asset_manifest.assets
                )
            }
        )
    with pytest.raises(ValueError, match="font instance"):
        validate_phone_pilot_recipe(recipe)


def test_qualified_font_does_not_qualify_authored_phases(monkeypatch):
    from app.agents._schemas.text_animation_phases import TextAnimationPhases

    monkeypatch.setattr(settings, "phone_font_qualification_strict", True)
    recipe = _default_font_recipe("DM Sans")
    recipe.text_layers[0].animation_phases = TextAnimationPhases(loop="float")
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    with pytest.raises(ValueError, match="phases"):
        validate_phone_pilot_recipe(recipe)


def test_guided_default_title_and_body_compile_through_phone_pilot(monkeypatch):
    import sys

    from app.agents._schemas.text_element import TextElement
    from app.pipeline.guided_story import _text_elements
    from app.schemas.edit_proposal import EditProposalSnapshot
    from tests.pipeline.test_guided_story import _guided_snapshot

    # Strict-mode pin: this is the exact 2026-09-14 default-variable-font
    # fade-in scenario the narrow per-instance gate was written to allow.
    # Default mode's broader test is
    # test_default_mode_qualifies_static_title_and_context_labels below.
    monkeypatch.setattr(settings, "phone_font_qualification_strict", True)
    snapshot = EditProposalSnapshot.model_validate(_guided_snapshot()["approved_proposal"])
    snapshot.duration_s = 3
    snapshot.story_beats = snapshot.story_beats[:1]
    elements = _text_elements(
        snapshot, [{"start_s": 0, "end_s": 3}], {"text_effect": "fade-in"}, compiler_version=6
    )
    plan, bindings = fixture()
    plan.text_elements = [TextElement.model_validate(element) for element in elements]
    recipe = compile_phone_guided_plan(plan, bindings)
    assert {
        asset.catalog_id for asset in recipe.asset_manifest.assets if asset.kind == "library"
    } == {"Fraunces-Bold.ttf", "DMSans-Bold.ttf"}
    if sys.platform != "linux":
        for layer in recipe.text_layers:
            for run in layer.runs:
                run.font_variations["opsz"] = 9
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


def test_account_cohort_obeys_kill_switch(monkeypatch):
    enrolled, outsider = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(settings, "phone_render_user_ids", [enrolled])
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    assert settings.phone_rendering_for(enrolled)
    assert settings.phone_rendering_for(str(enrolled))
    assert not settings.phone_rendering_for(outsider)
    assert not settings.phone_rendering_for(None)
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    assert not settings.phone_rendering_for(enrolled)


def test_unadvertised_capability_cannot_issue_a_device_recipe(monkeypatch):
    plan, sources = fixture()
    recipe = compile_phone_guided_plan(plan, sources)
    monkeypatch.setattr(settings, "phone_render_verified_features", [])
    with pytest.raises(ValueError, match="capability"):
        validate_phone_pilot_recipe(recipe)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


def test_pool_photo_recipe_needs_still_images_verified(monkeypatch):
    # PHONE_RENDER_VERIFIED_FEATURES is the only per-deploy switch for pool
    # stills; a device must never receive a photo it has not qualified.
    from tests.pipeline.test_phone_guided_plan import photo_fixture

    plan, sources, visuals = photo_fixture()
    recipe = compile_phone_guided_plan(plan, sources, visuals)
    assert "stillImages" in recipe.required_capabilities
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        list(recipe.required_capabilities - {"stillImages"}),
    )
    with pytest.raises(ValueError, match="capability"):
        validate_phone_pilot_recipe(recipe)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


def test_pool_video_recipe_needs_visual_videos_verified(monkeypatch):
    # stillImages qualifies only the photo path; a pool video has its own switch.
    from tests.pipeline.test_phone_guided_plan import pool_video_fixture

    recipe = compile_phone_guided_plan(*pool_video_fixture())
    assert "visualVideos" in recipe.required_capabilities
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        list(recipe.required_capabilities - {"visualVideos"} | {"stillImages"}),
    )
    with pytest.raises(ValueError, match="capability"):
        validate_phone_pilot_recipe(recipe)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


def test_photo_and_pool_video_recipe_needs_both_features_verified(monkeypatch):
    from tests.pipeline.test_phone_guided_plan import (
        append_pool_video,
        photo_fixture,
        pool_video,
    )

    plan, sources, photos = photo_fixture()
    video = pool_video()
    append_pool_video(plan, video)
    recipe = compile_phone_guided_plan(plan, sources, (*photos, video))
    for missing in ("stillImages", "visualVideos"):
        monkeypatch.setattr(
            settings,
            "phone_render_verified_features",
            list(recipe.required_capabilities - {missing}),
        )
        with pytest.raises(ValueError, match="capability"):
            validate_phone_pilot_recipe(recipe)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


def test_supporting_card_recipe_passes_the_pilot_gate_with_still_images(monkeypatch):
    # The card is a layout of an already-qualified pool still: it must not trip
    # the editor-media / visual-block gates or ask for another capability.
    from tests.pipeline.test_phone_guided_plan import photo_fixture

    fullscreen = compile_phone_guided_plan(*photo_fixture())
    recipe = compile_phone_guided_plan(*photo_fixture(layout="supporting_card"))
    assert recipe.tracks[0].clips[1].still_layout == "supporting_card"
    assert recipe.required_capabilities == fullscreen.required_capabilities
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(fullscreen.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        list(recipe.required_capabilities - {"stillImages"}),
    )
    with pytest.raises(ValueError, match="capability"):
        validate_phone_pilot_recipe(recipe)


def test_slow_giant_handwriting_stays_blocked_with_animated_text_enabled(monkeypatch):
    plan, sources = fixture()
    recipe = compile_phone_guided_plan(plan, sources)
    layer, _ = compile_text_overlay(
        dict(
            text="GO",
            effect="handwriting",
            start_s=0,
            end_s=3,
            font_family="Inter",
            text_size_px=62,
            preserve_font_size=True,
            theme_transition={"type": "giant-title-wipe"},
        ),
        layer_id="slow",
        canvas=PORTRAIT,
    )
    recipe.text_layers = [layer]
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        list(recipe.required_capabilities) + ["animatedText", "positionedText"],
    )
    with pytest.raises(ValueError, match="Giant-title handwriting"):
        validate_phone_pilot_recipe(recipe)


def test_authored_phases_stay_blocked_until_device_parity_is_verified(monkeypatch):
    from app.agents._schemas.text_animation_phases import TextAnimationPhases
    from app.kria.recipes_v2 import EditRecipeV2
    from tests.kria.test_portable_text import text_document

    monkeypatch.setattr(settings, "phone_font_qualification_strict", True)
    recipe = EditRecipeV2.model_validate(text_document())
    recipe.text_layers[0].animation_phases = TextAnimationPhases(loop="float")
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    with pytest.raises(ValueError, match="phases"):
        validate_phone_pilot_recipe(recipe)


def test_camera_program_cannot_bypass_device_qualification(monkeypatch):
    from app.kria.portable_camera import CameraPulse

    plan, sources = fixture()
    recipe = compile_phone_guided_plan(plan, sources)
    recipe.camera_pulses = [CameraPulse(id="pulse", start=0, end=1, intensity=0.04)]
    monkeypatch.setattr(
        settings, "phone_render_verified_features", ["cameraEffects", *recipe.required_capabilities]
    )
    with pytest.raises(ValueError, match="Camera effects"):
        validate_phone_pilot_recipe(recipe)


# -- Default (non-strict) font/effect qualification (2026-09-18) -----------
#
# PHONE_FONT_QUALIFICATION_STRICT defaults to False. These pin the relaxed
# gate: any bundled-registry font (assets/fonts/font-registry.json) with
# correct variation coordinates, on any of the 17 native-supported text
# effects, qualifies -- reproducing prod job b33e1c88-a8eb-4d51-81b6-
# 388f7a32aebf (static Fraunces title + Inter-Bold static context labels).


def test_default_mode_qualifies_static_title_and_context_labels(monkeypatch):
    import sys

    from app.agents._schemas.text_element import TextElement

    monkeypatch.setattr(settings, "phone_font_qualification_strict", False)
    plan, bindings = fixture()
    plan.text_elements = [
        TextElement(
            id="title",
            text="This view",
            start_s=0.5,
            end_s=2.5,
            font_family="Fraunces",
            effect="static",
            size_px=64,
        )
    ]
    plan.context_label_text_elements = [
        TextElement(
            id="context",
            text="Context",
            start_s=0,
            end_s=3,
            font_family="Inter",
            effect="static",
        )
    ]
    recipe = compile_phone_guided_plan(plan, bindings)
    assert {
        asset.catalog_id for asset in recipe.asset_manifest.assets if asset.kind == "library"
    } == {"Fraunces-Bold.ttf", "Inter-Bold.ttf"}
    if sys.platform != "linux":
        for layer in recipe.text_layers:
            for run in layer.runs:
                if run.font_variations:
                    run.font_variations["opsz"] = 9
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


def test_default_mode_allows_giant_title_on_non_handwriting_effects(monkeypatch):
    monkeypatch.setattr(settings, "phone_font_qualification_strict", False)
    recipe = _default_font_recipe("Fraunces", giant=True)
    assert recipe.text_layers[0].giant_title is not None
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


def test_default_mode_rejects_font_outside_the_bundled_registry(monkeypatch):
    monkeypatch.setattr(settings, "phone_font_qualification_strict", False)
    recipe = _default_font_recipe("Fraunces")
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    run = recipe.text_layers[0].runs[0]
    asset = next(a for a in recipe.asset_manifest.assets if a.id == run.font_asset_id)
    replacement = asset.model_copy(update={"catalog_id": "NotBundled.ttf"})
    recipe.asset_manifest = recipe.asset_manifest.model_copy(
        update={
            "assets": tuple(
                replacement if a.id == asset.id else a for a in recipe.asset_manifest.assets
            )
        }
    )
    with pytest.raises(ValueError, match="font instance"):
        validate_phone_pilot_recipe(recipe)


def test_default_mode_rejects_variable_font_missing_variations(monkeypatch):
    monkeypatch.setattr(settings, "phone_font_qualification_strict", False)
    recipe = _default_font_recipe("Fraunces")
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    recipe.text_layers[0].runs[0].font_variations.clear()
    with pytest.raises(ValueError, match="font instance"):
        validate_phone_pilot_recipe(recipe)


def test_default_mode_still_blocks_giant_title_handwriting(monkeypatch):
    plan, sources = fixture()
    recipe = compile_phone_guided_plan(plan, sources)
    layer, _ = compile_text_overlay(
        dict(
            text="GO",
            effect="handwriting",
            start_s=0,
            end_s=3,
            font_family="Inter",
            text_size_px=62,
            preserve_font_size=True,
            theme_transition={"type": "giant-title-wipe"},
        ),
        layer_id="slow",
        canvas=PORTRAIT,
    )
    recipe.text_layers = [layer]
    monkeypatch.setattr(settings, "phone_font_qualification_strict", False)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        list(recipe.required_capabilities) + ["animatedText", "positionedText"],
    )
    with pytest.raises(ValueError, match="Giant-title handwriting"):
        validate_phone_pilot_recipe(recipe)


def test_narration_audio_requires_the_capability_to_be_verified(monkeypatch):
    """KRI-132: a montage-family voiceover recipe needs narrationAudio in
    phone_render_verified_features, same gating pattern as stillImages/
    visualVideos -- follow how those are gated (see docs/reviews/kri-29
    /capability-matrix.md's "narrationAudio" row)."""
    from app.pipeline.phone_montage_plan import compile_phone_montage_plan
    from tests.pipeline.test_phone_montage_plan import _narration
    from tests.pipeline.test_phone_montage_plan import fixture as montage_fixture

    decision, bindings = montage_fixture(
        voiceover_gcs_path="voiceover-uploads/direct/u/i/voice.m4a",
        voiceover_target_s=11.4,
        mix=1.0,
    )
    narration = _narration()
    recipe = compile_phone_montage_plan(decision, bindings, music=None, narration=narration)
    assert "narrationAudio" in recipe.required_capabilities

    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        list(recipe.required_capabilities - {"narrationAudio"}),
    )
    with pytest.raises(ValueError, match="phone capability that is not enabled"):
        validate_phone_pilot_recipe(recipe)

    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)
