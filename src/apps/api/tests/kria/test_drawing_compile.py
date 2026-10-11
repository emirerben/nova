"""KRI-589: editor ``drawings`` -> pinned ``drawing_layers``, against the shared fixture.

``tests/fixtures/drawing-parity/compile_v1.json`` is asserted by this Python reference AND by
the Swift ``NativeEditorRenderCompiler`` (what the creator previews on the phone is what the
server pins for export). The second half runs the same fixture through the real phone Save
path, so the wiring around the contract function is covered too, not only the function.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.kria.device_render import make_device_request
from app.kria.portable_drawing import compile_drawing_layers
from app.pipeline.phone_recipe_shared import with_drawings
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.routes import generative_jobs as gj
from app.services.device_render import device_status, pin_device_request
from app.services.phone_editor import prepare_phone_editor_commit
from app.services.phone_sources import PHONE_SOURCES_FIELD
from tests.pipeline.test_phone_subtitled_plan import _CUES, _binding

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[5]
        / "tests"
        / "fixtures"
        / "drawing-parity"
        / "compile_v1.json"
    ).read_text()
)
CASES = FIXTURE["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_reference_compiler_matches_the_shared_fixture(case: dict) -> None:
    layers = compile_drawing_layers(
        case["drawings"], timeline_duration_s=case["timeline_duration_s"]
    )
    assert [layer.model_dump(mode="json") for layer in layers] == case["expected_layers"]


def _device_job(monkeypatch):
    """A phone `subtitled` variant pinned exactly as the worker leaves it (10 s)."""
    monkeypatch.setattr(gj.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_subtitled_editor_lanes_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(gj.settings, "sound_effects_enabled", True)
    monkeypatch.setattr(gj.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [
            "basicComposition",
            "local1080Export",
            "positionedText",
            "animatedText",
            "audioMix",
            "soundEffects",
            "visualBlocks",
            "alphaOverlay",
            "stillImages",
            "visualVideos",
            "freehandDrawing",
        ],
    )
    bindings = (_binding(duration_s=10.0),)
    recipe = compile_phone_subtitled_plan(bindings, caption_cues=_CUES)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        assembly_plan={
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            "variants": [
                {
                    "variant_id": "subtitled",
                    "resolved_archetype": "subtitled",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": recipe.duration,
                    "caption_cues": _CUES,
                    "voiceover_caption_style": "sentence",
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe),
        base_generation="first",
    )
    return job, recipe


@pytest.mark.parametrize(
    "case",
    [c for c in CASES if c["timeline_duration_s"] >= 10],
    ids=lambda case: case["name"],
)
def test_fixture_through_the_real_phone_save(monkeypatch, case: dict) -> None:
    """The cases whose timeline is the fixture job's own 10 s run the whole Save path."""
    job, recipe = _device_job(monkeypatch)
    assert recipe.duration == pytest.approx(case["timeline_duration_s"])

    def prepare(staged):
        staged.assembly_plan["variants"][0]["drawings"] = case["drawings"]
        return {
            "has_render_section": True,
            "sections": {"drawings": True},
            "generation": "second",
        }

    prepare_phone_editor_commit(job, "subtitled", prepare=prepare)

    pinned = device_status(job, "subtitled").request.recipe
    assert [layer.model_dump(mode="json") for layer in pinned.drawing_layers] == case[
        "expected_layers"
    ]
    assert ("freehandDrawing" in pinned.required_capabilities) is bool(case["expected_layers"])


def _canonical(recipe) -> dict:
    dumped = recipe.model_dump(mode="json")
    dumped["required_capabilities"] = sorted(dumped["required_capabilities"])
    return dumped


def test_with_drawings_clamps_to_each_cut_and_strips_cleanly(monkeypatch) -> None:
    """A drawing is stored once and re-clamped on every build: the same lane yields a clamped
    layer on a shorter cut and the full one on a longer cut; stripping restores the recipe."""
    _job, recipe = _device_job(monkeypatch)
    stored = [CASES[0]["drawings"][0]]  # 1.0 -> 4.0 s
    full = with_drawings(recipe, stored)
    assert [(layer.start, layer.end) for layer in full.drawing_layers] == [(1.0, 4.0)]
    assert full.required_capabilities >= {"freehandDrawing"}

    short = compile_phone_subtitled_plan((_binding(duration_s=3.0),), caption_cues=[])
    clamped = with_drawings(short, stored)
    assert [(layer.start, layer.end) for layer in clamped.drawing_layers] == [(1.0, short.duration)]

    stripped = with_drawings(full, None)
    assert stripped.drawing_layers == []
    assert "freehandDrawing" not in stripped.required_capabilities
    # `required_capabilities` is a set: its list order in a dump follows the hash seed.
    assert _canonical(stripped) == _canonical(recipe)


def test_a_sliver_window_compiles_and_never_breaks_a_later_save(monkeypatch) -> None:
    """A 0.15 s window is legal; its draw duration clamps to the window instead of raising."""
    _job, recipe = _device_job(monkeypatch)
    sliver = {**CASES[0]["drawings"][0], "id": "sliver", "start_s": 1.0, "end_s": 1.15}
    layers = with_drawings(recipe, [sliver]).drawing_layers
    assert [(layer.id, round(layer.draw_duration, 2)) for layer in layers] == [("sliver", 0.15)]
