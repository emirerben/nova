"""A saved empty visual program never exposes the last rendered artifact."""

import copy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.kria.api_schemas import DraftSnapshotOut
from app.routes import generative_jobs as routes
from app.services.editor_empty_drafts import stage_saved_draft_baseline


def empty_job():
    variant = {
        "variant_id": "v",
        "editor_state": "empty",
        "render_status": "draft",
        "render_generation_id": "new-generation",
        "editor_draft": {"draft_id": "draft"},
        "video_path": "old.mp4",
        "output_url": "https://old.test/video",
        "download_url": "https://old.test/download",
        "base_video_path": "base.mp4",
        "base_video_url": "https://old.test/base",
        "poster_path": "old.jpg",
        "text_elements": [{"id": "removed", "text": "Old"}],
    }
    draft = DraftSnapshotOut(
        draft_id="draft",
        item_id="item",
        variant_key="v",
        draft_revision=2,
        snapshot_hash="a" * 64,
        etag='"draft-a"',
        can_undo=True,
        base_generation_id="new-generation",
        created_at=datetime.now(UTC),
        snapshot={
            "kind": "editor",
            "editor_payload": {
                "editor_state": "empty",
                "base_generation": "new-generation",
                "sections": {
                    "timeline_slots": [],
                    "text_elements": [],
                    "caption_cues": [{"text": "Keep"}],
                },
            },
        },
    )
    return SimpleNamespace(
        id="job",
        status="variants_ready",
        all_candidates={},
        assembly_plan={"variants": [variant]},
        _saved_editor_drafts={"v": draft},
    )


def test_empty_status_hides_old_artifact_without_signing_or_mutating(monkeypatch):
    job = empty_job()
    before = copy.deepcopy(job.assembly_plan)
    monkeypatch.setattr(routes, "_lazy_backfill_media_overlay_previews", lambda _: False)
    monkeypatch.setattr(routes, "signed_get_url", lambda *args: pytest.fail("signed old artifact"))
    result = routes._variants_for_response(job)[0]
    assert result["editor_state"] == "empty"
    assert result["text_elements"] == []
    assert result["caption_cues"] == [{"text": "Keep"}]
    for key in ("output_url", "download_url", "base_video_url", "video_path", "poster_path"):
        assert not result.get(key)
    assert result["editor_draft"]["draft_revision"] == 2
    assert job.assembly_plan == before


def test_empty_timeline_preserves_authorized_pool_but_never_old_slots(monkeypatch):
    job = empty_job()
    monkeypatch.setattr(
        routes,
        "_dispatch_rendered_timeline",
        lambda *a, **kw: {
            "slots": [{"slot_id": "old"}],
            "total_duration_s": 12,
            "clips": [{"clip_index": 0}],
            "source_pool": [{"media_id": "owned"}],
            "editable": False,
            "reason": "locked_to_voiceover",
            "beat_grid": [0, 1],
        },
    )
    result = routes.dispatch_get_timeline(job, "v")
    assert result["slots"] == []
    assert result["total_duration_s"] == 0
    assert result["source_pool"] == [{"media_id": "owned"}]
    assert result["base_generation"] == "new-generation"
    assert result["draft"]["snapshot"]["editor_payload"]["sections"]["text_elements"] == []


@pytest.mark.parametrize(
    "version,generation,detail",
    [
        (None, "new-generation", "editor_draft_requires_supported_client"),
        (1, "old-generation", "baseline_conflict"),
    ],
)
def test_draft_baseline_rejects_old_client_or_stale_save(version, generation, detail):
    job = empty_job()
    before = copy.deepcopy(job.assembly_plan)
    body = routes.EditorCommitRequest(
        base_generation=generation, editor_state_version=version, title="x"
    )
    with pytest.raises(HTTPException) as caught:
        stage_saved_draft_baseline(job, "v", body)
    assert caught.value.status_code == 409
    assert caught.value.detail == detail
    assert job.assembly_plan == before


def test_draft_baseline_uses_saved_layers_and_keeps_original_untouched():
    job = empty_job()
    body = routes.EditorCommitRequest(
        base_generation="new-generation", editor_state_version=1, title="x"
    )
    staged = stage_saved_draft_baseline(job, "v", body)
    variant = staged.assembly_plan["variants"][0]
    assert variant["text_elements"] == []
    assert "editor_state" not in variant
    assert job.assembly_plan["variants"][0]["editor_state"] == "empty"


def test_cloud_composite_has_one_exact_deletion_identity():
    job = SimpleNamespace(assembly_plan={}, all_candidates={})
    rows = routes.editor_deletion_timeline(
        job,
        {
            "resolved_archetype": "talking_head",
            "base_video_path": "owned/base.mp4",
            "duration_s": 4,
        },
    )
    assert [row["slot_id"] for row in rows] == ["native-composite-base"]
    assert rows[0]["duration_s"] == 4


def test_phone_speech_cleanup_ids_match_native_kept_segments():
    from app.services.phone_sources import PHONE_SOURCES_FIELD

    job = SimpleNamespace(
        all_candidates={"clip_paths": ["users/u/analysis-proxy-video.mp4"]},
        assembly_plan={
            PHONE_SOURCES_FIELD: [
                {
                    "media_id": "speaker",
                    "proxy_path": "users/u/analysis-proxy-video.mp4",
                    "generation": "1",
                    "original": {
                        "sha256": "a" * 64,
                        "byte_count": 1234,
                        "duration_s": 10,
                        "width": 1080,
                        "height": 1920,
                        "orientation_degrees": 0,
                        "has_audio": True,
                    },
                }
            ]
        },
    )
    variant = {
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "silence_cut": {"removed": [{"start_s": 2, "end_s": 3}, {"start_s": 7, "end_s": 8}]},
    }
    rows = routes.editor_deletion_timeline(job, variant)
    assert [row["slot_id"] for row in rows] == [
        "native-phone-talking-source",
        "native-phone-talking-source-1",
        "native-phone-talking-source-2",
    ]
    assert [(row["in_s"], row["duration_s"]) for row in rows] == [(0, 2), (3, 4), (8, 2)]
    assert routes.editor_deletion_timeline(job, {**variant, "editor_state": "empty"}) == []


def test_authored_render_dispatch_never_uses_caption_reburn(monkeypatch):
    from unittest.mock import Mock

    from app.tasks import generative_build

    full_render, captions = Mock(), Mock()
    monkeypatch.setattr(generative_build.regenerate_generative_variant, "apply_async", full_render)
    monkeypatch.setattr(generative_build.reburn_narrated_captions, "apply_async", captions)
    routes.enqueue_editor_commit_render(
        "job",
        "v",
        {
            "has_render_section": True,
            "authored_timeline": True,
            "generation": "g2",
            "sections": {"caption_cues": True},
        },
    )
    full_render.assert_called_once()
    assert full_render.call_args.kwargs["kwargs"] == {
        "render_gen_id": "g2",
        "force_full_render": True,
    }
    captions.assert_not_called()
