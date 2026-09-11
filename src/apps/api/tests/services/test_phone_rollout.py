import uuid

import pytest

from app.config import settings
from app.pipeline.canvas import PORTRAIT
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.portable_text_layout import compile_text_overlay
from app.services.phone_rollout import validate_phone_pilot_recipe
from tests.pipeline.test_phone_guided_plan import fixture


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
