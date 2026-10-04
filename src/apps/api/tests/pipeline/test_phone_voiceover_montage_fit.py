"""KRI-285: the voiceover-montage phone compiler letterboxes landscape clips when
``assembly_landscape_fit == "fit"`` (it used to reject it), and a re-cut keeps
those bars for newly placed clips.

Mirrors ``test_phone_subtitled_plan.py``'s KRI-283 fit cases.
"""

import pytest

from app.config import settings
from app.kria.device_render import recipe_digest
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_recipe_shared import (
    apply_landscape_fit,
    display_dims,
    landscape_fit_from_recipe,
)
from app.pipeline.phone_voiceover_cut import replace_voiceover_cut
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneSourceBinding
from tests.pipeline.test_phone_voiceover_montage_plan import (
    _step,
    fixture,
)
from tests.pipeline.test_phone_voiceover_montage_plan import (
    compile_phone_voiceover_montage_plan as compile_montage,
)

_FIT_SCALE = 0.31640625


def _binding(media_id: str, *, width=1920, height=1080, orientation_degrees=0, duration_s=10.0):
    return PhoneSourceBinding(
        media_id=media_id,
        proxy_path=f"user/analysis-proxy-{media_id}.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=duration_s,
            width=width,
            height=height,
            orientation_degrees=orientation_degrees,
            has_audio=True,
        ),
    )


def _compile(fit, *, bindings, extras=None, **kwargs):
    overrides = {"assembly_landscape_fit": fit, **(extras or {})}
    decision, _ = fixture(bindings=bindings, extras_overrides=overrides, **kwargs)
    return compile_montage(decision, bindings)


def _clips(recipe):
    return next(t for t in recipe.tracks if t.id == "montage").clips


def _landscape_bindings():
    return tuple(_binding(f"c{i}") for i in range(3))


@pytest.mark.parametrize(
    "dims",
    [
        {"width": 1920, "height": 1080},
        {"width": 1080, "height": 1920, "orientation_degrees": 90},
    ],
)
def test_fit_letterboxes_every_landscape_clip(dims):
    bindings = tuple(_binding(f"c{i}", **dims) for i in range(3))
    recipe = _compile("fit", bindings=bindings)
    clips = _clips(recipe)
    assert len(clips) == 3
    assert all(c.transform.scale == _FIT_SCALE for c in clips)
    assert all(c.transform.position_x == 0 and c.transform.position_y == 0 for c in clips)
    assert landscape_fit_from_recipe(recipe) == "fit"


def test_mixed_orientations_only_letterbox_the_landscape_clips():
    bindings = (
        _binding("c0"),
        _binding("c1", width=1080, height=1920),
        _binding("c2", width=1080, height=1080),
    )
    clips = _clips(_compile("fit", bindings=bindings))
    assert [c.transform.scale for c in clips] == [_FIT_SCALE, 1, 1]


def test_fill_and_default_are_identity_and_byte_identical():
    bindings = _landscape_bindings()
    fill = _compile("fill", bindings=bindings)
    default = _compile(None, bindings=bindings)
    assert all(c.transform.scale == 1 for c in _clips(fill))
    assert fill.model_dump_json() == default.model_dump_json()
    assert landscape_fit_from_recipe(fill) == "fill"


@pytest.mark.parametrize("dims", [(1080, 1080), (1080, 1920)])
def test_square_and_portrait_are_byte_identical_whatever_the_fit(dims):
    bindings = tuple(_binding(f"c{i}", width=dims[0], height=dims[1]) for i in range(3))
    assert (
        _compile("fit", bindings=bindings).model_dump_json()
        == _compile("fill", bindings=bindings).model_dump_json()
    )


def test_fit_recipe_passes_phone_pilot_validation(monkeypatch):
    recipe = _compile("fit", bindings=_landscape_bindings())
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        list(recipe.required_capabilities),
    )
    validate_phone_pilot_recipe(recipe)


def test_landscape_output_canvas_never_letterboxes():
    bindings = _landscape_bindings()
    fit = _compile("fit", bindings=bindings, extras={"canvas": {"width": 1920, "height": 1080}})
    fill = _compile("fill", bindings=bindings, extras={"canvas": {"width": 1920, "height": 1080}})
    assert fit.model_dump_json() == fill.model_dump_json()


def test_unknown_fit_value_still_fails_closed():
    with pytest.raises(UnsupportedPhonePlan, match="landscape fit"):
        _compile("zoom", bindings=_landscape_bindings())


