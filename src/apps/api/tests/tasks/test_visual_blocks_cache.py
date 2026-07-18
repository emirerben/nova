from __future__ import annotations

from app.config import settings
from app.tasks import generative_build as gb


def _block() -> dict:
    return {
        "version": 1,
        "id": "montage-1",
        "kind": "montage",
        "start_s": 0.0,
        "end_s": 3.0,
        "timing_mode": "manual",
        "origin": "user",
        "transition_in": "cut",
        "transition_out": "cut",
        "audio_policy": {"base": "continue", "sfx": "continue"},
        "shots": [
            {
                "id": f"shot-{index}",
                "asset_id": f"asset-{index}",
                "src_gcs_path": f"users/u/plan/i/pool/{index}.jpg",
                "kind": "image",
                "start_offset_s": float(index),
                "duration_s": 1.0,
                "crop": {"x_frac": 0.5, "y_frac": 0.5, "scale": 1.0},
                "motion": "none",
            }
            for index in range(3)
        ],
    }


def test_stale_visual_block_cache_is_rebuilt(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(
        "app.pipeline.visual_blocks.apply_visual_blocks",
        lambda **kwargs: calls.append(kwargs) or "signed",
    )

    render_base, cache = gb._ensure_visual_blocks_base(
        job_id="job-1",
        variant_id="variant-1",
        variant={
            "visual_blocks": [_block()],
            "visual_blocks_base_path": "old-cache.mp4",
            "visual_blocks_cache_stale": True,
        },
        base_gcs_path="clean-base.mp4",
    )

    assert render_base == cache
    assert render_base != "old-cache.mp4"
    assert calls[0]["base_gcs_path"] == "clean-base.mp4"


def test_fresh_visual_block_cache_is_reused(monkeypatch) -> None:
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    render_base, cache = gb._ensure_visual_blocks_base(
        job_id="job-1",
        variant_id="variant-1",
        variant={
            "visual_blocks": [_block()],
            "visual_blocks_base_path": "fresh-cache.mp4",
            "visual_blocks_cache_stale": False,
        },
        base_gcs_path="clean-base.mp4",
    )
    assert (render_base, cache) == ("fresh-cache.mp4", "fresh-cache.mp4")


def test_retired_visual_block_cache_deletes_only_when_replaced(monkeypatch) -> None:
    deleted: list[str] = []
    monkeypatch.setattr("app.storage.delete_object_best_effort", deleted.append)

    gb._free_retired_visual_blocks_base(
        {"visual_blocks_base_path": "old-cache.mp4"}, "new-cache.mp4"
    )
    gb._free_retired_visual_blocks_base(
        {"visual_blocks_base_path": "still-live.mp4"}, "still-live.mp4"
    )

    assert deleted == ["old-cache.mp4"]
