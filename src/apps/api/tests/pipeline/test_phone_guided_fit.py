"""KRI-285: ``compile_phone_guided_plan(landscape_fit=...)`` letterboxes landscape
video moments on a portrait canvas (guided + unified montage share the compiler).

Mirrors ``test_phone_subtitled_plan.py``'s KRI-283 fit cases.
"""

import pytest

from app.config import settings
from app.kria.device_render import recipe_digest
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.phone_recipe_shared import apply_landscape_fit, landscape_fit_from_recipe
from app.services.phone_rollout import validate_phone_pilot_recipe
from tests.pipeline.test_phone_guided_plan import CROP, fixture

_FIT_SCALE = 0.31640625


def _clips(recipe):
    return next(t for t in recipe.tracks if t.id == "story").clips


def _with_source(*, width, height, orientation_degrees=0):
    plan, bindings = fixture()
    original = bindings[0].original
    original.width = width
    original.height = height
    original.orientation_degrees = orientation_degrees
    return plan, bindings


@pytest.mark.parametrize(
    "dims",
    [
        {"width": 1920, "height": 1080, "orientation_degrees": 0},
        {"width": 1080, "height": 1920, "orientation_degrees": 90},
    ],
)
def test_fit_letterboxes_landscape_display(dims):
    plan, bindings = _with_source(**dims)
    recipe = compile_phone_guided_plan(plan, bindings, landscape_fit="fit")
    (clip,) = _clips(recipe)
    assert clip.transform.scale == _FIT_SCALE
    assert clip.transform.position_x == 0 and clip.transform.position_y == 0
    assert landscape_fit_from_recipe(recipe) == "fit"


def test_fill_and_default_are_identity_and_byte_identical():
    plan, bindings = _with_source(width=1920, height=1080)
    default = compile_phone_guided_plan(plan, bindings)
    fill = compile_phone_guided_plan(plan, bindings, landscape_fit="fill")
    assert all(c.transform.scale == 1 for c in _clips(default))
    assert default.model_dump_json() == fill.model_dump_json()
    assert landscape_fit_from_recipe(fill) == "fill"


@pytest.mark.parametrize("dims", [(1080, 1080), (1080, 1920)])
def test_square_and_portrait_are_byte_identical_whatever_the_fit(dims):
    plan, bindings = _with_source(width=dims[0], height=dims[1])
    base = compile_phone_guided_plan(plan, bindings)
    fit = compile_phone_guided_plan(plan, bindings, landscape_fit="fit")
    assert base.model_dump_json() == fit.model_dump_json()


def test_fit_recipe_passes_phone_pilot_validation(monkeypatch):
    plan, bindings = _with_source(width=1920, height=1080)
    recipe = compile_phone_guided_plan(plan, bindings, landscape_fit="fit")
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


def test_fit_skips_a_creator_reframe_crop():
    plan, bindings = _with_source(width=1920, height=1080)
    plan.story_timeline[0] = plan.story_timeline[0].model_copy(update={"source_crop": CROP})
    recipe = compile_phone_guided_plan(plan, bindings, landscape_fit="fit")
    (clip,) = _clips(recipe)
    assert clip.source_crop is not None
    assert clip.transform.scale == 1


def test_look_with_fit_is_rejected_for_a_landscape_source():
    # golden_hour demands an exact-canvas unrotated source, so a letterboxed
    # (landscape) source can never carry a look; and fit never transforms a
    # clip that does carry one.
    plan, bindings = _with_source(width=1920, height=1080)
    plan.story_timeline[0].look_preset = "golden_hour"
    with pytest.raises(UnsupportedPhonePlan, match="exact-canvas"):
        compile_phone_guided_plan(plan, bindings, landscape_fit="fit")
    plan, bindings = _with_source(width=1080, height=1920)
    plan.story_timeline[0].look_preset = "golden_hour"
    recipe = compile_phone_guided_plan(plan, bindings, landscape_fit="fit")
    (clip,) = _clips(recipe)
    assert clip.look == "golden_hour" and clip.transform.scale == 1


def test_landscape_output_canvas_never_letterboxes():
    plan, bindings = _with_source(width=1920, height=1080)
    plan.output_orientation = "landscape"
    fit = compile_phone_guided_plan(plan, bindings, landscape_fit="fit")
    fill = compile_phone_guided_plan(plan, bindings)
    assert fit.model_dump_json() == fill.model_dump_json()
    assert (fit.canvas.width, fit.canvas.height) == (1920, 1080)


def test_apply_landscape_fit_round_trips_and_is_idempotent():
    plan, bindings = _with_source(width=1920, height=1080)
    fill = compile_phone_guided_plan(plan, bindings)
    fit = compile_phone_guided_plan(plan, bindings, landscape_fit="fit")
    once = apply_landscape_fit(fill, bindings, "fit")
    assert recipe_digest(once) == recipe_digest(fit)
    assert recipe_digest(apply_landscape_fit(once, bindings, "fit")) == recipe_digest(fit)
    back = apply_landscape_fit(once, bindings, "fill")
    assert recipe_digest(back) == recipe_digest(fill)
    assert recipe_digest(apply_landscape_fit(back, bindings, "fit")) == recipe_digest(fit)


def test_apply_landscape_fit_falls_back_to_recipe_asset_dims_without_bindings():
    plan, bindings = _with_source(width=1080, height=1920, orientation_degrees=90)
    fill = compile_phone_guided_plan(plan, bindings)
    fit = apply_landscape_fit(fill, (), "fit")
    assert _clips(fit)[0].transform.scale == _FIT_SCALE


def test_apply_landscape_fit_leaves_a_landscape_canvas_unchanged():
    plan, bindings = _with_source(width=1920, height=1080)
    plan.output_orientation = "landscape"
    recipe = compile_phone_guided_plan(plan, bindings)
    assert apply_landscape_fit(recipe, bindings, "fit") is recipe


def test_apply_landscape_fit_skips_look_and_crop_clips():
    plan, bindings = _with_source(width=1920, height=1080)
    plan.story_timeline[0] = plan.story_timeline[0].model_copy(update={"source_crop": CROP})
    recipe = compile_phone_guided_plan(plan, bindings)
    assert apply_landscape_fit(recipe, bindings, "fit") is recipe
