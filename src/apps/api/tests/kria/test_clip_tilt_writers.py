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
from app.pipeline.phone_recipe_shared import (
    apply_landscape_fit,
    clip_tilt_fields,
    landscape_fit_from_recipe,
    split_clip_scale,
)
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


def _guided(tilt, fit="fill"):
    plan, bindings = guided_fixture()
    if tilt is not None:
        plan.story_timeline[0].tilt = tilt
    return compile_phone_guided_plan(plan, bindings, landscape_fit=fit)


def _authored(tilt, fit="fill"):
    job, variant, previous = authored.fixture()
    edit = {"tilt": tilt} if tilt is not None else {}
    variant["user_timeline"]["slots"] = resolve_authored_phone_slots(
        job,
        variant,
        [TimelineSlotEdit(slot_id="added", clip_index=1, in_s=2, duration_s=3, **edit)],
    )
    return compile_phone_authored_timeline(job, variant, previous)


def _sideways(binding):
    original = binding.original.model_copy(update={"width": 1920, "height": 1080})
    return binding.model_copy(update={"original": original})


def _voiceover_cut(archetype, tilt, fit="fill"):
    recipe = voiceover_cut._recipe()
    if archetype == "voiceover":
        recipe = voiceover_cut._montage(recipe)
        slots = voiceover_cut._montage_slots(recipe)
    else:
        slots = voiceover_cut._narrated_slots(recipe)
    if tilt is not None:
        slots[0]["tilt"] = tilt
    bindings = voiceover_cut._BINDINGS
    if fit == "fit":
        slots[0]["slot_id"] = "fresh"
        bindings = tuple(_sideways(b) for b in bindings)
    return replace_voiceover_cut(
        recipe,
        archetype=archetype,
        slots=slots,
        bindings=bindings,
        pool=voiceover_cut._POOL,
        landscape_fit=fit,
    )


def _montage(tilt, fit="fill"):
    decision, bindings = montage_plan.fixture()
    first = f"step-0-{decision.assembly_steps[0].clip_id}"
    decision.extras["assembly_landscape_fit"] = fit
    return montage_plan._compile(
        decision,
        tuple(_sideways(b) for b in bindings) if fit == "fit" else bindings,
        narration=montage_plan._narration(),
        clip_tilts={first: tilt} if tilt is not None else None,
    )


def _subtitled(tilt, fit="fill"):
    from tests.pipeline.test_authored_timeline import binding

    source = binding("old")
    return compile_phone_subtitled_plan(
        (_sideways(source) if fit == "fit" else source,),
        caption_cues=[],
        clip_tilts={"clip-0": tilt} if tilt is not None else None,
        landscape_fit=fit,
    )


