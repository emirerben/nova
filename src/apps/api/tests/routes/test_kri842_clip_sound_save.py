"""KRI-842: an editor Save on a Voiceover edit that plays one clip's own sound.

The first render pins per-clip gains (the asked clip faint under the voice, every other clip
silent) and a tail (the last clip playing on once the voice ends), and records what it built in
`variant["clip_sound"]`. A Save must keep what it did not touch, and the receipt must follow the
recipe the Save pinned: a re-cut that silently dropped a gain or pulled the tail under the voice,
while the finished video's receipt still said "Done", is the failure this guards (FM12).
"""

from __future__ import annotations

import pytest

import tests.routes.test_phone_voiceover_editor_lanes as lanes
from app.pipeline.phone_narrated_plan import NarratedVoiceTail, compile_phone_narrated_plan
from tests.routes.test_phone_voiceover_cut_edits import (
    _enable,
    _recipe,
    _save,
    _slots,
    _track,
    _variant,
    voiceover_job,
)

RECEIPT = {
    "version": 1,
    "under_voice": [{"requirement_id": "r1", "media_id": "c1", "status": "built", "gain": 0.3}],
    "after_voice": {
        "requirement_id": "r2",
        "media_id": "c2",
        "status": "built",
        "line": "bye bye",
        "line_start_s": 4.2,
        "line_end_s": 5.0,
        "source_start_s": 4.0,
        "source_end_s": 6.0,
        "tail_start_s": 12.0,
        "tail_end_s": 14.0,
    },
    "not_built": [],
}


def clip_sound_recipe():
    return compile_phone_narrated_plan(
        [
            lanes._step("s0", "c0", start_s=0.0, end_s=4.0, source_start_s=1.5),
            lanes._step("s1", "c1", start_s=4.0, end_s=8.0),
            lanes._step("s2", "c2", start_s=8.0, end_s=12.0),
        ],
        lanes._bindings(),
        lanes._narration(duration_s=12.0),
        voiceover_duration_s=12.0,
        mix=0.7,
        caption_cues=lanes._CUES,
        under_voice_media_ids=frozenset({"c1"}),
        voice_tail=NarratedVoiceTail(media_id="c2", source_start_s=4.0, duration_s=2.0),
    )


@pytest.fixture
def job_and_variant(monkeypatch):
    monkeypatch.setattr(lanes, "_narrated_recipe", clip_sound_recipe)
    _enable(monkeypatch)
    return voiceover_job(persisted={"clip_sound": RECEIPT})


def gains(recipe) -> list[tuple[str, float]]:
    return [
        (clip.source_asset_id, round(clip.volume, 6)) for clip in _track(recipe, "narrated").clips
    ]


def statuses(job) -> tuple[str, str]:
    receipt = _variant(job)["clip_sound"]
    return receipt["under_voice"][0]["status"], receipt["after_voice"]["status"]


def test_the_first_render_pins_what_the_receipt_promises(job_and_variant):
    job, vid = job_and_variant

    assert gains(_recipe(job, vid)) == [("c0", 0.0), ("c1", 0.3), ("c2", 0.0), ("c2", 1.0)]
    assert statuses(job) == ("built", "built")


def test_a_caption_save_keeps_the_gains_the_tail_and_the_receipt(job_and_variant):
    job, vid = job_and_variant
    before = _track(_recipe(job, vid), "narrated").clips

    _save(job, vid, caption_cues=[{"text": "Packed", "start_s": 1.0, "end_s": 3.0}])

    recipe = _recipe(job, vid)
    assert _track(recipe, "narrated").clips == before
    assert recipe.audio.original_volume == 1.0
    assert statuses(job) == ("built", "built")


def test_a_cut_save_that_leaves_the_clips_alone_keeps_everything(job_and_variant):
    job, vid = job_and_variant

    _save(job, vid, timeline_slots=_slots(job, vid))

    recipe = _recipe(job, vid)
    assert gains(recipe) == [("c0", 0.0), ("c1", 0.3), ("c2", 0.0), ("c2", 1.0)]
    assert statuses(job) == ("built", "built")
    assert _variant(job)["narrated_timings"][-1]["end_s"] == pytest.approx(14.0)


def test_trimming_the_tail_keeps_it_while_it_still_holds_the_line(job_and_variant):
    job, vid = job_and_variant
    slots = _slots(job, vid)
    slots[-1]["duration_s"] = 1.5  # 4.0 -> 5.5 still holds the line at 4.2-5.0

    _save(job, vid, timeline_slots=slots)

    assert statuses(job) == ("built", "built")
    assert _variant(job)["clip_sound"]["after_voice"]["tail_end_s"] == pytest.approx(13.5)


def test_trimming_the_tail_into_the_line_marks_the_receipt(job_and_variant):
    job, vid = job_and_variant
    slots = _slots(job, vid)
    slots[-1]["duration_s"] = 0.5  # 4.0 -> 4.5: "bye bye" is cut off

    _save(job, vid, timeline_slots=slots)

    after = _variant(job)["clip_sound"]["after_voice"]
    assert (after["status"], after["reason_code"]) == ("not_possible", "edited")
    assert statuses(job)[0] == "built"


def test_deleting_the_tail_marks_the_receipt_and_keeps_the_gains(job_and_variant):
    job, vid = job_and_variant
    slots = _slots(job, vid)
    slots[-1]["removed"] = True

    _save(job, vid, timeline_slots=slots)

    assert gains(_recipe(job, vid)) == [("c0", 0.0), ("c1", 0.3), ("c2", 0.0)]
    assert statuses(job) == ("built", "not_possible")


def test_pulling_the_tail_under_the_voice_is_not_playing_on_after_it(job_and_variant):
    job, vid = job_and_variant
    slots = _slots(job, vid)
    slots[0]["duration_s"] = 2.0  # every later clip, the tail included, starts 2 s earlier

    _save(job, vid, timeline_slots=slots)

    assert [v for _, v in gains(_recipe(job, vid))] == [0.0, 0.3, 0.0, 1.0]
    # The full-volume tail now plays under the voice, so the faint clip is no longer the only one.
    assert statuses(job) == ("not_possible", "not_possible")


def test_deleting_the_asked_clip_marks_its_receipt(job_and_variant):
    job, vid = job_and_variant
    slots = _slots(job, vid)
    slots[1]["removed"] = True

    _save(job, vid, timeline_slots=slots)

    assert [v for _, v in gains(_recipe(job, vid))] == [0.0, 0.0, 1.0]
    # ... and the picture is 4 s shorter, so the tail now starts under the voice.
    assert statuses(job) == ("not_possible", "not_possible")
