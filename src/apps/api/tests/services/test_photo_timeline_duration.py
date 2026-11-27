from __future__ import annotations

import json
import uuid

import pytest

from app.agents._runtime import ModelClient
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.kria.brief import BriefRequirement, CreativeBrief
from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
from app.services.length_policy import creator_duration_s
from tests.services._guided_timeline_fixtures import arm_guided, guided_job, guided_revision


@pytest.mark.parametrize(
    "message",
    [
        "Make the images show only 0.2 seconds",
        "Make the overlay images 0.2 seconds",
        "Make the photos 0.2 seconds",
    ],
)
def test_photo_seconds_are_not_the_total(message):
    requirement = BriefRequirement(
        id="timing", kind="timing", scope="global", description=message, facts={"duration_s": 0.2}
    )
    assert creator_duration_s(brief=CreativeBrief(requirements=[requirement])) is None


def test_total_seconds_still_own_the_video():
    requirement = BriefRequirement(
        id="timing",
        kind="timing",
        scope="global",
        description="Make the video 20 seconds",
        facts={"duration_s": 20},
    )
    assert creator_duration_s(brief=CreativeBrief(requirements=[requirement])) == 20


@pytest.mark.parametrize("misread", [False, True])
def test_guided_photo_duration_compiles_and_saves_native_segment_windows(monkeypatch, misread):
    from app.config import settings
    from app.services.guided_timeline import build_guided_segments

    job_id = str(uuid.uuid4())
    revision = guided_revision(job_id)
    for i in [1, 3]:
        revision["sources"][i].update(
            kind="image", duration_s=None, gcs_path=f"visuals/asset-{i}.jpg"
        )
    job, variant = guided_job(revision, [], job_id)
    job.all_candidates["clip_paths"] = [revision["sources"][i]["gcs_path"] for i in [0, 2]]
    arm_guided(monkeypatch, revision)
    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True)
    snapshot = build_editor_snapshot(job, variant)
    raw = json.dumps(
        {
            "intent": "edit",
            "ops": [
                {
                    "op": "set_media_duration",
                    "selector": {"scope": "timeline", "media_kind": "image", "quantifier": "all"},
                    "duration_s": 0.2,
                }
            ],
            "confidence": 0.99,
            "reply": "The photos are 0.2 seconds.",
        }
    )
    if misread:
        raw = json.dumps(
            {
                "intent": "edit",
                "ops": [{"op": "set_total_duration", "target_s": 0.2, "strategy": "proportional"}],
                "confidence": 0.99,
                "reply": "Shortened the video.",
            }
        )
    output = EditCopilotAgent(ModelClient()).parse(
        raw,
        EditCopilotInput(
            utterance="Make the images show only 0.2 seconds"
            if misread
            else "Set the duration of the stills in this timeline to 0.2 seconds each",
            variant_snapshot=snapshot,
        ),
    )
    assert output.outcome == "proposed"
    compiled = compile_editor_ops(job, variant, output.ops)
    segments = build_guided_segments(
        revision,
        compiled.payload.timeline_slots,
        transitions_enabled=True,
        max_slots=120,
        max_total_s=120,
    )
    assert [(row["media_id"], row["duration_s"]) for row in segments] == [
        ("m0", 2.0),
        ("m1", 0.2),
        ("m2", 2.0),
        ("m3", 0.2),
    ]


@pytest.mark.parametrize("message", ["Make the overlay images 0.2 seconds"])
def test_explicit_overlay_request_does_not_retime_main_timeline(message):
    snapshot = {
        "guided_revision": {"base_generation": "gen"},
        "allowed_op_families": ["clip"],
        "slots": [{"slot_id": "photo", "clip_index": 0, "media_kind": "image", "duration_s": 2.0}],
    }
    raw = json.dumps(
        {
            "intent": "edit",
            "ops": [{"op": "set_total_duration", "target_s": 0.2}],
            "confidence": 0.99,
            "reply": "Shortened the video.",
        }
    )
    response = EditCopilotAgent(ModelClient()).parse(
        raw, EditCopilotInput(utterance=message, variant_snapshot=snapshot)
    )
    assert not response.ops
    assert response.outcome == "clarification"
    assert "timeline" in response.reply.lower()


