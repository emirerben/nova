"""KRI-1332: what binding a text to the clip it records (``source_params.source_asset_id`` +
``source_timeline_id``) must not change for the other texts that carry the same keys.

Two writers stamp them: the phone repair's event titles (KRI-1331) and the typed-media
participant labels (``PLAYER N``, ``narration_label_kind: participant``). Nothing else does
(captions, media overlays and narration score/topic labels never record a clip).

Ways this could fail, written before the code:
  1. a participant label or a linked title counts as a per-clip label lane, so a guided
     variant with removable uploaded media loses the ``visual_media`` family (the creator
     can no longer ask to remove that media);
  2. ``realign_labels`` treats a participant label as a run-on title: it keeps the label's
     old length from the clip's start, so it runs over the next clips;
  3. "add the hour to each clip" folds the hour into an event title (``Football · 12:05``)
     or a participant label instead of labelling the clip, and the replace mode leaves the
     clip with no hour at all because the bound text counts as the creator's edited label;
  4. a participant label stops following its clip on a duration edit (the editor save path
     already ties PLAYER placeholders to their moment, ``guided_timeline.py``).
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import get_args

import pytest

from app.agents._runtime import ModelClient
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.config import settings
from app.kria.recipes import MediaCapability
from app.services.kria_editor_ops import (
    build_editor_snapshot,
    compile_editor_ops,
    editor_payload_dump,
    project_editor_draft,
)
from tests.services._guided_timeline_fixtures import arm_guided, guided_job, guided_revision
from tests.services.test_kri1332_titles_follow_clips import (
    HALF_SECOND_CLIPS,
    SIXTY_SECONDS,
    TIMELINE,
    _chat,
    _clip_windows,
    _incident_plan,
    _parse,
    _phone_variant,
)

FRAME = 1 / 30


def _participant(media_id: str, timeline_id: str, start_s: float, end_s: float) -> dict:
    return {
        "id": f"participant-{media_id}",
        "text": "PLAYER 1",
        "start_s": start_s,
        "end_s": end_s,
        "role": "generative_sequence",
        "position": "custom",
        "x_frac": 0.08,
        "y_frac": 0.76,
        "alignment": "left",
        "effect": "static",
        "source_params": {
            "narration_label_kind": "participant",
            "source_kind": "typed_media_analysis",
            "editable_placeholder": True,
            "participant_key": f"asset:{media_id}",
            "source_asset_id": media_id,
            "source_timeline_id": timeline_id,
        },
    }


def _event_title(media_id: str, timeline_id: str, start_s: float, end_s: float) -> dict:
    return {
        "id": "creation-event-1",
        "text": "Football",
        "start_s": start_s,
        "end_s": end_s,
        "role": "generative_intro",
        "source_params": {"source_asset_id": media_id, "source_timeline_id": timeline_id},
    }


_GLOBAL_TITLE = {
    "id": "guided-title",
    "text": "Weekend",
    "start_s": 0.0,
    "end_s": 8.0,
    "role": "generative_intro",
}


def _media_block() -> dict:
    return {
        "id": "media-1",
        "kind": "media",
        "asset_id": "uploaded-asset",
        "src_gcs_path": "users/u/private-image.jpg",
        "media_kind": "image",
        "start_s": 0.0,
        "end_s": 2.0,
        "origin": "user",
    }


@pytest.fixture(autouse=True)
def _verified(monkeypatch):
    monkeypatch.setattr(settings, "phone_font_qualification_strict", False)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(get_args(MediaCapability)))
    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True, raising=False)


def _guided(monkeypatch, bars: list[dict], *, with_media: bool = False):
    job_id = str(uuid.uuid4())
    revision = guided_revision(job_id)
    job, variant = guided_job(revision, bars, job_id)
    arm_guided(
        monkeypatch,
        revision,
        caps={
            "text_elements": True,
            "timeline": True,
            "visual_blocks": True,
            "clips": {"transitions": {"editable": True}},
        },
    )
    if with_media:
        variant["base_video_path"] = "users/u/base.mp4"
        variant["visual_blocks"] = [_media_block()]
        revision["visual_blocks"] = [_media_block()]
    return job, variant


def test_bound_titles_and_participants_keep_the_visual_media_family(monkeypatch):
    bars = [
        copy.deepcopy(_GLOBAL_TITLE),
        _participant("m1", "s2", 2.0, 4.0),
        _event_title("m2", "s3", 4.0, 8.0),
    ]
    job, variant = _guided(monkeypatch, bars, with_media=True)
    snapshot = build_editor_snapshot(job, variant)
    assert "visual_media" in snapshot["allowed_op_families"]
    assert [row["id"] for row in snapshot["visual_media"]] == ["media-1"]


def test_a_per_clip_label_bar_still_withholds_the_visual_media_family(monkeypatch):
    """Control: the legacy label lane keeps its guard, so the fix is not a blanket opening."""
    from tests.services._guided_timeline_fixtures import label_bar

    bars = [copy.deepcopy(_GLOBAL_TITLE), label_bar("m1", "s2", 2.0, 4.0, "Beach")]
    job, variant = _guided(monkeypatch, bars, with_media=True)
    assert "visual_media" not in build_editor_snapshot(job, variant)["allowed_op_families"]


def test_realign_puts_a_participant_label_back_on_exactly_its_clip():
    plan = _incident_plan()
    _job, probe = _phone_variant(plan)
    moment = TIMELINE[7]
    start, end = _clip_windows(probe)[moment["moment_id"]]
    label = _participant(moment["media_id"], moment["moment_id"], start, end)
    job, saved = _phone_variant(plan, label)
    drifted = _chat(
        job,
        saved,
        [(SIXTY_SECONDS, "make it 60 seconds"), (HALF_SECOND_CLIPS, "make them 0.5 seconds")],
        preserve_text=True,
    )
    window = _clip_windows(drifted)[moment["moment_id"]]
    row = next(r for r in drifted["text_elements"] if r["id"] == label["id"])
    assert row["start_s"] > window[1], "the replay must start with the label off its clip"

    parsed, state = _parse(job, drifted, {"op": "realign_labels"}, "match the labels again")
    assert parsed is not None, state.no_effect_clarifications
    assert label["id"] in parsed["target_ids"]
    compiled = compile_editor_ops(job, drifted, [parsed])
    realigned = project_editor_draft(drifted, editor_payload_dump(compiled.payload), job)
    row = next(r for r in realigned["text_elements"] if r["id"] == label["id"])
    assert (row["start_s"], row["end_s"]) == pytest.approx(window, abs=FRAME)


def test_a_participant_label_follows_its_clip_on_a_duration_edit():
    plan = _incident_plan()
    _job, probe = _phone_variant(plan)
    moment = TIMELINE[7]
    start, end = _clip_windows(probe)[moment["moment_id"]]
    label = _participant(moment["media_id"], moment["moment_id"], start, end)
    job, saved = _phone_variant(plan, label)
    edited = _chat(
        job,
        saved,
        [(SIXTY_SECONDS, "make it 60 seconds"), (HALF_SECOND_CLIPS, "make them 0.5 seconds")],
    )
    window = _clip_windows(edited)[moment["moment_id"]]
    row = next(r for r in edited["text_elements"] if r["id"] == label["id"])
    assert (row["start_s"], row["end_s"]) == pytest.approx(window, abs=FRAME)


_FACTS = {
    "m0": [{"kind": "capture_time", "value": "2026-09-20T14:32:00Z", "provenance": "exif"}],
    "m1": [{"kind": "capture_time", "value": "2026-09-20T09:05:00Z", "provenance": "exif"}],
    "m2": [{"kind": "capture_time", "value": "2026-09-20T10:05:00Z", "provenance": "exif"}],
    "m3": [{"kind": "capture_time", "value": "2026-09-20T11:47:00Z", "provenance": "exif"}],
}


@pytest.mark.parametrize("mode", ["append", "replace"])
@pytest.mark.parametrize(
    "bound",
    [
        _event_title("m1", "s2", 2.0, 6.0),
        _participant("m1", "s2", 2.0, 4.0),
    ],
    ids=["event_title", "participant"],
)
def test_the_hour_labels_every_clip_and_leaves_bound_texts_as_written(monkeypatch, mode, bound):
    job, variant = _guided(monkeypatch, [copy.deepcopy(_GLOBAL_TITLE), copy.deepcopy(bound)])
    snapshot = build_editor_snapshot(job, variant, clip_context={"facts": _FACTS})
    raw = json.dumps(
        {
            "intent": "edit",
            "ops": [
                {
                    "op": "label_each_clip",
                    "source": "facts",
                    "label_from": "capture_time",
                    "mode": mode,
                }
            ],
            "confidence": 0.9,
            "reply": "Done.",
            "suggestions": [],
            "needs_clarification": False,
        }
    )
    out = EditCopilotAgent(ModelClient()).parse(
        raw,
        EditCopilotInput(
            utterance="add the hour to each clip", prior_turns=[], variant_snapshot=snapshot
        ),
    )
    assert out.outcome == "proposed"
    compiled = compile_editor_ops(job, variant, out.ops)
    texts = {row["id"]: row["text"] for row in compiled.payload.text_elements}
    assert texts[bound["id"]] == bound["text"]
    hours = {
        media: texts[f"clip-label-media-{media}"]
        for media in ("m0", "m1", "m2", "m3")
        if f"clip-label-media-{media}" in texts
    }
    assert hours == {"m0": "14:32", "m1": "09:05", "m2": "10:05", "m3": "11:47"}
