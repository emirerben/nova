"""KRI-589: freehand drawings through the real editor-commit path.

Covers the Save contract on every device archetype (authored, guided, subtitled, narrated,
voiceover montage), the capability map, the kill switch, the cloud lane and the old-app
protection (426). Fixtures are the ones each archetype's own commit tests already use, so
the recipes here are the shapes the workers really pin.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.routes.generative_jobs as gj
from app.kria.device_render import make_device_request
from app.services import phone_editor
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.device_render import device_status, pin_device_request
from app.services.phone_editor import prepare_phone_editor_commit
from tests.routes import test_phone_editor_commit as guided_fixtures
from tests.routes import test_phone_narrated_editor_commit as narrated_fixtures
from tests.routes import test_phone_subtitled_editor_commit as subtitled_fixtures
from tests.routes import test_phone_voiceover_cut_edits as cut_fixtures
from tests.routes import test_phone_voiceover_editor_lanes as voiceover_fixtures
from tests.routes.test_editor_commit import _arm_every_editor_lane, _job

_COMPILE = json.loads(
    (
        Path(__file__).resolve().parents[5]
        / "tests"
        / "fixtures"
        / "drawing-parity"
        / "compile_v1.json"
    ).read_text()
)
# The fixture's identity case: one drawing, 1.0 -> 4.0 s, and the layer it must compile to.
CASE = next(case for case in _COMPILE["cases"] if case["name"] == "identity-inside-timeline")
DRAWING = CASE["drawings"][0]
LAYER = CASE["expected_layers"][0]
FIRST_ID = DRAWING["id"]
SECOND = {**DRAWING, "id": "second", "start_s": 5.0, "end_s": 6.0}


@pytest.fixture(autouse=True)
def _reset_protocol():
    clear_request_context()
    yield
    clear_request_context()


def _verify_drawings(monkeypatch) -> None:
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [*gj.settings.phone_render_verified_features, "freehandDrawing"],
    )


# --- one environment per device archetype ------------------------------------------------


class Env:
    """A pinned device variant plus the two saves the tests need from it."""

    def __init__(self, job, variant_id, save, other_section, window=(1.0, 4.0)):
        self.job = job
        self.variant_id = variant_id
        self._save = save
        self.other_section = other_section
        # The drawing's window: a short fixture video (the guided story is 3 s) gets an earlier one.
        self.drawing = {**DRAWING, "start_s": window[0], "end_s": window[1]}
        self.layer = {**LAYER, "start": window[0], "end": window[1]}

    @property
    def variant(self) -> dict:
        return next(
            v for v in self.job.assembly_plan["variants"] if v["variant_id"] == self.variant_id
        )

    @property
    def recipe(self):
        return device_status(self.job, self.variant_id).request.recipe

    @property
    def revision(self) -> int:
        return device_status(self.job, self.variant_id).request.identity.recipe_revision

    def save(self, **sections):
        return self._save(self, **sections)


def _subtitled(monkeypatch) -> Env:
    job = subtitled_fixtures.phone_job(monkeypatch)
    job.all_candidates = {}
    _verify_drawings(monkeypatch)

    def save(env, **sections):
        return gj.prepare_editor_commit(
            env.job,
            "subtitled",
            gj.EditorCommitRequest(
                base_generation=gj.variant_render_baseline(env.variant), **sections
            ),
            user_id="owner",
            plan_item_id="item",
        )

    return Env(job, "subtitled", save, {"caption_cues": subtitled_fixtures._CUES})


def _narrated(monkeypatch) -> Env:
    job = narrated_fixtures.phone_job(monkeypatch)
    _verify_drawings(monkeypatch)

    def save(env, **sections):
        return gj.prepare_editor_commit(
            env.job,
            "narrated",
            gj.EditorCommitRequest(
                base_generation=gj.variant_render_baseline(env.variant), **sections
            ),
            user_id="owner",
            plan_item_id="item",
        )

    return Env(job, "narrated", save, {"caption_cues": narrated_fixtures._CUES})


def _voiceover(monkeypatch) -> Env:
    voiceover_fixtures._enable(monkeypatch)
    job, variant_id = voiceover_fixtures.voiceover_job(archetype="voiceover")
    clear_request_context()  # its `_enable` declares an old build; saves here are server-side
    _verify_drawings(monkeypatch)

    def save(env, **sections):
        return gj.prepare_editor_commit(
            env.job,
            env.variant_id,
            gj.EditorCommitRequest(
                base_generation=gj.variant_render_baseline(env.variant), **sections
            ),
            user_id="owner",
            plan_item_id="item",
        )

    return Env(job, variant_id, save, {"sound_effects": []})


def _guided(monkeypatch) -> Env:
    job, _revision = guided_fixtures._guided_shape_job(monkeypatch)
    _verify_drawings(monkeypatch)

    def save(env, **sections):
        variant = env.variant
        return gj.prepare_editor_commit(
            env.job,
            "guided_story",
            gj.EditorCommitRequest(
                base_generation=gj.variant_render_baseline(variant),
                guided_revision_number=gj._guided_v2_revision(env.job, variant)["revision_number"],
                **sections,
            ),
        )

    element = copy.deepcopy(job.assembly_plan["variants"][0]["text_elements"][0])
    return Env(
        job,
        "guided_story",
        save,
        {"text_elements": [{**element, "text": "Edited"}]},
        window=(0.5, 2.5),
    )


def _authored(monkeypatch) -> Env:
    """An authored phone timeline; its Save is driven through the phone compiler directly."""
    from tests.pipeline import test_authored_timeline as authored

    monkeypatch.setattr(gj.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "positionedText", "animatedText", "audioMix"],
    )
    _verify_drawings(monkeypatch)
    job, variant, previous = authored.fixture()
    variant.update(
        render_destination="device", render_status="awaiting_device", render_generation_id="g0"
    )
    job.id = uuid.uuid4()
    job.user_id = uuid.uuid4()
    job.status = "awaiting_device"
    job.assembly_plan["variants"] = [variant]
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="original_text", revision=1, recipe=previous),
        base_generation="g0",
    )
    counter = iter(range(1, 100))

    def save(env, **sections):
        generation = f"g{next(counter)}"

        def prepare(staged):
            row = staged.assembly_plan["variants"][0]
            if "drawings" in sections:
                row["drawings"] = sections["drawings"] or None
            if "text_elements" in sections:
                row["text_elements"] = sections["text_elements"]
            row["render_generation_id"] = generation
            return {
                "has_render_section": True,
                "sections": {key: True for key in sections},
                "generation": generation,
            }

        return prepare_phone_editor_commit(env.job, env.variant_id, prepare=prepare)

    return Env(job, "original_text", save, {"text_elements": []}, window=(0.5, 2.5))


ARCHETYPES = {
    "subtitled": _subtitled,
    "narrated": _narrated,
    "voiceover": _voiceover,
    "guided": _guided,
    "authored": _authored,
}


def _compiled(env: Env) -> list[dict]:
    return [layer.model_dump(mode="json") for layer in env.recipe.drawing_layers]


@pytest.mark.parametrize("archetype", sorted(ARCHETYPES))
def test_a_drawing_save_pins_the_compiled_layer_and_every_later_save_keeps_it(
    monkeypatch, archetype
) -> None:
    env = ARCHETYPES[archetype](monkeypatch)
    before = env.recipe
    assert before.drawing_layers == [] and "freehandDrawing" not in before.required_capabilities
    first = env.revision

    prep = env.save(drawings=[env.drawing])

    assert prep["render_destination"] == "device" and prep["sections"]["drawings"] is True
    assert env.revision == first + 1
    assert [row["id"] for row in env.variant["drawings"]] == [DRAWING["id"]]
    assert "freehandDrawing" in env.recipe.required_capabilities
    # The recipe is what the contract compiler says: the same fixture the phone is tested on.
    expected = env.layer
    assert _compiled(env) == [expected]

    # A Save that carries NO drawings section (the KRI-467 title rule) still compiles them.
    env.save(**env.other_section)
    assert env.revision == first + 2
    assert _compiled(env) == [expected]

    # Taking the drawing off removes the layers AND the capability the old build can't decode.
    env.save(drawings=[])
    assert env.recipe.drawing_layers == []
    assert "freehandDrawing" not in env.recipe.required_capabilities
    assert "drawing_layers" not in env.recipe.model_dump(mode="json")
    assert not env.variant.get("drawings")


@pytest.mark.parametrize("archetype", sorted(ARCHETYPES))
def test_a_save_without_any_drawing_leaves_the_recipe_free_of_the_lane(
    monkeypatch, archetype
) -> None:
    env = ARCHETYPES[archetype](monkeypatch)
    env.save(**env.other_section)
    assert "drawing_layers" not in env.recipe.model_dump(mode="json")
    assert "freehandDrawing" not in env.recipe.required_capabilities


@pytest.mark.parametrize("archetype", ["narrated", "voiceover"])
def test_drawings_are_re_clamped_to_a_shorter_cut_and_restored_by_a_longer_one(
    monkeypatch, archetype
) -> None:
    """A voiceover cut is swapped into the PINNED recipe: the layers the old cut allowed past the
    new end must be re-derived from the stored lane, not carried (or the recipe is invalid)."""
    voiceover_fixtures._enable(monkeypatch)
    job, vid = voiceover_fixtures.voiceover_job(archetype=archetype)
    clear_request_context()
    _verify_drawings(monkeypatch)
    env = Env(
        job,
        vid,
        lambda env, **sections: gj.prepare_editor_commit(
            env.job,
            env.variant_id,
            gj.EditorCommitRequest(
                base_generation=gj.variant_render_baseline(env.variant), **sections
            ),
            user_id="owner",
            plan_item_id="item",
        ),
        {},
    )
    late = {**DRAWING, "id": "late", "start_s": 9.0, "end_s": 11.5}
    env.save(drawings=[DRAWING, late])
    assert env.recipe.duration == pytest.approx(12.0)
    assert {layer["id"]: layer["end"] for layer in _compiled(env)} == {
        FIRST_ID: 4.0,
        "late": 11.5,
    }

    slots = cut_fixtures._slots(job, vid)
    slots[-1]["duration_s"] = 2.0  # footage now ends at 10 s
    env.save(timeline_slots=slots)
    assert env.recipe.duration == pytest.approx(10.0)
    assert {layer["id"]: layer["end"] for layer in _compiled(env)} == {FIRST_ID: 4.0, "late": 10.0}
    # The stored lane is never rewritten by a cut...
    assert [row["end_s"] for row in env.variant["drawings"]] == [4.0, 11.5]

    slots[-1]["duration_s"] = 4.0  # ...so a longer cut brings the full window back.
    env.save(timeline_slots=slots)
    assert env.recipe.duration == pytest.approx(12.0)
    assert {layer["id"]: layer["end"] for layer in _compiled(env)} == {
        FIRST_ID: 4.0,
        "late": 11.5,
    }

    # A cut that leaves nothing of a drawing drops it from the recipe, not from the lane.
    slots[0]["duration_s"] = 4.0
    slots[1]["removed"] = True
    slots[2]["removed"] = True
    env.save(timeline_slots=slots)
    assert [layer["id"] for layer in _compiled(env)] == [FIRST_ID]
    assert len(env.variant["drawings"]) == 2


# --- guided: a drawing-only save must not re-clock the approved timing ----------------


def test_a_drawing_only_guided_save_keeps_the_approved_timing_program(monkeypatch) -> None:
    env = _guided(monkeypatch)
    approved = copy.deepcopy(env.job.assembly_plan["guided_story_execution_plan"])

    def _boom(*_args, **_kwargs):
        raise AssertionError("a drawings-only save must not re-clock the approved timeline")

    monkeypatch.setattr(phone_editor, "compile_guided_runtime_plan", _boom)

    env.save(drawings=[env.drawing])

    # The plan the recipe was compiled from is the approved one, moment for moment.
    saved = env.variant[phone_editor.PHONE_EDITOR_SAVED_PLAN_FIELD]
    assert saved["story_timeline"] == approved["story_timeline"]
    assert env.recipe.duration == pytest.approx(approved["resolved_duration_s"])
    assert [layer["id"] for layer in _compiled(env)] == [FIRST_ID]
    # The revision is the guided variant's canonical lanes: the drawing lives there too.
    revision = gj._guided_v2_revision(env.job, env.variant)
    assert [row["id"] for row in revision["drawings"]] == [FIRST_ID]
    assert revision["lane_hashes"]["drawings"]


def test_a_guided_timeline_save_re_clamps_drawings_and_keeps_them_in_the_revision(
    monkeypatch,
) -> None:
    env = _guided(monkeypatch)
    env.save(drawings=[env.drawing])  # 0.5 -> 2.5 s of a 3 s story
    segment = gj._guided_v2_revision(env.job, env.variant)["segments"][0]

    env.save(
        timeline_slots=[
            gj.TimelineSlotEdit(
                slot_id=segment["segment_id"], clip_index=0, in_s=2.2, duration_s=0.8
            )
        ]
    )

    assert env.recipe.duration == pytest.approx(0.8)
    [layer] = _compiled(env)
    assert (layer["start"], layer["end"]) == pytest.approx((0.5, 0.8))
    # The revision still holds the creator's full window, ready for a longer cut.
    revision = gj._guided_v2_revision(env.job, env.variant)
    assert [row["end_s"] for row in revision["drawings"]] == [2.5]


# --- kill switch ------------------------------------------------------------------------


def test_flag_off_refuses_new_drawings_but_keeps_playing_and_deleting_stored_ones(
    monkeypatch,
) -> None:
    env = _subtitled(monkeypatch)
    env.save(drawings=[DRAWING, SECOND])
    monkeypatch.setattr(gj.settings, "freehand_drawing_enabled", False)

    with pytest.raises(HTTPException) as added:
        env.save(drawings=[DRAWING, SECOND, {**DRAWING, "id": "c"}])
    assert (added.value.status_code, added.value.detail) == (422, {"code": "drawings_disabled"})
    with pytest.raises(HTTPException) as edited:
        env.save(drawings=[{**DRAWING, "scale": 2.0}, SECOND])
    assert edited.value.detail == {"code": "drawings_disabled"}

    # Stored drawings still compile into every later recipe...
    env.save(**env.other_section)
    assert [layer["id"] for layer in _compiled(env)] == [FIRST_ID, "second"]
    # ...an untouched echo of the lane is not an edit...
    env.save(drawings=[DRAWING, SECOND])
    # ...and removal stays open, as a reduced lane and as an explicit deletion.
    env.save(drawings=[SECOND])
    assert [layer["id"] for layer in _compiled(env)] == ["second"]
    env.save(editor_state_version=1, deletions=[{"kind": "drawing", "id": "second"}])
    assert env.recipe.drawing_layers == []
    assert not env.variant.get("drawings")


def test_an_unverified_device_refuses_new_drawings_like_the_kill_switch(monkeypatch) -> None:
    env = _narrated(monkeypatch)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [f for f in gj.settings.phone_render_verified_features if f != "freehandDrawing"],
    )
    with pytest.raises(HTTPException) as refused:
        env.save(drawings=[DRAWING])
    assert refused.value.detail == {"code": "drawings_disabled"}


# --- validation -------------------------------------------------------------------------


def test_save_names_the_cap_it_breaks_and_keeps_the_baseline(monkeypatch) -> None:
    env = _subtitled(monkeypatch)
    before = copy.deepcopy(env.job.assembly_plan)

    with pytest.raises(ValidationError, match="at most 12 items"):
        env.save(drawings=[{**DRAWING, "id": f"d{i}"} for i in range(13)])

    points = [[0.1 + i / 2000, 0.5] for i in range(1000)]
    stroke = {**DRAWING["strokes"][0], "points": points}
    heavy = [{**DRAWING, "id": f"h{i}", "strokes": [stroke] * 3} for i in range(3)]
    with pytest.raises(HTTPException) as too_heavy:
        env.save(drawings=heavy)
    assert too_heavy.value.detail["code"] == "drawings_invalid"
    assert "8000 points" in too_heavy.value.detail["reason"]

    with pytest.raises(HTTPException) as past_the_end:
        env.save(drawings=[{**DRAWING, "start_s": 8.0, "end_s": 40.0}])
    assert past_the_end.value.detail["code"] == "drawings_invalid"
    assert "variant duration" in past_the_end.value.detail["reason"]
    assert env.job.assembly_plan == before


def test_a_stored_drawing_left_past_a_shorter_cut_never_blocks_its_neighbours(monkeypatch) -> None:
    env = _subtitled(monkeypatch)
    env.save(drawings=[DRAWING, SECOND])
    env.variant["drawings"][1]["end_s"] = 60.0  # a cut shortened the video since it was saved

    env.save(drawings=[DRAWING, env.variant["drawings"][1]])

    assert [layer["id"] for layer in _compiled(env)] == [FIRST_ID, "second"]


# --- cloud ------------------------------------------------------------------------------


def test_a_cloud_variant_has_the_lane_closed_and_refuses_a_drawing(monkeypatch) -> None:
    _arm_every_editor_lane(monkeypatch)
    job = _job()
    variant = job.assembly_plan["variants"][0]

    assert gj._editor_capabilities(job, variant)["drawings"] == {
        operation: {"editable": False, "reason": "drawing_requires_phone_render"}
        for operation in ("add", "edit", "trim", "delete")
    }
    with pytest.raises(HTTPException) as refused:
        gj.prepare_editor_commit(
            job,
            variant["variant_id"],
            gj.EditorCommitRequest(
                base_generation=gj.variant_render_baseline(variant), drawings=[DRAWING]
            ),
        )
    assert refused.value.detail == {"code": "drawing_requires_phone_render"}

    # An untouched empty echo (a blanket-dirtied section) is not a request: nothing is written.
    prep = gj.prepare_editor_commit(
        job,
        variant["variant_id"],
        gj.EditorCommitRequest(base_generation=gj.variant_render_baseline(variant), drawings=[]),
    )
    assert prep["sections"]["drawings"] is False and prep["has_render_section"] is False


def test_save_refuses_a_drawing_on_a_device_shape_the_capability_map_closes(monkeypatch) -> None:
    """A stale capability map must not let a client persist a lane this variant cannot render:
    it would show in the editor and be missing from the export."""
    _arm_every_editor_lane(monkeypatch)
    _verify_drawings(monkeypatch)
    set_client_protocol(5)
    job = _job(resolved_archetype="speech_montage")
    variant = job.assembly_plan["variants"][0]
    variant["render_destination"] = "device"
    assert gj._editor_capabilities(job, variant)["drawings"]["add"]["reason"] == (
        "drawing_unsupported_for_this_edit"
    )
    with pytest.raises(HTTPException) as refused:
        gj.prepare_editor_commit(
            job,
            variant["variant_id"],
            gj.EditorCommitRequest(
                base_generation=gj.variant_render_baseline(variant), drawings=[DRAWING]
            ),
        )
    assert refused.value.status_code == 422
    assert refused.value.detail == {"code": "drawing_unsupported_for_this_edit"}


# --- capability map ---------------------------------------------------------------------


def _open() -> dict:
    return {"editable": True, "reason": None}


def test_capabilities_open_only_with_flag_verification_and_a_build_that_decodes_drawings(
    monkeypatch,
) -> None:
    env = _narrated(monkeypatch)
    set_client_protocol(5)

    def caps():
        return gj._editor_capabilities(env.job, env.variant)["drawings"]

    assert caps() == {"add": _open(), "edit": _open(), "trim": _open(), "delete": _open()}

    set_client_protocol(4)
    assert caps()["add"] == {"editable": False, "reason": "client_update_required"}
    assert caps()["delete"] == _open()
    set_client_protocol(5)

    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [f for f in gj.settings.phone_render_verified_features if f != "freehandDrawing"],
    )
    assert caps()["trim"] == {"editable": False, "reason": "drawing_unavailable"}
    _verify_drawings(monkeypatch)

    monkeypatch.setattr(gj.settings, "freehand_drawing_enabled", False)
    assert {name: caps()[name] for name in ("add", "edit", "trim")} == {
        name: {"editable": False, "reason": "drawings_disabled"} for name in ("add", "edit", "trim")
    }
    # The kill switch stops authoring, never removal.
    assert caps()["delete"] == _open()


def test_a_device_edit_with_no_save_path_keeps_the_lane_closed(monkeypatch) -> None:
    """A spoken-excerpt montage can't be recompiled by the server editor: advertise nothing."""
    _arm_every_editor_lane(monkeypatch)
    _verify_drawings(monkeypatch)
    set_client_protocol(5)
    job = _job(resolved_archetype="speech_montage")
    variant = {**job.assembly_plan["variants"][0], "render_destination": "device"}
    assert gj._editor_capabilities(job, variant)["drawings"]["add"] == {
        "editable": False,
        "reason": "drawing_unsupported_for_this_edit",
    }


