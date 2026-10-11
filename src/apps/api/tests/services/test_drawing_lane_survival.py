"""KRI-589: freehand drawings must survive every surface that rebuilds a variant's lanes.

A lane that one of these surfaces forgets is silently lost the next time the creator touches
anything else, so the table lists each surface once. A surface that must also TRANSFORM the lane
(retime on a speech cut, clamp to a shorter cut) asserts the transform, not only presence.
(The voiceover-cut clamp runs through the real Save in ``test_editor_commit_drawings``.)
"""

from __future__ import annotations

import copy
import uuid

import pytest

from app.agents import edit_copilot
from app.kria.drafts import _editor_snapshot
from app.pipeline.speech_cut_state import Removal, reproject_variant_timing
from app.services.editor_empty_drafts import editor_sections
from app.services.kria_editor_ops import build_editor_snapshot, project_editor_draft
from tests.routes.test_editor_commit import _arm_every_editor_lane, _job
from tests.tasks import test_speech_cut_rerender as speech_cut


def _stroke() -> dict:
    return {"color": "#FF3B30", "width_frac": 0.012, "points": [[0.1, 0.1], [0.4, 0.4]]}


def _drawing(drawing_id: str, start_s: float, end_s: float, draw_duration_s: float = 1.2) -> dict:
    return {
        "id": drawing_id,
        "start_s": start_s,
        "end_s": end_s,
        "canvas_aspect": 0.5625,
        "animation": {"entrance": "draw", "draw_duration_s": draw_duration_s, "exit": "none"},
        "strokes": [_stroke()],
    }


DRAWINGS = [_drawing("kept", 0.5, 2.5), _drawing("late", 5.5, 7.0)]


def _variant_job(monkeypatch):
    _arm_every_editor_lane(monkeypatch)
    job = _job()
    variant = job.assembly_plan["variants"][0]
    variant["drawings"] = copy.deepcopy(DRAWINGS)
    return job, variant


def _draft_snapshot(monkeypatch):
    _job_, variant = _variant_job(monkeypatch)
    sections = _editor_snapshot(variant, "gen")["sections"]
    assert sections["drawings"] == DRAWINGS
    # ...and an empty draft (a zero-clip Save) keeps them through its own section copy.
    assert editor_sections(_job_, variant["variant_id"])["drawings"] == DRAWINGS


def _editor_ops_projection(monkeypatch):
    _job_, variant = _variant_job(monkeypatch)
    # No unsaved state: the projected variant still carries the saved lane...
    assert project_editor_draft(variant, {})["drawings"] == DRAWINGS
    # ...and the creator's unsaved lane wins over it (a turn-2 edit indexes what they see).
    unsaved = [_drawing("unsaved", 1.0, 2.0)]
    assert project_editor_draft(variant, {"drawings": unsaved})["drawings"] == unsaved


def _copilot_snapshot(monkeypatch):
    job, variant = _variant_job(monkeypatch)
    snapshot = build_editor_snapshot(job, variant)
    # Read-only context: that they exist and when, never their strokes.
    assert snapshot["drawings"] == {
        "count": 2,
        "windows": [{"start_s": 0.5, "end_s": 2.5}, {"start_s": 5.5, "end_s": 7.0}],
    }
    prompt = edit_copilot._format_snapshot(snapshot)
    assert "FREEHAND DRAWINGS" in prompt and "never remove" in prompt and "count=2" in prompt
    assert "stroke" not in prompt
    # No drawings: the snapshot (and so the prompt) is exactly what it always was.
    variant["drawings"] = None
    bare = build_editor_snapshot(job, variant)
    assert "drawings" not in bare
    assert "FREEHAND DRAWINGS" not in edit_copilot._format_snapshot(bare)


def _speech_cut_retime(_monkeypatch):
    variant = {
        "drawings": [
            _drawing("before-cut", 1.0, 3.0, draw_duration_s=1.2),
            _drawing("spans-cut", 3.0, 6.0, draw_duration_s=3.0),
            _drawing("inside-cut", 4.2, 4.8),
            _drawing("after-cut", 5.5, 7.0),
        ]
    }
    cut = [Removal(start_s=4.0, end_s=5.0, reason="silence")]

    moved = reproject_variant_timing(variant, old_removals=[], new_removals=cut)["drawings"]

    by_id = {row["id"]: row for row in moved}
    assert "inside-cut" not in by_id  # fully cut: dropped
    assert (by_id["before-cut"]["start_s"], by_id["before-cut"]["end_s"]) == (1.0, 3.0)
    # A window the cut shortened shrinks its draw-on with it (3.0 s of draw-on -> a 2.0 s window).
    assert (by_id["spans-cut"]["start_s"], by_id["spans-cut"]["end_s"]) == (3.0, 5.0)
    assert by_id["spans-cut"]["animation"]["draw_duration_s"] == 2.0
    assert (by_id["after-cut"]["start_s"], by_id["after-cut"]["end_s"]) == (4.5, 6.0)
    assert by_id["after-cut"]["strokes"] == variant["drawings"][3]["strokes"]


def _speech_cut_finalize(monkeypatch):
    """`_merge_speech_cut_prior_state`: the new render's variant never carries the lane."""
    from app.tasks import generative_build as gb

    job = speech_cut._inflight_job(attempt_id="winner")
    prior = job.assembly_plan["speech_cut_previous_variant"]
    prior["silence_cut"] = {"removed": [], "original_duration_s": 10.0}
    prior["drawings"] = [_drawing("before-cut", 1.0, 3.0), _drawing("after-cut", 5.5, 7.0)]
    monkeypatch.setattr(gb, "_sync_session", lambda: speech_cut._Session(job))
    result = {
        "variant_id": "subtitled",
        "silence_cut": {
            "removed": [{"start_s": 4.0, "end_s": 5.0, "reason": "retake_review"}],
            "original_duration_s": 10.0,
        },
    }

    merged = gb._merge_speech_cut_prior_state(
        str(uuid.uuid4()), result, expected_operation_id="operation-a", expected_attempt_id="winner"
    )

    assert [(row["id"], row["start_s"], row["end_s"]) for row in merged["drawings"]] == [
        ("before-cut", 1.0, 3.0),
        ("after-cut", 4.5, 6.0),
    ]


SURFACES = {
    "draft_snapshot": _draft_snapshot,
    "editor_ops_projection": _editor_ops_projection,
    "copilot_snapshot": _copilot_snapshot,
    "speech_cut_retime": _speech_cut_retime,
    "speech_cut_finalize": _speech_cut_finalize,
}


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_drawings_survive_the_surface(monkeypatch, surface) -> None:
    SURFACES[surface](monkeypatch)
