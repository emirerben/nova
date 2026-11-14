"""KRI-1423: cutting or trimming a photo in the phone editor still saves.

The phone moves `in_s` forward for the right half of a cut and for a left trim, on a photo as on
a video. A photo has no source timeline, so the position means nothing, but it used to fail the
recipe compile (`unsupported_phone_edit`) and the creator could not keep the edit.
"""

import pytest
from fastapi import HTTPException

from app.routes import generative_jobs as gj
from app.services.device_render import device_status
from tests.routes._editor_capability_job import PHOTO, capability_job, section, slot

VARIANT = "original_text"


def save(job, slots, sections=()):
    variant = job.assembly_plan["variants"][0]
    body = gj.EditorCommitRequest.model_validate(
        {
            "base_generation": gj.variant_render_baseline(variant),
            "editor_state_version": 2,
            "timeline_slots": slots,
            "split_screen_sections": list(sections),
        }
    )
    return gj.prepare_editor_commit(job, VARIANT, body)


def photo(**row):
    return slot(PHOTO, media_kind="image", **row)


@pytest.mark.parametrize(
    "pieces, sections",
    [
        pytest.param(
            [
                slot(0, panel=0),
                photo(panel=1, duration_s=1.5),
                photo(panel=1, offset=1.5, in_s=1.5, duration_s=2.5, source_start_locked=True),
            ],
            [section()],
            id="photo-cut-in-panel",
        ),
        pytest.param(
            [slot(0, panel=0), photo(panel=1, in_s=1.0, duration_s=3.0, source_start_locked=True)],
            [section()],
            id="photo-trim-start-in-panel",
        ),
        pytest.param(
            [photo(duration_s=2.0), photo(in_s=2.0, duration_s=2.0, source_start_locked=True)],
            [],
            id="photo-cut-full-screen",
        ),
    ],
)
def test_a_photo_the_creator_cut_or_trimmed_saves_at_position_zero(monkeypatch, pieces, sections):
    job = capability_job(monkeypatch)
    save(job, pieces, sections)
    stored = job.assembly_plan["variants"][0]["user_timeline"]["slots"]
    images = [r for r in stored if r["source_kind"] == "image"]
    assert len(images) == sum(r["clip_index"] == PHOTO for r in pieces)
    assert all(r["in_s"] == 0 for r in images)
    assert device_status(job, VARIANT).request.recipe.duration > 0


@pytest.mark.parametrize("change", [{"playback_rate": 2.0}, {"source_end_s": 3.0}])
def test_a_photo_with_a_clock_is_still_refused(monkeypatch, change):
    job = capability_job(monkeypatch)
    with pytest.raises(HTTPException) as refused:
        save(job, [photo(**change)])
    assert refused.value.status_code == 422
    assert refused.value.detail["code"] in {"phone_edit_unsupported", "unsupported_phone_edit"}
