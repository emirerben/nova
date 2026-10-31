from app.routes import generative_jobs as gj
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    MediaRef,
    StoryBeat,
    canonical_media_digest,
)
from app.services.phone_editor import PHONE_EDITOR_PLAN_FIELD
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD
from tests.routes.test_phone_editor_commit import phone_job
from tests.services.test_phone_visuals import photo_plan


def mixed_job(monkeypatch):
    job = phone_job(monkeypatch, photo=True)
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    plan, _ = photo_plan()
    original = job.assembly_plan[PHONE_SOURCES_FIELD][0]
    visual = job.assembly_plan[PHONE_VISUALS_FIELD][0]
    media = [
        MediaRef(
            media_id=original["media_id"],
            lane="clip",
            kind="video",
            gcs_path=original["proxy_path"],
            generation=original["generation"],
            duration_s=10,
        ),
        MediaRef(
            media_id=visual["media_id"],
            lane="asset",
            kind="image",
            gcs_path=visual["gcs_path"],
            generation=visual["generation"],
        ),
    ]
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        duration_s=5,
        title="Scene and photo",
        media=media,
        story_beats=[
            StoryBeat(
                beat_id="scene", topic="Scene", media_ids=[m.media_id for m in media], duration_s=5
            )
        ],
    )
    job.assembly_plan["guided_edit"] = {
        "proposal_version": 1,
        "media_digest": canonical_media_digest(media),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {
                key: getattr(row, key)
                for key in ("lane", "media_id", "gcs_path", "generation", "kind")
            }
            for row in media
        ],
    }
    variant = job.assembly_plan["variants"][0]
    variant["_guided_editor_clock"] = "rendered-v1"
    variant[PHONE_EDITOR_PLAN_FIELD] = plan.model_dump(mode="json")
    return job, variant


def test_mixed_receipts_preserve_photo_and_original(monkeypatch):
    job, variant = mixed_job(monkeypatch)
    revision = gj._guided_v2_revision(job, variant)
    assert revision is not None
    assert revision["timing_mode"] == "rendered"
    assert [s["kind"] for s in revision["sources"]] == ["video", "image"]
    assert revision["sources"][1]["duration_s"] is None
    assert [s["duration_s"] for s in revision["segments"]] == [3, 2]


def test_missing_photo_receipt_refuses_projection(monkeypatch):
    job, variant = mixed_job(monkeypatch)
    job.assembly_plan[PHONE_VISUALS_FIELD] = []
    assert gj._guided_v2_revision(job, variant) is None
