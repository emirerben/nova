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
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes, SubtitledEndingClip
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.pipeline.silence_cut import CutPlan, Removal
from app.routes import generative_jobs as gj
from app.services.device_render import device_status, pin_device_request
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD
from tests.pipeline.test_phone_subtitled_plan import (
    VIDEO_ID,
    VIDEO_PATH,
    _binding,
    _pool_video_visual,
    _resolved_sfx,
)
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


def talking_job(
    monkeypatch,
    *,
    range_on: bool = True,
    ending: bool = False,
    ending_sfx_at: float | None = None,
    first_segment_end: float = 4.0,
):
    """A 10 s Talking take cleaned at `first_segment_end`-5 s: kept [0,first_segment_end) and
    [5,10), a sound effect at output 6.0 s (source 7.0 s), two caption cues. ``ending`` adds a
    muted 6 s ending clip after the speaker; ``ending_sfx_at`` puts a second effect at that
    output time (under the ending when it is past the speaker's end)."""
    _enable_range(monkeypatch, on=range_on)
    monkeypatch.setattr(gj.settings, "phone_sfx_speech_duck_enabled", False)
    binding = _binding(duration_s=10.0)
    cut = CutPlan(
        keep_segments=[(0.0, first_segment_end), (5.0, 10.0)],
        removed=[Removal(start_s=first_segment_end, end_s=5.0, reason="pause")],
        time_saved_s=5.0 - first_segment_end,
    )
    sfx = _resolved_sfx(request=_resolved_sfx().request.model_copy(update={"at_s": 6.0}))
    effects = [sfx]
    if ending_sfx_at is not None:
        later = _resolved_sfx().request.model_copy(update={"at_s": ending_sfx_at, "id": "sfx-late"})
        effects.append(_resolved_sfx(request=later))
    visuals = (_pool_video_visual(),) if ending else ()
    recipe = compile_phone_subtitled_plan(
        (binding,),
        caption_cues=_CUES,
        cut_plan=cut,
        lanes=PhoneSubtitledLanes(
            sound_effects=effects,
            ending_clip=(
                SubtitledEndingClip(media_id=VIDEO_ID, gcs_path=VIDEO_PATH, generation="88")
                if ending
                else None
            ),
        ),
        visuals=visuals,
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        all_candidates={"clip_paths": [binding.proxy_path]},
        assembly_plan={
            PHONE_SOURCES_FIELD: [binding.model_dump(mode="json")],
            PHONE_VISUALS_FIELD: [v.model_dump(mode="json") for v in visuals],
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
                    "silence_cut": {"removed": [{"start_s": first_segment_end, "end_s": 5.0}]},
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
    assert error.value.detail == {"code": "unsupported_variant"}


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


# --- an ending clip sits on the same track as the speaker ---------------------------


def test_an_ending_clip_does_not_close_range_and_survives_a_window_change(monkeypatch):
    job = talking_job(monkeypatch, ending=True)
    assert _leaf(job) == {"editable": True, "reason": None}

    save(job, [_row(_SLOT0, 1.0, 3.0), _row(_SLOT1, 5.0, 5.0)])

    recipe = device_status(job, "subtitled").request.recipe
    track = next(t for t in recipe.tracks if t.id == "subtitled")
    ending = [c for c in track.clips if c.id == "clip-ending"]
    assert len(ending) == 1
    assert ending[0].timeline_start == pytest.approx(8.0)
    assert _speaker_windows_only(recipe) == [pytest.approx((1.0, 4.0)), pytest.approx((5.0, 10.0))]
    assert recipe.duration == pytest.approx(8.0 + 6.0)


def test_an_effect_under_the_ending_clip_moves_with_the_cut(monkeypatch):
    job = talking_job(monkeypatch, ending=True, ending_sfx_at=9.5)

    save(job, [_row(_SLOT0, 1.0, 3.0), _row(_SLOT1, 5.0, 5.0)])

    recipe = device_status(job, "subtitled").request.recipe
    late = next(c for t in recipe.tracks if t.id == "sfx" for c in t.clips if "late" in c.id)
    assert late.timeline_start == pytest.approx(8.5)


def _speaker_windows_only(recipe) -> list[tuple[float, float]]:
    track = next(t for t in recipe.tracks if t.id == "subtitled")
    speaker = [c for c in track.clips if c.id != "clip-ending"]
    return [(c.source_start, c.source_start + c.source_duration) for c in speaker]


# --- a Talking edit that became authored (a clip delete) keeps the open timeline ----


def test_an_authored_talking_edit_gets_no_closed_range_leaf(monkeypatch):
    from tests.routes._editor_save_matrix import TALKING_VARIANT, talking_segment_save

    outcome = talking_segment_save(monkeypatch, "none")
    assert outcome.error is None, outcome.error
    monkeypatch.setattr(gj.settings, "phone_talking_range_edits_enabled", True, raising=False)
    variant = next(
        v for v in outcome.job.assembly_plan["variants"] if v["variant_id"] == TALKING_VARIANT
    )
    assert variant["editor_timeline_mode"] == "authored"

    capabilities = gj._editor_capabilities(outcome.job, variant)

    assert capabilities["timeline"] is True
    assert "source_window" not in capabilities.get("clips", {})


# --- the app's minimum window applies to rows the Save changed ----------------------


@pytest.mark.parametrize("duration", [0.002, 0.05, 0.0999])
def test_a_changed_window_shorter_than_the_editor_minimum_is_refused(monkeypatch, duration):
    job = talking_job(monkeypatch)
    job.assembly_plan["variants"][0]["captions_enabled"] = False
    with pytest.raises(HTTPException) as error:
        save(job, [_row(_SLOT0, 1.0, duration), _row(_SLOT1, 5.0, 5.0)])
    assert error.value.detail["code"] == "TIMELINE_INVALID_WINDOW"


def test_the_phones_own_minimum_window_survives_float_noise(monkeypatch):
    job = talking_job(monkeypatch)
    job.assembly_plan["variants"][0]["captions_enabled"] = False
    in_s = 0.7
    noisy = (in_s + 0.1) - in_s
    assert noisy < 0.1
    save(job, [_row(_SLOT0, in_s, noisy), _row(_SLOT1, 5.0, 5.0)])
    assert _speaker_windows(job)[0][1] - _speaker_windows(job)[0][0] == pytest.approx(0.1)


def test_an_untouched_short_cleanup_segment_still_saves_beside_a_changed_one(monkeypatch):
    job = talking_job(monkeypatch, first_segment_end=0.05)
    job.assembly_plan["variants"][0]["captions_enabled"] = False
    save(job, [_row(_SLOT0, 0.0, 0.05), _row(_SLOT1, 6.0, 4.0)])
    assert _speaker_windows(job) == [pytest.approx((0.0, 0.05)), pytest.approx((6.0, 10.0))]


# --- things the Range sheet cannot say are refused, not dropped ----------------------


def test_muting_a_talking_segment_is_refused_by_name(monkeypatch):
    job = talking_job(monkeypatch)
    job.assembly_plan["variants"][0]["captions_enabled"] = False
    with pytest.raises(HTTPException) as error:
        save(job, [_row(_SLOT0, 0.0, 4.0, muted=True), _row(_SLOT1, 5.0, 5.0)])
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "talking_range_muted"


def test_swapping_two_segments_is_an_order_refusal_not_an_overlap(monkeypatch):
    job = talking_job(monkeypatch)
    job.assembly_plan["variants"][0]["captions_enabled"] = False
    with pytest.raises(HTTPException) as error:
        save(job, [_row(_SLOT0, 5.0, 5.0), _row(_SLOT1, 0.0, 4.0)])
    assert error.value.detail["code"] == "talking_range_order"


def test_an_edit_with_no_hydrated_segments_is_not_reported_as_several_clips(monkeypatch):
    from app.services.phone_talking_range import talking_range_reason

    job = talking_job(monkeypatch)
    monkeypatch.setattr(gj, "editor_deletion_timeline", lambda *_args, **_kw: [])
    reason = talking_range_reason(job, job.assembly_plan["variants"][0])
    assert reason == "talking_range_cut_mismatch"


# --- the happy path -----------------------------------------------------------------


def test_widening_a_window_with_captions_on_and_the_transcript_covering_it_saves(monkeypatch):
    job = talking_job(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    # The preflight transcript covers the whole 10 s take, so the pause 4.0-4.5 s has words
    # the captions can follow (without it Save says `talking_range_captions_missing`).
    variant["speech_timing_basis"] = {
        "source_windows": [
            {"slot_id": slot, "clip_index": 0, "in_s": 0, "duration_s": 10.0}
            for slot in (_SLOT0, _SLOT1)
        ]
    }

    save(job, [_row(_SLOT0, 0.0, 4.5), _row(_SLOT1, 5.0, 5.0)])

    assert _speaker_windows(job) == [pytest.approx((0.0, 4.5)), pytest.approx((5.0, 10.0))]
    assert device_status(job, "subtitled").request.recipe.duration == pytest.approx(9.5)
    saved = job.assembly_plan["variants"][0]
    assert saved["speech_timing_unresolved"] is False
    assert [c["text"] for c in saved["caption_cues"]] == ["Hello everyone", "welcome back"]
