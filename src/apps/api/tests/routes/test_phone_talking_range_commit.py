"""Range (source window) on a phone Talking (`subtitled`) edit.

The Range sheet writes each hydrated talking-source slot's `in_s` / `duration_s`; Save must
recompile the speaker's kept segments from those rows and move every timed lane with them.
Before this feature a Talking Save carrying `timeline_slots` answered `unsupported_variant`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.kria.device_render import make_device_request
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.pipeline.silence_cut import CutPlan, Removal
from app.routes import generative_jobs as gj
from app.services.device_render import device_status, pin_device_request
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD
from tests.pipeline.test_phone_subtitled_plan import _binding, _resolved_sfx
from tests.routes.test_phone_subtitled_editor_commit import _enable_subtitled_editor

_SLOT0 = "native-phone-talking-source"
_SLOT1 = "native-phone-talking-source-1"
_CUES = [
    {"id": "c0", "text": "Hello everyone", "start_s": 0.0, "end_s": 1.5},
    {"id": "c1", "text": "welcome back", "start_s": 1.5, "end_s": 3.0},
]


def _enable_range(monkeypatch, *, on: bool = True) -> None:
    _enable_subtitled_editor(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_talking_range_edits_enabled", on, raising=False)


def talking_job(monkeypatch, *, range_on: bool = True):
    """A 10 s Talking take cleaned at 4-5 s: kept [0,4) and [5,10), a sound effect at output
    6.0 s (source 7.0 s), two caption cues."""
    _enable_range(monkeypatch, on=range_on)
    monkeypatch.setattr(gj.settings, "phone_sfx_speech_duck_enabled", False)
    binding = _binding(duration_s=10.0)
    cut = CutPlan(
        keep_segments=[(0.0, 4.0), (5.0, 10.0)],
        removed=[Removal(start_s=4.0, end_s=5.0, reason="pause")],
        time_saved_s=1.0,
    )
    sfx = _resolved_sfx(request=_resolved_sfx().request.model_copy(update={"at_s": 6.0}))
    recipe = compile_phone_subtitled_plan(
        (binding,),
        caption_cues=_CUES,
        cut_plan=cut,
        lanes=PhoneSubtitledLanes(sound_effects=[sfx]),
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        all_candidates={"clip_paths": [binding.proxy_path]},
        assembly_plan={
            PHONE_SOURCES_FIELD: [binding.model_dump(mode="json")],
            PHONE_VISUALS_FIELD: [],
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
                    "silence_cut": {"removed": [{"start_s": 4.0, "end_s": 5.0}]},
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe),
        base_generation="first",
    )
    return job


def _row(slot_id: str, in_s: float, duration_s: float, **extra) -> gj.TimelineSlotEdit:
    return gj.TimelineSlotEdit(
        slot_id=slot_id, clip_index=0, in_s=in_s, duration_s=duration_s, **extra
    )


def save(job, rows):
    return gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(base_generation="first", timeline_slots=rows),
        user_id="owner",
        plan_item_id="item",
    )


def _speaker_windows(job) -> list[tuple[float, float]]:
    recipe = device_status(job, "subtitled").request.recipe
    track = next(t for t in recipe.tracks if t.id == "subtitled")
    return [(c.source_start, c.source_start + c.source_duration) for c in track.clips]


def test_save_compiles_the_chosen_window_and_moves_the_lanes_with_it(monkeypatch):
    job = talking_job(monkeypatch)
    save(job, [_row(_SLOT0, 1.0, 3.0), _row(_SLOT1, 5.0, 5.0)])

    assert _speaker_windows(job) == [pytest.approx((1.0, 4.0)), pytest.approx((5.0, 10.0))]
    recipe = device_status(job, "subtitled").request.recipe
    variant = job.assembly_plan["variants"][0]
    assert recipe.duration == pytest.approx(8.0)
    assert variant["duration_s"] == pytest.approx(8.0)
    assert variant["render_status"] == "awaiting_device"
    assert variant.get("editor_timeline_mode") is None
    # source 7.0 s used to play at output 6.0 s; the first segment lost its first second.
    sfx = next(t for t in recipe.tracks if t.id == "sfx")
    assert sfx.clips[0].timeline_start == pytest.approx(5.0)
    # "Hello everyone" (source 0-1.5) keeps only 1.0-1.5; "welcome back" (1.5-3.0) follows it.
    cues = [(c["text"], c["start_s"], c["end_s"]) for c in variant["caption_cues"]]
    assert cues[0][1:] == pytest.approx((0.0, 0.5))
    assert cues[1][1:] == pytest.approx((0.5, 2.0))
    assert [r["slot_id"] for r in variant["user_timeline"]["slots"]] == [_SLOT0, _SLOT1]


def test_widening_into_a_removed_pause_is_one_continuous_clip_run(monkeypatch):
    job = talking_job(monkeypatch)
    save(job, [_row(_SLOT0, 0.0, 4.5), _row(_SLOT1, 5.0, 5.0)])

    assert _speaker_windows(job) == [pytest.approx((0.0, 4.5)), pytest.approx((5.0, 10.0))]
    assert device_status(job, "subtitled").request.recipe.duration == pytest.approx(9.5)


@pytest.mark.parametrize(
    ("rows", "code"),
    [
        ([_row(_SLOT0, 0.0, 5.5), _row(_SLOT1, 5.0, 5.0)], "talking_range_overlap"),
        ([_row(_SLOT0, 0.0, 4.0)], "talking_range_structure_changed"),
        ([_row(_SLOT1, 5.0, 5.0), _row(_SLOT0, 0.0, 4.0)], "talking_range_structure_changed"),
        ([_row(_SLOT0, 0.0, 4.0), _row(_SLOT1, 5.0, 5.2)], "TIMELINE_OUT_OF_BOUNDS"),
        (
            [_row(_SLOT0, 0.0, 4.0), _row(_SLOT1, 5.0, 5.0, playback_rate=2.0)],
            "talking_range_structure_changed",
        ),
    ],
)
def test_a_save_that_is_not_a_pure_window_change_is_refused_by_name(monkeypatch, rows, code):
    job = talking_job(monkeypatch)
    with pytest.raises(HTTPException) as error:
        save(job, rows)
    assert error.value.status_code in (409, 422)
    assert error.value.detail["code"] == code


def test_flag_off_keeps_the_old_refusal(monkeypatch):
    job = talking_job(monkeypatch, range_on=False)
    with pytest.raises(HTTPException) as error:
        save(job, [_row(_SLOT0, 1.0, 3.0), _row(_SLOT1, 5.0, 5.0)])
    assert error.value.status_code == 422


def test_range_is_refused_after_the_creator_removed_words(monkeypatch):
    job = talking_job(monkeypatch)
    job.assembly_plan["variants"][0]["explicit_speech_cuts"] = [{"status": "applied"}]
    with pytest.raises(HTTPException) as error:
        save(job, [_row(_SLOT0, 1.0, 3.0), _row(_SLOT1, 5.0, 5.0)])
    assert error.value.detail["code"] == "talking_range_word_cuts"