def test_look_with_fit_is_rejected_for_a_landscape_source():
    # golden_hour needs an exact-canvas unrotated source, so a letterboxed
    # (landscape) source can never carry the look.
    with pytest.raises(UnsupportedPhonePlan, match="exact-canvas"):
        _compile(
            "fit",
            bindings=_landscape_bindings(),
            extras={"recipe": {"color_grade": "golden_hour"}},
        )


def test_look_on_an_exact_canvas_source_keeps_identity_under_fit():
    bindings = tuple(_binding(f"c{i}", width=1080, height=1920) for i in range(3))
    recipe = _compile("fit", bindings=bindings, extras={"recipe": {"color_grade": "golden_hour"}})
    assert all(c.look == "golden_hour" and c.transform.scale == 1 for c in _clips(recipe))


def test_display_dims_applies_the_orientation_flag():
    assert display_dims(_binding("a").original) == (1920, 1080)
    assert display_dims(
        _binding("a", width=1080, height=1920, orientation_degrees=90).original
    ) == (
        1920,
        1080,
    )
    assert display_dims(
        _binding("a", width=1080, height=1920, orientation_degrees=270).original
    ) == (
        1920,
        1080,
    )


# ---- re-cut keeps the bars -------------------------------------------------


def _slots(recipe, new_clip_index=None):
    slots = []
    for index, clip in enumerate(_clips(recipe)):
        slots.append(
            {
                "slot_id": clip.id,
                "clip_index": index,
                "in_s": clip.source_start,
                "duration_s": clip.source_duration / clip.rate,
                "transition_after": "cut",
                "playback_rate": clip.rate,
            }
        )
    if new_clip_index is not None:
        slots.append(
            {
                "slot_id": "added",
                "clip_index": new_clip_index,
                "in_s": 0.0,
                "duration_s": 2.0,
                "transition_after": "cut",
                "playback_rate": 1.0,
            }
        )
    return slots


def _recut(recipe, bindings, slots, **kwargs):
    return replace_voiceover_cut(
        recipe,
        archetype="voiceover",
        slots=slots,
        bindings=bindings,
        pool=[b.proxy_path for b in bindings],
        **kwargs,
    )


def _recut_fixture():
    bindings = (*_landscape_bindings(), _binding("c3"))
    decision, _ = fixture(
        bindings=bindings[:3],
        extras_overrides={"assembly_landscape_fit": "fit"},
        steps=[_step("c0"), _step("c1"), _step("c2")],
    )
    return compile_montage(decision, bindings[:3]), bindings


def test_recut_keeps_the_letterbox_for_a_newly_placed_clip():
    recipe, bindings = _recut_fixture()
    edited = _recut(recipe, bindings, _slots(recipe, new_clip_index=3))
    clips = _clips(edited)
    assert len(clips) == 4
    assert [c.transform.scale for c in clips] == [_FIT_SCALE] * 4


def test_recut_of_a_fill_recipe_adds_a_center_cropped_clip():
    bindings = (*_landscape_bindings(), _binding("c3"))
    decision, _ = fixture(
        bindings=bindings[:3], extras_overrides={"assembly_landscape_fit": "fill"}
    )
    recipe = compile_montage(decision, bindings[:3])
    edited = _recut(recipe, bindings, _slots(recipe, new_clip_index=3))
    assert [c.transform.scale for c in _clips(edited)] == [1, 1, 1, 1]


def test_recut_explicit_fit_overrides_an_ambiguous_recipe():
    # An all-portrait recipe compiled with "fit" reads back as "fill"; the
    # persisted variant value wins when the caller passes it.
    portrait = tuple(_binding(f"c{i}", width=1080, height=1920) for i in range(3))
    decision, _ = fixture(bindings=portrait, extras_overrides={"assembly_landscape_fit": "fit"})
    recipe = compile_montage(decision, portrait)
    assert landscape_fit_from_recipe(recipe) == "fill"
    bindings = (*portrait, _binding("c3"))
    edited = _recut(recipe, bindings, _slots(recipe, new_clip_index=3), landscape_fit="fit")
    assert [c.transform.scale for c in _clips(edited)] == [1, 1, 1, _FIT_SCALE]


# ---- apply_landscape_fit ---------------------------------------------------


