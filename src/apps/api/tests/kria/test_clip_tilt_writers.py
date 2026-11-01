"""KRI-1136: one row with a `tilt` compiles to the same clip through EVERY phone writer.

`tests/fixtures/clip-tilt-row/v1.json` pins the row contract for Python and Swift
(`ClipTiltTests`). The writers are the five phone compilers a Save or a first render can reach:
guided/montage, authored (Talking, song, lip-sync after an editor Save), voiceover cut,
voiceover montage and subtitled.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import settings
from app.kria.clip_tilt_row import canonical_row_tilt, parse_row_tilt
from app.kria.device_render import recipe_digest
from app.kria.recipes import ClipBackdrop
from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.phone_recipe_shared import apply_landscape_fit, clip_tilt_fields
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.pipeline.phone_voiceover_cut import replace_voiceover_cut
from app.routes.generative_jobs import TimelineSlotEdit
from app.services.authored_editor import resolve_authored_phone_slots
from app.services.phone_voiceover_timeline import (
    narrated_timings_and_assignments,
    voiceover_ai_timeline,
)
from tests.pipeline import test_authored_timeline as authored
from tests.pipeline import test_phone_voiceover_cut as voiceover_cut
from tests.pipeline import test_phone_voiceover_montage_plan as montage_plan
from tests.pipeline.test_phone_guided_plan import fixture as guided_fixture

ROW = Path(__file__).parents[5] / "tests/fixtures/clip-tilt-row/v1.json"
FIXTURE = json.loads(ROW.read_text())
TILT = {"rotation_deg": 15.0, "backdrop": {"kind": "blur"}}
WRITERS = [
    "guided",
    "authored",
    "voiceover_cut_narrated",
    "voiceover_cut_montage",
    "montage",
    "subtitled",
]


def _clip(recipe, clip_id=None):
    clips = [clip for track in recipe.tracks if track.kind == "video" for clip in track.clips]
    return clips[0] if clip_id is None else next(c for c in clips if c.id == clip_id)


def _guided(tilt):
    plan, bindings = guided_fixture()
    if tilt is not None:
        plan.story_timeline[0].tilt = tilt
    return compile_phone_guided_plan(plan, bindings)


def _authored(tilt):
    job, variant, previous = authored.fixture()
    edit = {"tilt": tilt} if tilt is not None else {}
    variant["user_timeline"]["slots"] = resolve_authored_phone_slots(
        job,
        variant,
        [TimelineSlotEdit(slot_id="added", clip_index=1, in_s=2, duration_s=3, **edit)],
    )
    return compile_phone_authored_timeline(job, variant, previous)


def _voiceover_cut(archetype, tilt):
    recipe = voiceover_cut._recipe()
    if archetype == "voiceover":
        recipe = voiceover_cut._montage(recipe)
        slots = voiceover_cut._montage_slots(recipe)
    else:
        slots = voiceover_cut._narrated_slots(recipe)
    if tilt is not None:
        slots[0]["tilt"] = tilt
    return replace_voiceover_cut(
        recipe,
        archetype=archetype,
        slots=slots,
        bindings=voiceover_cut._BINDINGS,
        pool=voiceover_cut._POOL,
    )


def _montage(tilt):
    decision, bindings = montage_plan.fixture()
    first = f"step-0-{decision.assembly_steps[0].clip_id}"
    return montage_plan._compile(
        decision,
        bindings,
        narration=montage_plan._narration(),
        clip_tilts={first: tilt} if tilt is not None else None,
    )


def _subtitled(tilt):
    from tests.pipeline.test_authored_timeline import binding

    return compile_phone_subtitled_plan(
        (binding("old"),),
        caption_cues=[],
        clip_tilts={"clip-0": tilt} if tilt is not None else None,
    )


COMPILE = {
    "guided": _guided,
    "authored": _authored,
    "voiceover_cut_narrated": lambda tilt: _voiceover_cut("narrated", tilt),
    "voiceover_cut_montage": lambda tilt: _voiceover_cut("voiceover", tilt),
    "montage": _montage,
    "subtitled": _subtitled,
}


@pytest.mark.parametrize("writer", WRITERS)
def test_every_phone_writer_compiles_a_tilt_to_the_same_transform_and_backdrop(writer):
    clip = _clip(COMPILE[writer](TILT))
    assert clip.transform.rotation_degrees == 15.0
    assert clip.backdrop == ClipBackdrop(kind="blur")


@pytest.mark.parametrize("writer", WRITERS)
def test_every_phone_writer_leaves_an_untilted_clip_byte_identical(writer):
    upright = COMPILE[writer](None)
    assert _clip(upright).transform.rotation_degrees == 0
    assert _clip(upright).backdrop is None
    assert "clipTilt" not in upright.required_capabilities
    assert "backdrop" not in json.dumps(upright.model_dump(mode="json"))
    assert recipe_digest(COMPILE[writer]({"rotation_deg": 0, "backdrop": {"kind": "blank"}})) == (
        recipe_digest(upright)
    )


@pytest.mark.parametrize("writer", WRITERS)
def test_every_phone_writer_marks_a_tilted_recipe_clip_tilt(writer):
    assert "clipTilt" in COMPILE[writer](TILT).required_capabilities


@pytest.mark.parametrize("case", FIXTURE["valid"], ids=lambda case: case["name"])
def test_shared_fixture_valid_tilts_normalize_and_map_to_recipe_fields(case):
    assert canonical_row_tilt(case["tilt"]) == case["normalized"]
    patch, backdrop = clip_tilt_fields({"tilt": case["tilt"]})
    assert patch.get("rotation_degrees", 0) == case["recipe"]["rotation_degrees"]
    assert (backdrop.model_dump(mode="json") if backdrop else None) == case["recipe"]["backdrop"]


@pytest.mark.parametrize("case", FIXTURE["invalid"], ids=lambda case: case["name"])
def test_shared_fixture_invalid_tilts_are_rejected(case):
    with pytest.raises(ValueError):
        parse_row_tilt(case["tilt"])
    with pytest.raises(ValidationError):
        TimelineSlotEdit(clip_index=0, in_s=0, duration_s=1, tilt=case["tilt"])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_a_non_finite_rotation_is_rejected(value):
    with pytest.raises(ValueError):
        parse_row_tilt({"rotation_deg": value})


def test_a_save_slot_normalizes_its_tilt_and_an_identity_tilt_clears_it():
    slot = TimelineSlotEdit(clip_index=0, in_s=0, duration_s=1, tilt={"rotation_deg": 190})
    assert slot.tilt == {"rotation_deg": -170.0}
    upright = TimelineSlotEdit(clip_index=0, in_s=0, duration_s=1, tilt={"rotation_deg": 0})
    assert upright.tilt is None and "tilt" in upright.model_fields_set


def test_a_tilted_sideways_clip_keeps_its_letterbox():
    recipe = _guided(TILT)
    assert _clip(recipe).transform.scale == 1
    fitted = apply_landscape_fit(recipe, guided_fixture()[1], "fit")
    clip = _clip(fitted)
    assert clip.transform.scale == pytest.approx(0.31640625)
    assert clip.transform.rotation_degrees == 15.0
    assert clip.backdrop == ClipBackdrop(kind="blur")
    assert apply_landscape_fit(fitted, guided_fixture()[1], "fill") != fitted
    assert _clip(apply_landscape_fit(fitted, guided_fixture()[1], "fill")).transform.scale == 1


def test_a_tilt_on_a_clip_with_a_look_is_refused():
    plan, bindings = guided_fixture()
    plan.story_timeline[0].tilt = TILT
    plan.story_timeline[0].look_preset = "golden_hour"
    with pytest.raises(UnsupportedPhonePlan):
        compile_phone_guided_plan(plan, bindings)


def test_a_surviving_voiceover_slot_keeps_its_tilt_when_a_later_cut_omits_it():
    recipe = _voiceover_cut("voiceover", TILT)
    slots = voiceover_ai_timeline(recipe, list(voiceover_cut._BINDINGS), voiceover_cut._POOL)[
        "slots"
    ]
    assert slots[0]["tilt"] == canonical_row_tilt(TILT)
    for slot in slots:
        slot.pop("tilt", None)
    again = replace_voiceover_cut(
        recipe,
        archetype="voiceover",
        slots=slots,
        bindings=voiceover_cut._BINDINGS,
        pool=voiceover_cut._POOL,
    )
    assert _clip(again).transform.rotation_degrees == 15.0
    assert _clip(again).backdrop == ClipBackdrop(kind="blur")
    slots[0]["tilt"] = None
    straight = replace_voiceover_cut(
        again,
        archetype="voiceover",
        slots=slots,
        bindings=voiceover_cut._BINDINGS,
        pool=voiceover_cut._POOL,
    )
    assert recipe_digest(straight) == recipe_digest(_voiceover_cut("voiceover", None))


def test_a_narrated_projection_round_trips_the_tilt_to_the_row():
    recipe = _voiceover_cut("narrated", TILT)
    slots = narrated_timings_and_assignments(
        recipe, list(voiceover_cut._BINDINGS), voiceover_cut._POOL
    )[2]
    assert slots[0]["tilt"] == canonical_row_tilt(TILT)
    assert "tilt" not in slots[1]


def test_the_flag_is_dark_by_default():
    assert settings.media_tilt_enabled is False
