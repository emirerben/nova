"""A clip placed from the phone has no look, and that must not make the editor unopenable.

A saved edit is read back as the draft's own rows, so a ``null`` look stored by Save reached
``TimelineResponse`` and the timeline route answered 500 for good.
"""

from __future__ import annotations

from app.routes import generative_jobs as gj
from tests.routes._editor_capability_job import capability_job, section, slot
from tests.routes.test_editor_capability_save_matrix import VARIANT, Doc


def test_a_new_placement_without_a_look_is_saved_as_no_look(monkeypatch):
    job = capability_job(monkeypatch)
    doc = Doc([slot(0, panel=0), slot(2, panel=1)], [section()])
    variant = job.assembly_plan["variants"][0]
    body = gj.EditorCommitRequest.model_validate(doc.wire(gj.variant_render_baseline(variant)))

    gj.prepare_editor_commit(job, VARIANT, body)

    stored = job.assembly_plan["variants"][0]["user_timeline"]["slots"]
    assert [row["look_preset"] for row in stored] == ["none", "none"]


def test_a_saved_row_with_a_null_look_still_opens_in_the_editor():
    row = {"slot_id": "a", "clip_index": 4, "in_s": 0.0, "duration_s": 10.6, "look_preset": None}
    response = gj.TimelineResponse(
        editable=True,
        beat_grid=[],
        total_duration_s=10.6,
        has_user_edits=True,
        slots=[row],
        clips=[],
    )
    assert response.slots[0].look_preset == "none"
