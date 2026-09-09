"""Unit tests for `_run_slide_post_job` in isolation.

Storage and FFmpeg are mocked (no GCS, no subprocess) — the real FFmpeg
pipeline is covered by tests/pipeline/test_slide_post_build.py. This test
pins the ORCHESTRATION contract: which PlanItemAsset rows get included/
dropped, what the persisted variant dict looks like, and that `video_path`
lands in the exact same write as `slide_post.bundle_gcs_path`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.tasks.generative_build as gb
from app import storage as storage_module
from app.models import PlanItem
from app.pipeline.slide_post import build as slide_build_module
from app.schemas.slide_post import SlidePostDraft, SlideRef


def _asset(*, asset_id, gcs_path, kind, plan_item_id, user_id, media_status="ready", **extra):
    defaults = {
        "id": asset_id,
        "gcs_path": gcs_path,
        "kind": kind,
        "plan_item_id": plan_item_id,
        "user_id": user_id,
        "media_status": media_status,
        "content_fingerprint": f"fp-{asset_id}",
        "gcs_generation": None,
        "duration_s": None,
    }
    defaults.update(extra)
    return SimpleNamespace(**defaults)


class _FakeSession:
    def __init__(self, job, item, assets):
        self._job = job
        self._item = item
        self._assets = assets

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, model, _pk, **_kwargs):
        if model is gb.Job:
            return self._job
        if model is PlanItem:
            return self._item
        return None

    def execute(self, _stmt):
        assets = self._assets

        class _Result:
            def scalars(self_inner):
                return self_inner

            def all(self_inner):
                return assets

        return _Result()

    def commit(self):
        pass


def _patch_storage_and_ffmpeg(
    monkeypatch, *, existing_normalized: set[str] | None = None
) -> list[str]:
    """No-op storage + ffmpeg stand-ins. Returns the list of uploaded keys."""
    uploaded: list[str] = []
    existing = existing_normalized or set()

    def _object_exists(key):
        return key in existing

    def _download_to_file(_object_path, local_path):
        with open(local_path, "wb") as f:
            f.write(b"fake-bytes")

    def _download_generation_to_file(_object_path, local_path, *, generation):  # noqa: ARG001
        _download_to_file(_object_path, local_path)

    def _upload_local_file(_local_path, object_path, _content_type):
        uploaded.append(object_path)

    monkeypatch.setattr(storage_module, "object_exists", _object_exists)
    monkeypatch.setattr(storage_module, "download_to_file", _download_to_file)
    monkeypatch.setattr(storage_module, "download_generation_to_file", _download_generation_to_file)
    monkeypatch.setattr(storage_module, "upload_local_file", _upload_local_file)

    monkeypatch.setattr(slide_build_module, "normalize_image_slide", lambda *a, **k: None)
    monkeypatch.setattr(slide_build_module, "normalize_video_slide", lambda *a, **k: None)
    monkeypatch.setattr(slide_build_module, "probe_duration_s", lambda _path: 3.0)
    monkeypatch.setattr(slide_build_module, "probe_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(slide_build_module, "render_preview_segment", lambda *a, **k: None)
    monkeypatch.setattr(slide_build_module, "concat_preview_segments", lambda *a, **k: None)
    monkeypatch.setattr(slide_build_module, "extract_cover", lambda *a, **k: None)
    monkeypatch.setattr(
        slide_build_module,
        "build_bundle_zip",
        lambda out_zip_path, **k: open(out_zip_path, "wb").close(),
    )
    return uploaded


def test_run_slide_post_job_persists_ordered_slides_and_bundle(monkeypatch) -> None:
    job_id = str(uuid.uuid4())
    item_id = uuid.uuid4()
    user_id = uuid.uuid4()
    good_image_id = uuid.uuid4()
    good_video_id = uuid.uuid4()

    job = SimpleNamespace(id=uuid.UUID(job_id), content_plan_item_id=item_id, user_id=user_id)
    draft = SlidePostDraft(
        platform_profile="instagram_carousel",
        slides=[
            SlideRef(id="s0", asset_id=good_image_id, kind="image", alt="a photo"),
            SlideRef(id="s1", asset_id=good_video_id, kind="video"),
        ],
        cover_index=0,
        caption="hello world",
    )
    item = SimpleNamespace(slide_post=draft.model_dump(mode="json"))
    assets = [
        _asset(
            asset_id=good_image_id,
            gcs_path=f"users/{user_id}/plan/{item_id}/pool/a.jpg",
            kind="image",
            plan_item_id=item_id,
            user_id=user_id,
        ),
        _asset(
            asset_id=good_video_id,
            gcs_path=f"users/{user_id}/plan/{item_id}/pool/b.mp4",
            kind="video",
            plan_item_id=item_id,
            user_id=user_id,
            duration_s=6.0,
        ),
    ]

    monkeypatch.setattr(gb, "_sync_session", lambda: _FakeSession(job, item, assets))
    uploaded = _patch_storage_and_ffmpeg(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(
        gb,
        "_upsert_variant_entry",
        lambda _job_id, result: captured.setdefault("result", result) or True,
    )
    monkeypatch.setattr(gb, "_finalize_job", lambda _job_id, _results: True)

    gb._run_slide_post_job(job_id, render_trace_id="trace-1")

    result = captured["result"]
    assert result["variant_id"] == "slides"
    assert result["resolved_archetype"] == "slides"
    assert result["render_status"] == "ready"
    assert [s["kind"] for s in result["slides"]] == ["image", "video"]
    assert [s["index"] for s in result["slides"]] == [0, 1]
    assert result["slide_post"]["platform_profile"] == "instagram_carousel"
    assert result["slide_post"]["caption"] == "hello world"
    assert result["slide_post"]["cover_index"] == 0
    # video_path (preview) and slide_post.bundle_gcs_path land in the SAME
    # upsert call — the reaper's ready-implies-complete invariant (risk #7).
    assert result["video_path"]
    assert result["slide_post"]["bundle_gcs_path"]
    assert result["poster_path"]
    assert any(key.endswith("bundle.zip") for key in uploaded)
    assert any(key.endswith("preview.mp4") for key in uploaded)
    assert any(key.endswith("cover.jpg") for key in uploaded)


def test_run_slide_post_job_drops_foreign_owned_asset_but_keeps_the_rest(monkeypatch) -> None:
    """A reference to another user's asset (or a deleted/foreign one) is
    dropped, not fatal — same best-effort posture as a missing clip
    elsewhere in the pipeline."""
    job_id = str(uuid.uuid4())
    item_id = uuid.uuid4()
    user_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    good_id = uuid.uuid4()
    foreign_id = uuid.uuid4()

    job = SimpleNamespace(id=uuid.UUID(job_id), content_plan_item_id=item_id, user_id=user_id)
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[
            SlideRef(id="s0", asset_id=good_id, kind="image"),
            SlideRef(id="s1", asset_id=foreign_id, kind="image"),
        ],
        cover_index=0,
    )
    item = SimpleNamespace(slide_post=draft.model_dump(mode="json"))
    assets = [
        _asset(
            asset_id=good_id,
            gcs_path=f"users/{user_id}/plan/{item_id}/pool/a.jpg",
            kind="image",
            plan_item_id=item_id,
            user_id=user_id,
        ),
        # Belongs to a different user — must never be resolved into this job.
        _asset(
            asset_id=foreign_id,
            gcs_path=f"users/{other_user_id}/plan/{item_id}/pool/b.jpg",
            kind="image",
            plan_item_id=item_id,
            user_id=other_user_id,
        ),
    ]

    monkeypatch.setattr(gb, "_sync_session", lambda: _FakeSession(job, item, assets))
    _patch_storage_and_ffmpeg(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(
        gb,
        "_upsert_variant_entry",
        lambda _job_id, result: captured.setdefault("result", result) or True,
    )
    monkeypatch.setattr(gb, "_finalize_job", lambda _job_id, _results: True)

    gb._run_slide_post_job(job_id, render_trace_id="trace-2")

    result = captured["result"]
    assert len(result["slides"]) == 1
    assert result["slides"][0]["asset_id"] == str(good_id)


def test_run_slide_post_job_raises_when_no_usable_slides_remain(monkeypatch) -> None:
    job_id = str(uuid.uuid4())
    item_id = uuid.uuid4()
    user_id = uuid.uuid4()
    only_id = uuid.uuid4()

    job = SimpleNamespace(id=uuid.UUID(job_id), content_plan_item_id=item_id, user_id=user_id)
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="s0", asset_id=only_id, kind="image")],
        cover_index=0,
    )
    item = SimpleNamespace(slide_post=draft.model_dump(mode="json"))
    # media_status is "pending", not "ready" — the only slide is unusable.
    assets = [
        _asset(
            asset_id=only_id,
            gcs_path=f"users/{user_id}/plan/{item_id}/pool/a.jpg",
            kind="image",
            plan_item_id=item_id,
            user_id=user_id,
            media_status="pending",
        ),
    ]
    monkeypatch.setattr(gb, "_sync_session", lambda: _FakeSession(job, item, assets))

    with pytest.raises(gb.SlidePostPolicyError) as exc_info:
        gb._run_slide_post_job(job_id, render_trace_id="trace-3")
    assert exc_info.value.reason == "no_usable_slides"


def test_run_slide_post_job_raises_on_empty_draft(monkeypatch) -> None:
    job_id = str(uuid.uuid4())
    item_id = uuid.uuid4()
    job = SimpleNamespace(id=uuid.UUID(job_id), content_plan_item_id=item_id, user_id=uuid.uuid4())
    item = SimpleNamespace(slide_post=None)
    monkeypatch.setattr(gb, "_sync_session", lambda: _FakeSession(job, item, []))

    with pytest.raises(gb.SlidePostPolicyError) as exc_info:
        gb._run_slide_post_job(job_id, render_trace_id="trace-4")
    assert exc_info.value.reason == "empty_draft"


def test_run_slide_post_job_reuses_already_normalized_slide(monkeypatch) -> None:
    """A pure reorder/caption edit must not re-encode an unchanged slide —
    only the normalize step is skipped; the slide is still downloaded to
    build this render's preview segment."""
    job_id = str(uuid.uuid4())
    item_id = uuid.uuid4()
    user_id = uuid.uuid4()
    asset_id = uuid.uuid4()

    job = SimpleNamespace(id=uuid.UUID(job_id), content_plan_item_id=item_id, user_id=user_id)
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="s0", asset_id=asset_id, kind="image")],
        cover_index=0,
    )
    item = SimpleNamespace(slide_post=draft.model_dump(mode="json"))
    fingerprint = f"fp-{asset_id}"
    assets = [
        _asset(
            asset_id=asset_id,
            gcs_path=f"users/{user_id}/plan/{item_id}/pool/a.jpg",
            kind="image",
            plan_item_id=item_id,
            user_id=user_id,
            content_fingerprint=fingerprint,
        ),
    ]
    monkeypatch.setattr(gb, "_sync_session", lambda: _FakeSession(job, item, assets))
    existing_key = f"generative-jobs/{job_id}/slides/normalized/{fingerprint}_1080x1920.jpg"
    _patch_storage_and_ffmpeg(monkeypatch, existing_normalized={existing_key})

    normalize_calls: list[str] = []
    monkeypatch.setattr(
        slide_build_module,
        "normalize_image_slide",
        lambda *a, **k: normalize_calls.append("called"),
    )
    monkeypatch.setattr(gb, "_upsert_variant_entry", lambda _job_id, _result: True)
    monkeypatch.setattr(gb, "_finalize_job", lambda _job_id, _results: True)

    gb._run_slide_post_job(job_id, render_trace_id="trace-5")

    assert normalize_calls == [], (
        "normalize must be skipped when the content-addressed key already exists"
    )
