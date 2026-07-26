from __future__ import annotations

import pytest

from app.config import settings
from app.pipeline.motion_scene import MOTION_RUNTIME_HASH
from app.tasks import generative_build as gb


def _scene() -> dict:
    return {
        "id": "route-1",
        "preset_id": "route_trace",
        "preset_version": 1,
        "start_frame": 0,
        "end_frame_exclusive": 60,
        "palette": {"primary": "#8B5CF6", "accent": "#D9FF43"},
        "intensity": 0.8,
    }


def test_stale_motion_cache_is_rebuilt_with_required_runtime(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(settings, "motion_scenes_enabled", True)
    monkeypatch.setattr(
        "app.pipeline.motion_scene.apply_motion_scenes",
        lambda **kwargs: calls.append(kwargs),
    )

    render_base, cache = gb._ensure_motion_base(
        job_id="job-1",
        variant_id="variant-1",
        variant={
            "motion_scenes": [_scene()],
            "motion_runtime_hash": MOTION_RUNTIME_HASH,
            "motion_base_path": "old-cache.mp4",
            "motion_cache_stale": True,
        },
        base_gcs_path="visual-base.mp4",
    )

    assert render_base == cache
    assert render_base != "old-cache.mp4"
    assert calls[0]["base_gcs_path"] == "visual-base.mp4"


def test_fresh_motion_cache_is_reused(monkeypatch) -> None:
    monkeypatch.setattr(settings, "motion_scenes_enabled", True)
    render_base, cache = gb._ensure_motion_base(
        job_id="job-1",
        variant_id="variant-1",
        variant={
            "motion_scenes": [_scene()],
            "motion_runtime_hash": MOTION_RUNTIME_HASH,
            "motion_base_path": "fresh-cache.mp4",
            "motion_base_source_path": "clean-base.mp4",
            "motion_cache_stale": False,
        },
        base_gcs_path="clean-base.mp4",
    )
    assert (render_base, cache) == ("fresh-cache.mp4", "fresh-cache.mp4")


def test_motion_cache_is_rebuilt_when_clean_base_changes(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(settings, "motion_scenes_enabled", True)
    monkeypatch.setattr(
        "app.pipeline.motion_scene.apply_motion_scenes",
        lambda **kwargs: calls.append(kwargs),
    )

    render_base, cache = gb._ensure_motion_base(
        job_id="job-1",
        variant_id="variant-1",
        variant={
            "motion_scenes": [_scene()],
            "motion_runtime_hash": MOTION_RUNTIME_HASH,
            "motion_base_path": "fresh-for-old-source.mp4",
            "motion_base_source_path": "old-clean-base.mp4",
            "motion_cache_stale": False,
        },
        base_gcs_path="new-clean-base.mp4",
    )

    assert render_base == cache
    assert render_base != "fresh-for-old-source.mp4"
    assert calls[0]["base_gcs_path"] == "new-clean-base.mp4"


def test_flag_off_reuses_fresh_cache_bound_to_same_source(monkeypatch) -> None:
    monkeypatch.setattr(settings, "motion_scenes_enabled", False)
    render_base, cache = gb._ensure_motion_base(
        job_id="job-1",
        variant_id="variant-1",
        variant={
            "motion_scenes": [_scene()],
            "motion_runtime_hash": MOTION_RUNTIME_HASH,
            "motion_base_path": "fresh-cache.mp4",
            "motion_base_source_path": "clean-base.mp4",
            "motion_cache_stale": False,
        },
        base_gcs_path="clean-base.mp4",
    )
    assert (render_base, cache) == ("fresh-cache.mp4", "fresh-cache.mp4")


def test_flag_off_fails_closed_when_motion_cache_needs_rebuild(monkeypatch) -> None:
    monkeypatch.setattr(settings, "motion_scenes_enabled", False)
    with pytest.raises(RuntimeError, match="needs a cache rebuild"):
        gb._ensure_motion_base(
            job_id="job-1",
            variant_id="variant-1",
            variant={
                "motion_scenes": [_scene()],
                "motion_runtime_hash": MOTION_RUNTIME_HASH,
                "motion_base_path": "cache-for-old-source.mp4",
                "motion_base_source_path": "old-clean-base.mp4",
                "motion_cache_stale": False,
            },
            base_gcs_path="new-clean-base.mp4",
        )


def test_worker_rejects_runtime_version_skew(monkeypatch) -> None:
    monkeypatch.setattr(settings, "motion_scenes_enabled", True)
    with pytest.raises(RuntimeError, match="motion runtime mismatch"):
        gb._ensure_motion_base(
            job_id="job-1",
            variant_id="variant-1",
            variant={
                "motion_scenes": [_scene()],
                "motion_runtime_hash": "old-runtime",
            },
            base_gcs_path="clean-base.mp4",
        )


def test_worker_rejects_motion_on_non_portrait_variant(monkeypatch) -> None:
    monkeypatch.setattr(settings, "motion_scenes_enabled", True)
    with pytest.raises(RuntimeError, match="require portrait orientation"):
        gb._ensure_motion_base(
            job_id="job-1",
            variant_id="variant-1",
            variant={
                "motion_scenes": [_scene()],
                "motion_runtime_hash": MOTION_RUNTIME_HASH,
                "orientation": "landscape",
            },
            base_gcs_path="clean-base.mp4",
        )


def test_retired_motion_cache_deletes_only_when_replaced(monkeypatch) -> None:
    deleted: list[str] = []
    monkeypatch.setattr("app.storage.delete_object_best_effort", deleted.append)
    gb._free_retired_motion_base({"motion_base_path": "old.mp4"}, "new.mp4")
    gb._free_retired_motion_base({"motion_base_path": "live.mp4"}, "live.mp4")
    assert deleted == ["old.mp4"]
