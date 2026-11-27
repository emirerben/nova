"""KRI-1613: an intro scene's sound through the REAL `_run_phone_subtitled_job` to the recipe the
phone exports.

The harness of `test_intro_scene_model_worker.py`: the real agent parser, pin, fit, compiler,
placement and Talking recipe compile; only the provider transport, the face sampler, the library
lookup (`_intro_sound_catalog_id`, checked on real Postgres in tests/services/test_sfx_library.py)
and the catalog asset probe are replayed. Times are read from the compiled recipe: the cue must
start on the frame its title line's text layer starts, and with `accent_after` that is 0.6 s in.

Ways it could fail (intro_scene M13-M16), a test each:
* the cue misses the entrance it belongs to, or the accent-only cue also sounds on the solid line;
* one sound per line where lines enter together;
* a closed gate (the kill switch, an unverified `soundEffects`, the SFX lane), a silent scene, her
  "no sound effects", a library row that is missing or unplayable: any sound, or a failed render;
* a sound she placed is stacked on by the intro sound;
* the editor cannot see the row by its id (a Save could not re-pin it).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agents import _model_client
from app.pipeline import intro_scene as scene_mod
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_subtitled_lanes import PHONE_SUBTITLED_LANES_FIELD
from app.pipeline.title_family import INTRO_SOUND_ID_PREFIX
from app.services import pipeline_trace
from app.services.device_render import device_status
from app.services.phone_subtitled_editor import project_phone_subtitled_editor_sections
from app.tasks import generative_build as gb
from tests.tasks.test_intro_scene_model_worker import COMPOSE, Routing, _model_job
from tests.tasks.test_intro_scene_worker import EN, TR, _check, _title_rows
from tests.tasks.test_phone_subtitled_narrated_dispatch import _fake_resolve_sfx, _lanes_features

LATE = scene_mod.STAGGERS["accent_after"]
VOICES = {EN: ["solid"] * 4 + ["accent"] * 2, TR: ["solid"] * 6 + ["accent"] * 2}
LIBRARY = {slug: f"lib-{slug}" for slug in scene_mod.SOUNDS if slug != scene_mod.SILENT}


@pytest.fixture
def route(monkeypatch):
    def install(answer: str) -> Routing:
        client = Routing(answer)
        monkeypatch.setattr(_model_client, "default_client", lambda: client)
        return client

    return install


def _open_gates(monkeypatch) -> list[dict]:
    monkeypatch.setattr(gb.settings, "intro_scene_sound_enabled", True)
    monkeypatch.setattr(gb.settings, "sound_effects_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_subtitled_editor_lanes_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        _lanes_features("stillImages", "visualBlocks", "alphaOverlay", "soundEffects"),
    )
    monkeypatch.setattr(gb, "_resolve_phone_sound_effect", _fake_resolve_sfx)
    monkeypatch.setattr(gb, "_intro_sound_catalog_id", LIBRARY.get)
    events: list[dict] = []
    real = pipeline_trace.record_pipeline_event

    def record(stage, event, data=None):  # noqa: ANN001, ANN202
        if event == "intro_scene_sound":
            events.append(data)
        return real(stage, event, data)

    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", record)
    return events


def _render(monkeypatch, install, title: str, language: str, **answer: Any):
    install(json.dumps({**COMPOSE, "voices": VOICES[title], "entrance": "pop", **answer}))
    job = _model_job(monkeypatch, title, language)
    return job


def _run(job) -> Any:
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.error_detail
    return device_status(job, "subtitled").request.recipe


def _sfx(recipe) -> list:
    return [clip for track in recipe.tracks if track.id == "sfx" for clip in track.clips]


def _entrance(recipe, row: dict) -> float:
    [layer] = [
        layer
        for layer in recipe.text_layers
        if "".join(run.text for run in layer.runs).replace(" ", "").casefold()
        == row["text"].replace(" ", "").casefold()
    ]
    return float(layer.start)


def _accent_row(job) -> dict:
    [row] = [
        r for r in _title_rows(job) if r["source_params"]["title_look"]["style"] == "intro_accent"
    ]
    return row


@pytest.mark.parametrize(("title", "language"), [(EN, "en"), (TR, "tr")], ids=["en", "tr"])
def test_the_accent_cue_starts_on_the_late_accent_entrance(monkeypatch, route, title, language):
    job = _render(
        monkeypatch,
        route,
        title,
        language,
        stagger="accent_after",
        sound="accent-pop",
        sound_cue="accent",
    )
    _open_gates(monkeypatch)
    recipe = _run(job)

    accent = _accent_row(job)
    [clip] = _sfx(recipe)
    assert clip.timeline_start == pytest.approx(_entrance(recipe, accent), abs=1e-6)
    assert clip.timeline_start == pytest.approx(LATE, abs=1e-6)
    assert clip.id == f"sfx-{INTRO_SOUND_ID_PREFIX}{accent['id']}"
    assert clip.source_asset_id == "sfx-lib-accent-pop"
    _check(job, title)  # the approval check still finds her words
    sections = project_phone_subtitled_editor_sections(
        job.assembly_plan, job.assembly_plan["variants"][0]
    )
    assert [(row["id"], row["at_s"]) for row in sections["sound_effects"]] == [
        (f"{INTRO_SOUND_ID_PREFIX}{accent['id']}", pytest.approx(LATE))
    ]


def test_one_per_entrance_sounds_the_title_then_the_late_accent(monkeypatch, route):
    job = _render(
        monkeypatch, route, EN, "en", stagger="accent_after", sound="soft-pop", sound_cue="each"
    )
    _open_gates(monkeypatch)
    recipe = _run(job)
    rows = sorted(_title_rows(job), key=lambda r: r["start_s"])
    assert [clip.timeline_start for clip in _sfx(recipe)] == pytest.approx(
        [_entrance(recipe, row) for row in rows]
    )
    assert [clip.timeline_start for clip in _sfx(recipe)] == pytest.approx([0.0, LATE])


def test_lines_entering_together_share_one_sound(monkeypatch, route):
    job = _render(monkeypatch, route, EN, "en", sound="click", sound_cue="each")
    _open_gates(monkeypatch)
    recipe = _run(job)
    [clip] = _sfx(recipe)
    assert clip.timeline_start == pytest.approx(0.0)
    assert len(_title_rows(job)) == 2


def _silent(monkeypatch, route, close, **answer) -> tuple[Any, Any, list[dict]]:
    job = _render(
        monkeypatch,
        route,
        EN,
        "en",
        **{"stagger": "accent_after", "sound": "accent-pop", "sound_cue": "accent", **answer},
    )
    events = _open_gates(monkeypatch)
    close(monkeypatch, job)
    recipe = _run(job)
    assert _sfx(recipe) == []
    assert len(_title_rows(job)) == 2  # the scene is still drawn, only silent
    return job, recipe, events


def _flag_off(monkeypatch, job):
    monkeypatch.setattr(gb.settings, "intro_scene_sound_enabled", False)


def _sfx_unverified(monkeypatch, job):
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        _lanes_features("stillImages", "visualBlocks", "alphaOverlay"),
    )


def _effects_off(monkeypatch, job):
    monkeypatch.setattr(gb.settings, "sound_effects_enabled", False)


def _said_no(monkeypatch, job):
    monkeypatch.setattr(gb, "_no_sound_effects_asked", lambda snapshot: True)


@pytest.mark.parametrize(
    "close",
    [_flag_off, _sfx_unverified, _effects_off, _said_no],
    ids=["kill_switch_off", "sound_effects_unverified", "sound_effects_off", "she_said_no"],
)
def test_a_closed_gate_is_silence_and_rows_declare_nothing(monkeypatch, route, close):
    job, _recipe, events = _silent(monkeypatch, route, close)
    assert all("sound" not in r["source_params"]["title_look"] for r in _title_rows(job))
    assert events == []  # nothing declared, so nothing to place


def test_a_silent_scene_is_silence(monkeypatch, route):
    _silent(monkeypatch, route, lambda *_: None, sound="none", sound_cue="none")


def test_a_missing_library_row_is_silence_and_says_so(monkeypatch, route):
    def missing(monkeypatch, job):
        monkeypatch.setattr(gb, "_intro_sound_catalog_id", lambda slug: None)

    _job, _recipe, events = _silent(monkeypatch, route, missing)
    assert events and {cue["skipped"] for cue in events[-1]["cues"]} == {"catalog_missing"}


def test_an_unplayable_library_row_is_silence_and_the_render_completes(monkeypatch, route):
    def unplayable(monkeypatch, job):
        def refuse(sfx):  # noqa: ANN001, ANN202
            raise UnsupportedPhonePlan("gone", capability="soundEffects")

        monkeypatch.setattr(gb, "_resolve_phone_sound_effect", refuse)

    _job, _recipe, events = _silent(monkeypatch, route, unplayable)
    assert events[-1]["cues"][0]["skipped"].startswith("unplayable")


def test_her_own_sound_keeps_its_slot(monkeypatch, route):
    job = _render(
        monkeypatch,
        route,
        EN,
        "en",
        stagger="accent_after",
        sound="accent-pop",
        sound_cue="accent",
    )
    events = _open_gates(monkeypatch)
    job.assembly_plan = {
        **job.assembly_plan,
        PHONE_SUBTITLED_LANES_FIELD: {
            "sound_effects": [{"id": "hers", "catalog_id": "fx-hers", "at_s": 1.0}]
        },
    }
    recipe = _run(job)
    assert [(clip.id, clip.timeline_start) for clip in _sfx(recipe)] == [("sfx-hers", 1.0)]
    assert events[-1]["cues"][0]["skipped"] == "taken"
