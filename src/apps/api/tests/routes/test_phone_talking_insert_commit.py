"""Editor Save on a Talking edit whose take is paused for an inserted clip (parent KRI-1151).

A Save recompiles from the pinned recipe plus the rows as saved (already on the final timeline):
the insert must come back exactly where it was, not dropped, not moved to the end as a speaker
and not shifted twice.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.kria.device_render import make_device_request
from app.pipeline.phone_subtitled_plan import PhoneCutaway, PhoneInsert, compile_phone_subtitled_plan
from app.routes import generative_jobs as gj
from app.services.device_render import device_status, pin_device_request
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.services.phone_talking_range import REASON_MULTI_CLIP, talking_range_reason
from tests.pipeline.test_phone_subtitled_plan import _binding
from tests.routes.test_phone_subtitled_editor_commit import _enable_subtitled_editor, save

# The take is 30 s; the 8 s talk plays from 12 s to 20 s; captions are on the FINAL timeline.
CUES = [
    {"text": "before the insert", "start_s": 1.0, "end_s": 3.0},
    {"text": "after the insert", "start_s": 21.0, "end_s": 24.0},
]


def insert_job(monkeypatch):
    _enable_subtitled_editor(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_sfx_speech_duck_enabled", False)
    take = _binding("take", duration_s=30.0)
    talk = _binding("talk", duration_s=8.0)
    broll = _binding("broll", duration_s=4.0)
    recipe = compile_phone_subtitled_plan(
        (take,),
        caption_cues=CUES,
        inserts=(PhoneInsert(binding=talk, at_take_s=12.0),),
        cutaways=(PhoneCutaway(binding=broll, start_s=24.0, end_s=27.0),),
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        assembly_plan={
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in (take, talk, broll)],
            "variants": [
                {
                    "variant_id": "subtitled",
                    "resolved_archetype": "subtitled",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": recipe.duration,
                    "caption_cues": CUES,
                    "voiceover_caption_style": "sentence",
                    "phone_talking_multi_clip": {"version": 1},
                    "phone_insert_clips": {"placed": [{"media_id": "talk"}], "dropped": []},
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


def _main(recipe):
    track = next(t for t in recipe.tracks if t.id == "subtitled")
    return [(c.id, c.source_asset_id, c.timeline_start, c.source_start, c.source_duration, c.volume) for c in track.clips]


def test_a_caption_save_keeps_the_insert_where_it_was_and_the_captions_unshifted(monkeypatch):
    job, old = insert_job(monkeypatch)
    edited = [
        {"text": "before, edited", "start_s": 1.0, "end_s": 3.0},
        {"text": "after, edited", "start_s": 21.0, "end_s": 24.0},
    ]
    save(job, caption_cues=edited)

    new = device_status(job, "subtitled").request.recipe
    assert _main(new) == _main(old)
    assert new.duration == pytest.approx(38.0)
    assert job.assembly_plan["variants"][0]["duration_s"] == pytest.approx(38.0)
    # The cutaway (final-timeline seconds) and the rows are carried as saved: no second shift.
    cutaway = next(t for t in new.tracks if t.id == "talking-head-cutaways")
    assert [(c.timeline_start, c.source_duration) for c in cutaway.clips] == [(24.0, 3.0)]
    starts = sorted(layer.start for layer in new.text_layers)
    assert starts[-1] == pytest.approx(21.0, abs=0.01)


def test_a_second_save_is_stable(monkeypatch):
    job, old = insert_job(monkeypatch)
    save(job, caption_cues=CUES)
    first = _main(device_status(job, "subtitled").request.recipe)
    variant = job.assembly_plan["variants"][0]
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(variant), caption_cues=CUES
        ),
        user_id="owner",
        plan_item_id="item",
    )
    assert _main(device_status(job, "subtitled").request.recipe) == first == _main(old)


def test_range_is_closed_by_name_once_the_take_has_an_insert(monkeypatch):
    job, _old = insert_job(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    assert talking_range_reason(job, variant) == REASON_MULTI_CLIP
    del variant["phone_talking_multi_clip"]
    assert talking_range_reason(job, variant) == REASON_MULTI_CLIP
