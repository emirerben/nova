"""One row with a ``dim`` compiles to the same main-track clip through every phone writer.

``tests/fixtures/clip-dim-row/v1.json`` pins the row contract for Python and Swift
(``ClipDimTests``).
Failure modes, one test each:
- a writer drops the row's dim, or writes it without the ``clipDim`` capability;
- an undimmed (or 0) row changes a recipe byte: a ``dim`` key, a capability, a digest;
- Python and Swift read the same row differently (the shared fixture);
- a voiceover recut, a narrated projection, a refit or a Talking lane Save loses the dim;
- a dim on an overlay clip, or one outside 0..1, reaches a recipe the device would reject;
- the feature ships lit.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import settings
from app.kria.clip_dim import parse_row_dim
from app.kria.device_render import recipe_digest
from app.kria.recipes import EditRecipeV1
from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.phone_recipe_shared import apply_landscape_fit
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.pipeline.phone_voiceover_cut import replace_voiceover_cut
from app.routes.generative_jobs import TimelineSlotEdit
from app.services.authored_editor import resolve_authored_phone_slots
from app.services.phone_rollout import PhoneCapabilityUnavailable, validate_phone_pilot_recipe
from app.services.phone_voiceover_timeline import (
    narrated_timings_and_assignments,
    voiceover_ai_timeline,
)
from tests.pipeline import test_authored_timeline as authored
from tests.pipeline import test_phone_voiceover_cut as voiceover_cut
from tests.pipeline.test_phone_guided_plan import fixture as guided_fixture

FIXTURE = json.loads(
    (Path(__file__).parents[5] / "tests/fixtures/clip-dim-row/v1.json").read_text()
)
WRITERS = ["guided", "authored", "voiceover_cut_narrated", "voiceover_cut_montage"]


def _clip(recipe):
    return next(clip for track in recipe.tracks if track.kind == "video" for clip in track.clips)


def _guided(dim):
    plan, bindings = guided_fixture()
    if dim is not None:
        plan.story_timeline[0].dim = dim
    return compile_phone_guided_plan(plan, bindings)


def _authored(dim):
    job, variant, previous = authored.fixture()
    edit = {"dim": dim} if dim is not None else {}
    variant["user_timeline"]["slots"] = resolve_authored_phone_slots(
        job,
        variant,
        [TimelineSlotEdit(slot_id="added", clip_index=1, in_s=2, duration_s=3, **edit)],
    )
    return compile_phone_authored_timeline(job, variant, previous)


def _voiceover_cut(archetype, dim):
    recipe = voiceover_cut._recipe()
    if archetype == "voiceover":
        recipe = voiceover_cut._montage(recipe)
        slots = voiceover_cut._montage_slots(recipe)
    else:
        slots = voiceover_cut._narrated_slots(recipe)
    if dim is not None:
        slots[0]["dim"] = dim
    return replace_voiceover_cut(
        recipe,
        archetype=archetype,
        slots=slots,
        bindings=voiceover_cut._BINDINGS,
        pool=voiceover_cut._POOL,
    )


COMPILE = {
    "guided": _guided,
    "authored": _authored,
    "voiceover_cut_narrated": lambda dim: _voiceover_cut("narrated", dim),
    "voiceover_cut_montage": lambda dim: _voiceover_cut("voiceover", dim),
}


@pytest.mark.parametrize("writer", WRITERS)
def test_every_phone_writer_compiles_a_row_dim_onto_its_clip(writer):
    recipe = COMPILE[writer](0.4)
    assert _clip(recipe).dim == 0.4
    assert "clipDim" in recipe.required_capabilities


@pytest.mark.parametrize("writer", WRITERS)
def test_every_phone_writer_leaves_an_undimmed_clip_byte_identical(writer):
    plain = COMPILE[writer](None)
    assert _clip(plain).dim is None
    assert "clipDim" not in plain.required_capabilities
    assert '"dim"' not in json.dumps(plain.model_dump(mode="json"))
    if writer != "guided":
        assert recipe_digest(COMPILE[writer](0)) == recipe_digest(plain)


@pytest.mark.parametrize("case", FIXTURE["valid"], ids=lambda case: case["name"])
def test_shared_fixture_valid_dims_normalize(case):
    assert parse_row_dim(case["dim"]) == case["normalized"]
    assert TimelineSlotEdit(clip_index=0, in_s=0, dim=case["dim"]).dim == case["normalized"]


@pytest.mark.parametrize("case", FIXTURE["invalid"], ids=lambda case: case["name"])
def test_shared_fixture_invalid_dims_are_rejected(case):
    with pytest.raises(ValueError):
        parse_row_dim(case["dim"])
    with pytest.raises(ValidationError):
        TimelineSlotEdit(clip_index=0, in_s=0, dim=case["dim"])


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_a_non_finite_dim_is_rejected(value):
    with pytest.raises(ValueError):
        parse_row_dim(value)


def test_a_surviving_voiceover_slot_keeps_its_dim_and_a_null_removes_it():
    recipe = _voiceover_cut("voiceover", 0.5)
    slots = voiceover_ai_timeline(recipe, list(voiceover_cut._BINDINGS), voiceover_cut._POOL)[
        "slots"
    ]
    assert slots[0]["dim"] == 0.5
    for slot in slots:
        slot.pop("dim", None)
    again = replace_voiceover_cut(
        recipe,
        archetype="voiceover",
        slots=slots,
        bindings=voiceover_cut._BINDINGS,
        pool=voiceover_cut._POOL,
    )
    assert _clip(again).dim == 0.5
    slots[0]["dim"] = None
    removed = replace_voiceover_cut(
        again,
        archetype="voiceover",
        slots=slots,
        bindings=voiceover_cut._BINDINGS,
        pool=voiceover_cut._POOL,
    )
    assert recipe_digest(removed) == recipe_digest(_voiceover_cut("voiceover", None))


def test_a_repointed_voiceover_slot_starts_undimmed():
    recipe = _voiceover_cut("voiceover", 0.5)
    slots = voiceover_ai_timeline(recipe, list(voiceover_cut._BINDINGS), voiceover_cut._POOL)[
        "slots"
    ]
    slots[0].pop("dim")
    slots[0]["clip_index"] = slots[1]["clip_index"]
    again = replace_voiceover_cut(
        recipe,
        archetype="voiceover",
        slots=slots,
        bindings=voiceover_cut._BINDINGS,
        pool=voiceover_cut._POOL,
    )
    assert _clip(again).dim is None


def test_a_narrated_projection_round_trips_the_dim_to_the_row():
    recipe = _voiceover_cut("narrated", 0.3)
    slots = narrated_timings_and_assignments(
        recipe, list(voiceover_cut._BINDINGS), voiceover_cut._POOL
    )[2]
    assert slots[0]["dim"] == 0.3
    assert "dim" not in slots[1]


def test_a_refit_keeps_the_dim():
    fitted = apply_landscape_fit(_guided(0.6), guided_fixture()[1], "fit")
    assert _clip(fitted).dim == 0.6


def test_a_talking_lane_save_keeps_the_dim_the_creator_set():
    from app.services.phone_editor import _with_previous_clip_dims
    from tests.pipeline.test_authored_timeline import binding

    plain = compile_phone_subtitled_plan((binding("old"),), caption_cues=[])
    previous = _with_previous_clip_dims(plain, plain)
    assert previous is plain
    dimmed = plain.model_dump(mode="json")
    dimmed["tracks"][0]["clips"][0]["dim"] = 0.5
    kept = _with_previous_clip_dims(plain, type(plain).model_validate(dimmed))
    assert _clip(kept).dim == 0.5
    assert "clipDim" in kept.required_capabilities


def test_a_dim_on_an_overlay_clip_or_outside_zero_to_one_is_refused():
    recipe = _guided(0.4).model_dump(mode="json")
    overlay = {"id": "cards", "kind": "overlay", "clips": [dict(recipe["tracks"][0]["clips"][0])]}
    with pytest.raises(ValidationError):
        type(_guided(None)).model_validate({**recipe, "tracks": [*recipe["tracks"], overlay]})
    for bad in (0, 1.5, -0.2):
        clips = [dict(recipe["tracks"][0]["clips"][0], dim=bad)]
        with pytest.raises(ValidationError):
            EditRecipeV1.model_validate({"tracks": [{"id": "v", "kind": "video", "clips": clips}]})


def test_the_device_refuses_a_dimmed_recipe_until_clip_dim_is_verified(monkeypatch):
    recipe = _guided(0.4)
    monkeypatch.setattr(settings, "phone_render_verified_features", [])
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(recipe)
    assert refused.value.capability == "clipDim"


def test_the_flag_is_dark_by_default():
    assert settings.clip_dim_enabled is False