def test_apply_landscape_fit_round_trips_and_is_idempotent():
    bindings = _landscape_bindings()
    fill = _compile("fill", bindings=bindings)
    fit = _compile("fit", bindings=bindings)
    once = apply_landscape_fit(fill, bindings, "fit")
    assert recipe_digest(once) == recipe_digest(fit)
    assert recipe_digest(apply_landscape_fit(once, bindings, "fit")) == recipe_digest(fit)
    back = apply_landscape_fit(once, bindings, "fill")
    assert recipe_digest(back) == recipe_digest(fill)
    assert recipe_digest(apply_landscape_fit(back, bindings, "fit")) == recipe_digest(fit)


def test_apply_landscape_fit_only_touches_the_main_video_track():
    bindings = _landscape_bindings()
    fill = _compile("fill", bindings=bindings)
    fit = apply_landscape_fit(fill, bindings, "fit")
    assert [t.id for t in fit.tracks] == [t.id for t in fill.tracks]
    for before, after in zip(fill.tracks, fit.tracks, strict=True):
        if before.id != "montage":
            assert before == after
    assert fit.text_layers == fill.text_layers
    assert fit.audio == fill.audio


def test_apply_landscape_fit_is_a_noop_for_portrait_sources_and_landscape_canvas():
    portrait = tuple(_binding(f"c{i}", width=1080, height=1920) for i in range(3))
    recipe = _compile("fill", bindings=portrait)
    assert apply_landscape_fit(recipe, portrait, "fit") is recipe
    landscape_canvas = _compile(
        "fill", bindings=_landscape_bindings(), extras={"canvas": {"width": 1920, "height": 1080}}
    )
    assert apply_landscape_fit(landscape_canvas, _landscape_bindings(), "fit") is landscape_canvas


def test_apply_landscape_fit_skips_looked_clips():
    bindings = tuple(_binding(f"c{i}", width=1080, height=1920) for i in range(3))
    recipe = _compile("fill", bindings=bindings, extras={"recipe": {"color_grade": "golden_hour"}})
    assert all(c.look == "golden_hour" for c in _clips(recipe))
    # Pretend the sources turned out landscape: a look is never transformed.
    landscape = _landscape_bindings()
    assert apply_landscape_fit(recipe, landscape, "fit") is recipe


# ---- a repointed slot recomputes its transform for the NEW source -----------


def _repoint(recipe, bindings, *, slot=0, to_index):
    slots = _slots(recipe)
    slots[slot]["clip_index"] = to_index
    slots[slot]["in_s"] = 0.0
    return _recut(recipe, bindings, slots)


def test_recut_repointing_a_slot_to_a_portrait_source_drops_the_stale_letterbox():
    recipe, _ = _recut_fixture()  # three landscape clips compiled with "fit"
    bindings = (*_landscape_bindings(), _binding("c3", width=1080, height=1920))
    edited = _repoint(recipe, bindings, to_index=3)
    clips = _clips(edited)
    assert clips[0].source_asset_id == "c3"
    assert [c.transform.scale for c in clips] == [1, _FIT_SCALE, _FIT_SCALE]


def test_recut_repointing_a_slot_to_a_landscape_source_letterboxes_it():
    portrait_first = (
        _binding("c0", width=1080, height=1920),
        _binding("c1"),
        _binding("c2"),
    )
    decision, _ = fixture(
        bindings=portrait_first,
        extras_overrides={"assembly_landscape_fit": "fit"},
        steps=[_step("c0"), _step("c1"), _step("c2")],
    )
    recipe = compile_montage(decision, portrait_first)
    assert [c.transform.scale for c in _clips(recipe)] == [1, _FIT_SCALE, _FIT_SCALE]
    bindings = (*portrait_first, _binding("c3"))
    edited = _repoint(recipe, bindings, to_index=3)
    assert [c.transform.scale for c in _clips(edited)] == [_FIT_SCALE] * 3


def test_recut_repointing_in_a_fill_recipe_stays_identity():
    decision, _ = fixture(
        bindings=_landscape_bindings(),
        extras_overrides={"assembly_landscape_fit": "fill"},
    )
    recipe = compile_montage(decision, _landscape_bindings())
    bindings = (*_landscape_bindings(), _binding("c3", width=1080, height=1920))
    edited = _repoint(recipe, bindings, to_index=3)
    assert [c.transform.scale for c in _clips(edited)] == [1, 1, 1]


def test_recut_keeping_the_same_source_keeps_the_pinned_transform():
    recipe, bindings = _recut_fixture()
    edited = _recut(recipe, bindings, _slots(recipe))
    assert _clips(edited) == _clips(recipe)