COMPILE = {
    "guided": _guided,
    "authored": _authored,
    "voiceover_cut_narrated": lambda tilt, fit="fill": _voiceover_cut("narrated", tilt, fit),
    "voiceover_cut_montage": lambda tilt, fit="fill": _voiceover_cut("voiceover", tilt, fit),
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


LETTERBOX = 0.31640625
LETTERBOXED = {"guided", "voiceover_cut_montage", "montage", "subtitled"}


@pytest.mark.parametrize("zoom", [0.5, 1.5, 3.0])
@pytest.mark.parametrize("fit", ["fill", "fit"])
@pytest.mark.parametrize("writer", WRITERS)
def test_every_phone_writer_composes_the_zoom_with_the_letterbox(writer, fit, zoom):
    clip = _clip(COMPILE[writer]({**TILT, "zoom": zoom}, fit))
    framing = LETTERBOX if fit == "fit" and writer in LETTERBOXED else 1.0
    assert clip.transform.scale == pytest.approx(framing * zoom)
    assert clip.transform.rotation_degrees == 15.0
    assert clip.backdrop == ClipBackdrop(kind="blur")
    assert "clipTilt" in COMPILE[writer]({**TILT, "zoom": zoom}, fit).required_capabilities


@pytest.mark.parametrize("fit", ["fill", "fit"])
@pytest.mark.parametrize("writer", WRITERS)
def test_a_zoom_of_one_leaves_every_writer_byte_identical(writer, fit):
    plain = COMPILE[writer](TILT, fit)
    assert recipe_digest(COMPILE[writer]({**TILT, "zoom": 1.0}, fit)) == recipe_digest(plain)
    framing = LETTERBOX if fit == "fit" and writer in LETTERBOXED else 1.0
    assert _clip(plain).transform.scale == pytest.approx(framing)


@pytest.mark.parametrize("writer", WRITERS)
def test_a_zoom_alone_changes_the_scale_and_needs_no_new_capability(writer):
    recipe = COMPILE[writer]({"zoom": 1.5})
    assert "clipTilt" not in recipe.required_capabilities
    clip = _clip(recipe)
    assert clip.transform.scale == pytest.approx(1.5)
    assert clip.transform.rotation_degrees == 0
    assert clip.backdrop is None
    assert recipe_digest(recipe) != recipe_digest(COMPILE[writer](None))


@pytest.mark.parametrize("writer", WRITERS)
def test_every_phone_writer_marks_a_tilted_recipe_clip_tilt(writer):
    assert "clipTilt" in COMPILE[writer](TILT).required_capabilities


@pytest.mark.parametrize("case", FIXTURE["valid"], ids=lambda case: case["name"])
def test_shared_fixture_valid_tilts_normalize_and_map_to_recipe_fields(case):
    assert canonical_row_tilt(case["tilt"]) == case["normalized"]
    patch, backdrop = clip_tilt_fields({"tilt": case["tilt"]})
    assert patch.get("rotation_degrees", 0) == case["recipe"]["rotation_degrees"]
    assert patch.get("scale", 1) == case["recipe"].get("scale", 1)
    assert (backdrop.model_dump(mode="json") if backdrop else None) == case["recipe"]["backdrop"]


@pytest.mark.parametrize("case", FIXTURE["compose"], ids=lambda case: case["name"])
def test_shared_fixture_zoom_composes_with_the_framing_scale(case):
    patch, _ = clip_tilt_fields({"tilt": case["tilt"]}, fit_scale=case["letterbox"])
    assert patch.get("scale", case["letterbox"]) == pytest.approx(case["scale"])


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


ZOOMED = {**TILT, "zoom": 1.5}


def test_refitting_a_zoomed_sideways_clip_keeps_its_zoom_and_changes_only_the_letterbox():
    bindings = guided_fixture()[1]
    zoomed = _guided(ZOOMED)
    assert _clip(zoomed).transform.scale == 1.5
    fitted = apply_landscape_fit(zoomed, bindings, "fit")
    assert _clip(fitted).transform.scale == pytest.approx(LETTERBOX * 1.5)
    assert apply_landscape_fit(fitted, bindings, "fit") is fitted
    assert _clip(apply_landscape_fit(fitted, bindings, "fill")).transform.scale == 1.5
    assert _clip(fitted).transform.rotation_degrees == 15.0


def test_a_zoom_out_is_not_read_as_a_letterbox():
    assert landscape_fit_from_recipe(_guided({"zoom": 0.5})) == "fill"
    assert landscape_fit_from_recipe(_guided({"zoom": 0.5}, "fit")) == "fit"
    assert landscape_fit_from_recipe(_guided({"zoom": 2.0})) == "fill"
    assert landscape_fit_from_recipe(_guided(ZOOMED, "fit")) == "fit"
    assert landscape_fit_from_recipe(_guided(None, "fit")) == "fit"
    assert landscape_fit_from_recipe(_guided(None)) == "fill"


@pytest.mark.parametrize(
    ("scale", "letterbox", "expected"),
    [
        (1.0, 1.0, (1.0, 1.0)),
        (1.0, LETTERBOX, (1.0, 1.0)),
        (LETTERBOX, LETTERBOX, (LETTERBOX, 1.0)),
        (LETTERBOX * 1.5, LETTERBOX, (LETTERBOX, 1.5)),
        (1.5, LETTERBOX, (1.0, 1.5)),
        (0.5, LETTERBOX, (1.0, 0.5)),
        (LETTERBOX * 0.5, LETTERBOX, (LETTERBOX, 0.5)),
        (3.0, 1.0, (1.0, 3.0)),
        (0.4, 1.0, (0.4, 1.0)),
    ],
)
def test_the_clip_scale_splits_into_its_framing_and_its_zoom(scale, letterbox, expected):
    base, zoom = split_clip_scale(scale, letterbox)
    assert (base, zoom) == pytest.approx(expected)


def test_a_voiceover_slot_projects_its_zoom_back_to_the_editor_row():
    for fit, bindings in (("fill", voiceover_cut._BINDINGS), ("fit", None)):
        recipe = _voiceover_cut("voiceover", ZOOMED, fit)
        pool = voiceover_cut._POOL
        sources = (
            list(voiceover_cut._BINDINGS)
            if bindings
            else [_sideways(b) for b in voiceover_cut._BINDINGS]
        )
        slot = voiceover_ai_timeline(recipe, sources, pool)["slots"][0]
        assert slot["tilt"] == canonical_row_tilt(ZOOMED)


def test_a_surviving_voiceover_slot_keeps_its_zoom_through_a_crop_and_a_later_cut():
    recipe = _voiceover_cut("voiceover", ZOOMED)
    slots = voiceover_ai_timeline(recipe, list(voiceover_cut._BINDINGS), voiceover_cut._POOL)[
        "slots"
    ]
    for slot in slots:
        slot.pop("tilt", None)
    again = replace_voiceover_cut(
        recipe,
        archetype="voiceover",
        slots=slots,
        bindings=voiceover_cut._BINDINGS,
        pool=voiceover_cut._POOL,
    )
    assert _clip(again).transform.scale == 1.5
    slots[0]["source_crop"] = {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5}
    cropped = replace_voiceover_cut(
        recipe,
        archetype="voiceover",
        slots=slots,
        bindings=voiceover_cut._BINDINGS,
        pool=voiceover_cut._POOL,
    )
    assert _clip(cropped).transform.scale == 1.5
    assert _clip(cropped).transform.rotation_degrees == 15.0
    slots[0]["tilt"] = None
    straight = replace_voiceover_cut(
        cropped,
        archetype="voiceover",
        slots=slots,
        bindings=voiceover_cut._BINDINGS,
        pool=voiceover_cut._POOL,
    )
    assert _clip(straight).transform.scale == 1


def test_a_lane_save_keeps_the_zoom_the_creator_set_on_a_talking_clip():
    from app.services.phone_editor import _previous_clip_tilts

    for fit in ("fill", "fit"):
        recipe = _subtitled(ZOOMED, fit)
        assert _previous_clip_tilts(recipe) == {"clip-0": canonical_row_tilt(ZOOMED)}


def test_the_flag_is_dark_by_default():
    assert settings.media_tilt_enabled is False
