from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.agents._runtime import ModelClient, ModelInvocation
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.kria.brief import BriefRequirement, CreativeBrief
from app.services.kria_editor_ops import (
    build_editor_snapshot,
    compile_editor_ops,
    editor_compile_check,
)
from app.services.length_policy import creator_duration_s
from tests.services._guided_timeline_fixtures import arm_guided, guided_job, guided_revision


@pytest.mark.parametrize(
    "message",
    [
        "Make the images show only 0.2 seconds",
        "Make the overlay images 0.2 seconds",
        "Make the photos 0.2 seconds",
        "Set each photo to 0.2 seconds",
        "Show each photo for 0.2 seconds",
        "Her fotoğraf 0,2 saniye olsun",
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


@pytest.fixture
def mixed_guided_photos(monkeypatch):
    from app.config import settings

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
    return job, variant, revision


async def _photo_chat_turn(monkeypatch, mixed, message, model_ops, *, snapshot=None):
    from app.routes import _copilot

    job, variant, _ = mixed
    raw = json.dumps(
        {"intent": "edit", "ops": model_ops, "confidence": 0.99, "reply": "Prepared the change."}
    )
    provider = SimpleNamespace(invoke=lambda **_: ModelInvocation(raw_text=raw))
    monkeypatch.setattr(_copilot, "default_client", lambda: provider)
    return await _copilot.run_copilot_turn(
        _copilot.CopilotTurnBody(
            message=message,
            snapshot=snapshot if snapshot is not None else build_editor_snapshot(job, variant),
            client_contract_version=2,
        ),
        job_id=job.id,
        compile_check=editor_compile_check(job, variant),
    )


def _photo_chat_segments(mixed, ops):
    from app.services.guided_timeline import build_guided_segments

    job, variant, revision = mixed
    compiled = compile_editor_ops(job, variant, ops)
    return [
        (row["media_id"], row["duration_s"])
        for row in build_guided_segments(
            revision,
            compiled.payload.timeline_slots,
            transitions_enabled=True,
            max_slots=120,
            max_total_s=120,
        )
    ]


_WRONG_TOTAL = {"op": "set_total_duration", "target_s": 0.2, "strategy": "proportional"}
_PHOTO_HOLD = {
    "op": "set_media_duration",
    "selector": {"scope": "timeline", "media_kind": "image", "quantifier": "all"},
    "duration_s": 0.2,
}


@pytest.mark.parametrize(
    ("message", "misread"),
    [
        ("Make the images show only 0.2 seconds", True),
        ("Set the duration of the stills in this timeline to 0.2 seconds each", False),
        ("Set each photo to 0.2 seconds", True),
        ("Show each photo for 0.2 seconds", True),
        ("Her fotoğraf 0,2 saniye olsun", True),
        ("Set every picture to 0.2 seconds", True),
        ("Her görseli 0,2 saniye yap", True),
    ],
)
async def test_guided_photo_duration_compiles_and_saves_native_segment_windows(
    monkeypatch, mixed_guided_photos, message, misread
):
    response = await _photo_chat_turn(
        monkeypatch, mixed_guided_photos, message, [_WRONG_TOTAL if misread else _PHOTO_HOLD]
    )
    assert response.outcome == "proposed"
    assert _photo_chat_segments(mixed_guided_photos, response.ops) == [
        ("m0", 2.0),
        ("m1", 0.2),
        ("m2", 2.0),
        ("m3", 0.2),
    ]


@pytest.mark.parametrize(
    "message",
    ["Set this photo to 0.2 seconds", "Set the photo to 0.2 seconds"],
)
async def test_selected_photo_stays_selected(monkeypatch, mixed_guided_photos, message):
    response = await _photo_chat_turn(
        monkeypatch,
        mixed_guided_photos,
        message,
        [{"op": "set_clip_duration", "slot_index": 1, "duration_s": 0.2}],
    )
    assert response.outcome == "proposed"
    assert _photo_chat_segments(mixed_guided_photos, response.ops) == [
        ("m0", 2.0),
        ("m1", 0.2),
        ("m2", 2.0),
        ("m3", 2.0),
    ]


async def test_mixed_photo_and_video_edit_preserves_both_ops(monkeypatch, mixed_guided_photos):
    response = await _photo_chat_turn(
        monkeypatch,
        mixed_guided_photos,
        "Set each photo to 0.2 seconds and set the first video to 1 second",
        [_PHOTO_HOLD, {"op": "set_clip_duration", "slot_index": 0, "duration_s": 1.0}],
    )
    assert response.outcome == "proposed"
    assert _photo_chat_segments(mixed_guided_photos, response.ops) == [
        ("m0", 1.0),
        ("m1", 0.2),
        ("m2", 2.0),
        ("m3", 0.2),
    ]


async def test_whole_video_length_request_stays_a_whole_video_edit(
    monkeypatch, mixed_guided_photos
):
    response = await _photo_chat_turn(
        monkeypatch,
        mixed_guided_photos,
        "Make the whole video 6 seconds",
        [{"op": "set_total_duration", "target_s": 6.0, "strategy": "proportional"}],
    )
    assert response.outcome == "proposed"
    assert sum(
        duration for _, duration in _photo_chat_segments(mixed_guided_photos, response.ops)
    ) == pytest.approx(6.0)


@pytest.mark.parametrize(
    "message", ["Make the overlay photos 0.2 seconds", "Set each overlay photo to 0.2 seconds"]
)
async def test_fullscreen_overlay_floor_still_refuses_quick_cut(
    monkeypatch, mixed_guided_photos, message
):
    job, variant, _ = mixed_guided_photos
    snapshot = build_editor_snapshot(job, variant)
    snapshot["overlays"] = {
        "cards": [{"kind": "image", "index": 0, "start_s": 0.0, "display_mode": "fullscreen"}]
    }
    response = await _photo_chat_turn(
        monkeypatch, mixed_guided_photos, message, [_WRONG_TOTAL], snapshot=snapshot
    )
    assert not response.ops
    assert "at least 0.3 seconds" in response.reply


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


@pytest.mark.parametrize(
    ("message", "is_overlay"),
    [
        ("Make the images show only 0.2 seconds", False),
        ("Make the overlay images 0.2 seconds", True),
        ("fotoğraflar 0,2 saniye kalsın", False),
        ("Set each photo to 0.2 seconds", False),
        ("Show each photo for 0.2 seconds", False),
        ("Her fotoğraf 0,2 saniye olsun", False),
        ("overlay görselleri 0,2 saniye yap", True),
    ],
)
def test_legacy_photo_timing_receipt_uses_only_the_requested_lane(message, is_overlay):
    from app.kria.brief_checks import check_requirement, plan_facts_from_editor_payload

    request = BriefRequirement(
        id="legacy-photo",
        kind="timing",
        scope="global",
        description=message,
        facts={"duration_s": 0.2},
    )

    def receipt(timeline_hold, overlay_hold):
        return check_requirement(
            request,
            plan_facts_from_editor_payload(
                {
                    "timeline_slots": [
                        {"media_kind": "video", "duration_s": 2.0},
                        {"media_kind": "image", "duration_s": timeline_hold},
                    ],
                    "media_overlays": [{"kind": "image", "start_s": 0.0, "end_s": overlay_hold}],
                }
            ),
        ).status

    assert receipt(0.5, 0.2) == ("met" if is_overlay else "partial")
    assert receipt(0.2, 0.5) == ("partial" if is_overlay else "met")


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
