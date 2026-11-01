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


def test_widening_into_a_removed_pause_keeps_both_segments_hard_cut(monkeypatch):
    job = talking_job(monkeypatch)
    job.assembly_plan["variants"][0]["captions_enabled"] = False
    save(job, [_row(_SLOT0, 0.0, 4.5), _row(_SLOT1, 5.0, 5.0)])

    assert _speaker_windows(job) == [pytest.approx((0.0, 4.5)), pytest.approx((5.0, 10.0))]
    assert device_status(job, "subtitled").request.recipe.duration == pytest.approx(9.5)


def test_widening_with_captions_on_and_no_transcript_says_so_instead_of_leaving_a_gap(
    monkeypatch,
):
    job = talking_job(monkeypatch)
    with pytest.raises(HTTPException) as error:
        save(job, [_row(_SLOT0, 0.0, 4.5), _row(_SLOT1, 5.0, 5.0)])
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "talking_range_captions_missing"


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


# --- capability: advertised exactly when Save compiles it ---------------------------


def _leaf(job):
    variant = job.assembly_plan["variants"][0]
    return gj._editor_capabilities(job, variant).get("clips", {}).get("source_window")


def test_leaf_is_open_and_timeline_stays_closed(monkeypatch):
    job = talking_job(monkeypatch)
    capabilities = gj._editor_capabilities(job, job.assembly_plan["variants"][0])
    assert capabilities["clips"]["source_window"] == {"editable": True, "reason": None}
    assert capabilities["timeline"] is False


def test_leaf_is_absent_while_the_flag_is_off(monkeypatch):
    assert _leaf(talking_job(monkeypatch, range_on=False)) is None


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"explicit_speech_cuts": [{"status": "applied"}]}, "talking_range_word_cuts"),
        ({"engine_noise_reduction": {"status": "applied"}}, "talking_range_processed_audio"),
        ({"phone_talking_multi_clip": {"version": 1}}, "talking_range_multi_clip"),
        # The stored cut no longer matches the pinned recipe (a regenerate re-cut the take).
        (
            {"user_timeline": {"slots": [{"slot_id": _SLOT0, "in_s": 2.0, "duration_s": 2.0}]}},
            "talking_range_cut_mismatch",
        ),
    ],
)
def test_leaf_names_why_range_cannot_be_saved(monkeypatch, change, reason):
    job = talking_job(monkeypatch)
    job.assembly_plan["variants"][0].update(change)
    assert _leaf(job) == {"editable": False, "reason": reason}


# --- the saved window is the truth for the next Save --------------------------------


def test_a_second_range_save_starts_from_the_saved_window_and_a_caption_save_keeps_it(
    monkeypatch,
):
    job = talking_job(monkeypatch)
    save(job, [_row(_SLOT0, 1.0, 3.0), _row(_SLOT1, 5.0, 5.0)])
    # An unrelated Save recompiles from the pinned recipe and must not undo the window.
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation=job.assembly_plan["variants"][0]["render_generation_id"],
            caption_meta=gj.EditorCommitCaptionMeta(style="word"),
        ),
        user_id="owner",
        plan_item_id="item",
    )
    assert _speaker_windows(job) == [pytest.approx((1.0, 4.0)), pytest.approx((5.0, 10.0))]
    assert _leaf(job) == {"editable": True, "reason": None}

    # The next Range Save is judged against the saved rows, not the worker's original cut.
    save_with = job.assembly_plan["variants"][0]["render_generation_id"]
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation=save_with,
            timeline_slots=[_row(_SLOT0, 1.5, 2.5), _row(_SLOT1, 5.0, 4.0)],
        ),
        user_id="owner",
        plan_item_id="item",
    )
    assert _speaker_windows(job) == [pytest.approx((1.5, 4.0)), pytest.approx((5.0, 9.0))]
    assert device_status(job, "subtitled").request.recipe.duration == pytest.approx(6.5)


def test_a_caption_lane_the_app_already_moved_is_not_moved_a_second_time(monkeypatch):
    job = talking_job(monkeypatch)
    moved = [
        {"id": "c0", "text": "Hello everyone", "start_s": 0.0, "end_s": 0.5},
        {"id": "c1", "text": "welcome back", "start_s": 0.5, "end_s": 2.0},
    ]
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation="first",
            timeline_slots=[_row(_SLOT0, 1.0, 3.0), _row(_SLOT1, 5.0, 5.0)],
            caption_cues=moved,
        ),
        user_id="owner",
        plan_item_id="item",
    )
    cues = job.assembly_plan["variants"][0]["caption_cues"]
    assert [(c["start_s"], c["end_s"]) for c in cues] == [(0.0, 0.5), (0.5, 2.0)]