# --- old-app protection -----------------------------------------------------------------


@pytest.mark.parametrize("protocol", [4, None])
def test_an_old_build_saving_a_variant_with_drawings_is_told_to_update(
    monkeypatch, protocol
) -> None:
    env = _subtitled(monkeypatch)
    env.save(drawings=[DRAWING])  # a worker / newer build, outside any app request
    pinned = copy.deepcopy(env.job.assembly_plan)

    set_client_protocol(protocol)
    with pytest.raises(HTTPException) as refused:
        env.save(**env.other_section)

    assert refused.value.status_code == 426
    assert refused.value.detail == {"code": "client_update_required", "feature": "drawing"}
    # Nothing was recompiled or written: the last good recipe stays pinned.
    assert env.job.assembly_plan == pinned


def test_the_minimum_build_and_workers_may_save_a_variant_with_drawings(monkeypatch) -> None:
    env = _subtitled(monkeypatch)
    env.save(drawings=[DRAWING])
    set_client_protocol(gj.settings.freehand_drawing_min_client_protocol)
    env.save(**env.other_section)
    assert [layer["id"] for layer in _compiled(env)] == [FIRST_ID]
    clear_request_context()  # chat edits and workers have no app build to qualify
    env.save(**env.other_section)
    assert [layer["id"] for layer in _compiled(env)] == [FIRST_ID]


def test_an_old_build_may_still_save_a_variant_that_never_had_drawings(monkeypatch) -> None:
    env = _subtitled(monkeypatch)
    set_client_protocol(4)
    env.save(**env.other_section)
    assert env.recipe.drawing_layers == []
