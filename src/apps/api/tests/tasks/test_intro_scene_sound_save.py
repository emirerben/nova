"""KRI-1613: the intro scene's sound through the editor's Saves, after a REAL worker render.

Each test renders a Talking edit with the real worker (the harness of
`test_intro_scene_sound_worker.py`), then saves it through the real editor-commit route
(`generative_jobs.prepare_editor_commit`), so the first Save derives its lanes from the pinned
recipe exactly as on a phone. Times are read from the recipe the Save pins.

Ways it could fail (intro_scene M14, M17), a test each:
* a Save that never touches the title moves or loses the sound (the first Save keys it by id);
* the title retimed (or a Range trim of the take's first second) leaves the sound where the line
  was, or moves it through source time into footage that no longer plays;
* a sound she dragged off the entrance stays off it;
* the title removed, or rewritten as one plain line, keeps its sound;
* a sound she deleted comes back on a later Save, or the switch turned off strips saved sounds.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.agents import _model_client
from app.pipeline.title_family import (
    INTRO_SOUND_ID_PREFIX,
    intro_sound_cues,
    retime_title_rows,
)
from app.routes import generative_jobs as gj
from app.services.device_render import device_status
from app.services.phone_subtitled_editor import project_phone_subtitled_editor_sections
from app.tasks import generative_build as gb
from tests.tasks.test_intro_scene_model_worker import Routing
from tests.tasks.test_intro_scene_sound_worker import LATE, _accent_row, _open_gates, _render
from tests.tasks.test_intro_scene_worker import EN, _title_rows


@pytest.fixture
def rendered(monkeypatch):
    def install(answer: str) -> Routing:
        client = Routing(answer)
        monkeypatch.setattr(_model_client, "default_client", lambda: client)
        return client

    job = _render(
        monkeypatch,
        install,
        EN,
        "en",
        stagger="accent_after",
        sound="accent-pop",
        sound_cue="accent",
    )
    _open_gates(monkeypatch)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.error_detail
    job.current_phase = None
    sound_id = f"{INTRO_SOUND_ID_PREFIX}{_accent_row(job)['id']}"
    assert _sounds(job) == [(sound_id, pytest.approx(LATE))]
    return job, sound_id


def _sounds(job) -> list[tuple[str, float]]:
    recipe = device_status(job, "subtitled").request.recipe
    return [
        (clip.id.removeprefix("sfx-"), clip.timeline_start)
        for track in recipe.tracks
        if track.id == "sfx"
        for clip in track.clips
    ]


def _save(job, **sections: Any) -> None:
    variant = job.assembly_plan["variants"][0]
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(base_generation=variant["render_generation_id"], **sections),
        user_id="owner",
        plan_item_id="item",
    )


def _lane(job) -> list[dict]:
    """The sound-effect section as the editor shows it (and sends it back on a Save)."""
    rows = project_phone_subtitled_editor_sections(
        job.assembly_plan, job.assembly_plan["variants"][0]
    )["sound_effects"]
    return [
        {**row, "src_gcs_path": f"sound-effects/{row['sound_effect_id']}/a.m4a"} for row in rows
    ]


def _text(job) -> list[dict]:
    return copy.deepcopy(job.assembly_plan["variants"][0]["text_elements"])


def test_a_save_that_never_touches_the_title_keeps_the_sound_on_the_entrance(rendered):
    job, sound_id = rendered
    hers = {**_lane(job)[0], "id": "hers", "at_s": 4.0}
    _save(job, sound_effects=[*_lane(job), hers])
    assert _sounds(job) == [(sound_id, pytest.approx(LATE)), ("hers", pytest.approx(4.0))]


def test_a_retimed_title_takes_its_sound_with_it(rendered):
    job, sound_id = rendered
    text = _text(job)
    title = [row for row in text if row["id"].startswith("opening-title")]
    moved = {row["id"]: row for row in retime_title_rows(title, 0.5, 3.0)}
    _save(job, text_elements=[moved.get(row["id"], row) for row in text])
    assert _sounds(job) == [(sound_id, pytest.approx(0.5 + LATE))]


def test_a_sound_dragged_off_the_entrance_goes_back_on_it(rendered):
    job, sound_id = rendered
    _save(job, sound_effects=[{**row, "at_s": 2.0} for row in _lane(job)])
    assert _sounds(job) == [(sound_id, pytest.approx(LATE))]


def test_a_removed_title_takes_its_sound_away_and_keeps_hers(rendered):
    job, _sound_id = rendered
    hers = {**_lane(job)[0], "id": "hers", "at_s": 4.0}
    _save(job, sound_effects=[*_lane(job), hers])
    text = [row for row in _text(job) if not row["id"].startswith("opening-title")]
    _save(job, text_elements=text)
    assert _sounds(job) == [("hers", pytest.approx(4.0))]


def test_a_title_no_longer_drawn_by_the_scene_loses_its_sound(rendered):
    job, _sound_id = rendered
    text = _text(job)
    for row in text:  # another look: the lines no longer declare the scene's sound
        row["source_params"]["title_look"].pop("sound", None)
        row["source_params"]["title_look"].pop("sound_cue", None)
    _save(job, text_elements=text)
    assert _sounds(job) == []


def test_a_sound_she_deleted_never_comes_back(rendered):
    job, _sound_id = rendered
    _save(job, sound_effects=[])
    assert _sounds(job) == []
    _save(job, text_elements=_text(job))
    assert _sounds(job) == []


def test_the_switch_turned_off_keeps_a_saved_sound(rendered, monkeypatch):
    job, sound_id = rendered
    monkeypatch.setattr(gb.settings, "intro_scene_sound_enabled", False)
    _save(job, text_elements=_text(job))
    assert _sounds(job) == [(sound_id, pytest.approx(LATE))]


def _range_job_with_intro_sound(monkeypatch):
    """The Range fixture's 10 s Talking take (kept [0,4) and [5,10)) with a compiled intro scene
    as its title: the accent enters 0.6 s late and its sound sits there."""
    from app.kria.device_render import make_device_request
    from app.pipeline import intro_scene as scene_mod
    from app.pipeline.canvas import PORTRAIT
    from app.services.device_render import pin_device_request
    from tests.routes import test_phone_talking_range_commit as rng

    job = rng.talking_job(monkeypatch)
    scene = scene_mod.IntroScene.model_validate(
        {
            "decision": "compose",
            "voices": ["solid"] * 4 + ["accent"] * 2,
            "font_pair": "serif_italic",
            "ink": "cream",
            "zone": "above_head",
            "entrance": "pop",
            "sound": "accent-pop",
            "sound_cue": "accent",
            "stagger": "accent_after",
        }
    )
    out = scene_mod.compile_intro_scene(
        scene, EN, canvas=PORTRAIT, timeline_duration_s=8.0, duration_s=3.0, language="en"
    )
    assert out.rows is not None, out.reason
    variant = job.assembly_plan["variants"][0]
    variant["text_elements"] = out.rows
    variant["text_elements_user_edited"] = True
    [cue] = intro_sound_cues(out.rows)
    recipe = device_status(job, "subtitled").request.recipe
    sfx = next(t for t in recipe.tracks if t.id == "sfx")
    clip = sfx.clips[0].model_copy(update={"id": f"sfx-{cue.id}", "timeline_start": cue.at_s})
    tracks = [t.model_copy(update={"clips": [clip]}) if t.id == "sfx" else t for t in recipe.tracks]
    pin_device_request(
        job,
        make_device_request(
            job_id=job.id,
            variant_id="subtitled",
            revision=2,
            recipe=recipe.model_copy(update={"tracks": tracks}),
        ),
        base_generation="first",
    )
    return job, rng, cue.id


def test_a_range_trim_keeps_the_sound_on_the_retimed_accent(monkeypatch):
    job, rng, sound_id = _range_job_with_intro_sound(monkeypatch)
    assert _sounds(job) == [(sound_id, pytest.approx(LATE))]
    # The take's first second goes, so source 0.6 s (where the accent entered) no longer plays:
    # moved through source time the sound would be lost with that footage.
    rng.save(job, [rng._row(rng._SLOT0, 1.0, 3.0), rng._row(rng._SLOT1, 5.0, 5.0)])
    title = _title_rows(job)
    accent = next(r for r in title if r["source_params"]["title_look"]["style"] == "intro_accent")
    start = min(float(r["start_s"]) for r in title)
    assert float(accent["start_s"]) == pytest.approx(start + LATE)
    assert _sounds(job) == [(sound_id, pytest.approx(start + LATE))]