def test_receipt_rejects_wrong_photo_even_with_correct_video_duration():
    from app.kria.brief_checks import check_requirement, plan_facts_from_editor_payload

    request = BriefRequirement(
        id="photo",
        kind="timing",
        scope="per_clip",
        description="Make the photos 0.2 seconds",
        facts={"photo_hold_s": 0.2},
    )

    def receipt(hold):
        return check_requirement(
            request,
            plan_facts_from_editor_payload(
                {
                    "timeline_slots": [
                        {"media_kind": "video", "duration_s": 2.0},
                        {"media_kind": "image", "duration_s": hold},
                        {"media_kind": "image", "duration_s": 0.2},
                    ]
                }
            ),
        )

    assert receipt(0.2).status == "met"
    assert receipt(0.5).status != "met"


def test_montage_plan_receipt_uses_group_photo_holds():
    from app.kria.brief_checks import check_requirement, plan_facts_from_unified_montage

    request = BriefRequirement(
        id="photo",
        kind="timing",
        scope="per_clip",
        description="Make the photos 0.2 seconds",
        facts={"photo_hold_s": 0.2},
    )

    def receipt(hold):
        return check_requirement(
            request,
            plan_facts_from_unified_montage(
                {
                    "asset_holds": [
                        {
                            "media_id": "a",
                            "requirement_id": "photos",
                            "hold_s": 0.2,
                            "rendered_s": hold,
                        },
                        {
                            "media_id": "b",
                            "requirement_id": "photos",
                            "hold_s": 0.2,
                            "rendered_s": 0.2,
                        },
                    ]
                }
            ),
        )

    assert receipt(0.2).status == "met"
    assert receipt(0.5).status != "met"


def test_guided_phone_save_and_reopen_keep_photo_duration_and_order(monkeypatch):
    from app.config import settings
    from app.routes import generative_jobs as gj
    from app.services.device_render import device_status
    from app.services.phone_sources import PHONE_SOURCES_FIELD
    from tests.routes.test_phone_editor_media import admit_photo, media_job

    job, variant = media_job(monkeypatch)
    job.all_candidates = {"clip_paths": [job.assembly_plan[PHONE_SOURCES_FIELD][0]["proxy_path"]]}
    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True)
    admit_photo(variant)
    revision = gj._guided_v2_revision(job, variant)
    gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation="first",
            guided_revision_number=revision["revision_number"],
            timeline_slots=[
                gj.TimelineSlotEdit(
                    slot_id=revision["segments"][0]["segment_id"],
                    clip_index=0,
                    in_s=2,
                    duration_s=1,
                ),
                gj.TimelineSlotEdit(clip_index=1, in_s=0, duration_s=1),
            ],
        ),
    )
    variant = job.assembly_plan["variants"][0]
    before = build_editor_snapshot(job, variant)
    assert [(row["media_kind"], row["duration_s"]) for row in before["slots"]] == [
        ("video", 1.0),
        ("image", 1.0),
    ]
    output = EditCopilotAgent(ModelClient()).parse(
        json.dumps(
            {
                "intent": "edit",
                "ops": [{"op": "set_total_duration", "target_s": 0.2}],
                "confidence": 0.99,
                "reply": "Shortened the video.",
            }
        ),
        EditCopilotInput(utterance="Make the photos 0.2 seconds", variant_snapshot=before),
    )
    assert output.outcome == "proposed"
    compiled = compile_editor_ops(
        job, variant, [*output.ops, {"op": "reorder_clip", "from_index": 1, "to_index": 0}]
    )
    gj.prepare_editor_commit(job, "guided_story", compiled.payload)
    recipe = device_status(job, "guided_story").request.recipe
    cuts = recipe.tracks[0].clips
    assert cuts[0].source_asset_id.startswith("visual-")
    assert [(clip.timeline_start, clip.source_duration / clip.rate) for clip in cuts] == [
        (0.0, 0.2),
        (0.2, 1.0),
    ]
    reopened = build_editor_snapshot(job, job.assembly_plan["variants"][0])
    assert [(row["media_kind"], row["duration_s"]) for row in reopened["slots"]] == [
        ("image", 0.2),
        ("video", 1.0),
    ]
