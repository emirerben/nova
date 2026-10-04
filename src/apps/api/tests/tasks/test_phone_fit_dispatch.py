"""KRI-285: the worker reads the creator's bars/crop choice, hands it to the
guided compiler, and persists ``variant["landscape_fit"]``."""

import pytest

from app.pipeline import phone_guided_plan
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks import test_phone_guided_dispatch as guided
from tests.tasks import test_phone_montage_dispatch as montage

_FIT_SCALE = 0.31640625


@pytest.mark.parametrize(
    "candidates,expected",
    [
        (
            {"creator_render_shape": {"output_orientation": "portrait", "landscape_fit": "fit"}},
            "fit",
        ),
        ({"creator_render_shape": {"landscape_fit": "fill"}}, "fill"),
        ({"creator_render_shape": {"landscape_fit": "stretch"}}, "fill"),
        ({"creator_render_shape": {"landscape_fit": None}}, "fill"),
        ({"creator_render_shape": {}}, "fill"),
        ({"creator_render_shape": None}, "fill"),
        ({"creator_render_shape": "garbage"}, "fill"),
        ({"landscape_fit": "fit"}, "fill"),  # the item default is NOT an explicit choice
        ({}, "fill"),
        (None, "fill"),
    ],
)
def test_creator_landscape_fit_only_reads_the_explicit_choice(candidates, expected):
    assert gb._creator_landscape_fit(candidates) == expected


def _spy_compile(monkeypatch):
    seen: list[str] = []
    real = phone_guided_plan.compile_phone_guided_plan

    def spy(*args, **kwargs):
        seen.append(kwargs.get("landscape_fit"))
        return real(*args, **kwargs)

    monkeypatch.setattr(phone_guided_plan, "compile_phone_guided_plan", spy)
    return seen


def test_guided_call_site_passes_the_explicit_fit_and_persists_it(monkeypatch):
    job, _snapshot, _session, _planner, _cloud = guided.setup(monkeypatch)
    job.all_candidates = {"creator_render_shape": {"landscape_fit": "fit"}}
    seen = _spy_compile(monkeypatch)

    gb._run_generative_job(str(job.id))

    assert seen == ["fit"]
    variant = job.assembly_plan["variants"][0]
    assert variant["landscape_fit"] == "fit"
    clips = device_status(job, "guided_story").request.recipe.tracks[0].clips
    assert clips[0].transform.scale == _FIT_SCALE  # the fixture source is 1920x1080


def test_guided_call_site_without_an_explicit_choice_stays_fill(monkeypatch):
    job, _snapshot, _session, _planner, _cloud = guided.setup(monkeypatch)
    job.all_candidates = {"landscape_fit": "fit"}  # item default, not a creator choice
    seen = _spy_compile(monkeypatch)

    gb._run_generative_job(str(job.id))

    assert seen == ["fill"]
    assert job.assembly_plan["variants"][0]["landscape_fit"] == "fill"
    clips = device_status(job, "guided_story").request.recipe.tracks[0].clips
    assert clips[0].transform.scale == 1


@pytest.mark.parametrize("fit", ["fit", "fill"])
def test_voiceover_montage_variant_persists_its_landscape_fit(monkeypatch, fit):
    job, _snapshot, _session, _bindings, _cloud = montage.setup(monkeypatch)
    job.all_candidates = {**job.all_candidates, "landscape_fit": fit}

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert job.assembly_plan["variants"][0]["landscape_fit"] == fit
