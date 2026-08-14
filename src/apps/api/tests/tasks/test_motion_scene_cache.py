from __future__ import annotations

import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.pipeline.motion_scene import (
    LEGACY_MOTION_RUNTIME_HASH,
    MOTION_RUNTIME_HASH,
    MOTION_RUNTIME_V2_HASH,
    MOTION_RUNTIME_V3_HASH,
)
from app.tasks import generative_build as gb


@pytest.fixture(autouse=True)
def _stable_object_metadata(monkeypatch):
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda path: SimpleNamespace(
            path=path,
            generation=f"generation:{path}",
            etag=f"etag:{path}",
            size=1024,
            content_type="image/png" if path.endswith(".png") else "video/mp4",
        ),
    )


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


def _cache_identity(source: str = "clean-base.mp4", scenes: list[dict] | None = None) -> str:
    return gb._motion_cache_identity(
        source_path=source,
        runtime_hash=MOTION_RUNTIME_HASH,
        scenes=scenes or [_scene()],
        source_identity=gb._motion_object_identity(source),
    )


@pytest.mark.parametrize("variant", [{}, {"motion_scenes": []}])
def test_no_motion_scenes_skip_runtime_validation(monkeypatch, variant) -> None:
    def fail_if_called(_value):
        raise AssertionError("no-motion reburn must not load the optional runtime")

    monkeypatch.setattr(
        "app.pipeline.motion_scene.validate_motion_instances",
        fail_if_called,
    )

    assert gb._ensure_motion_base(
        job_id="job-1",
        variant_id="variant-1",
        variant=variant,
        base_gcs_path="clean-base.mp4",
    ) == ("clean-base.mp4", None)


def test_malformed_falsey_motion_scenes_are_still_validated() -> None:
    with pytest.raises(ValueError, match="is not of type 'array'"):
        gb._ensure_motion_base(
            job_id="job-1",
            variant_id="variant-1",
            variant={"motion_scenes": {}},
            base_gcs_path="clean-base.mp4",
        )


def test_creator_layer_base_orders_visual_blocks_before_motion(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def _visual(**kwargs):
        calls.append(("visual", kwargs["base_gcs_path"]))
        return "visual-base.mp4", "visual-base.mp4"

    def _motion(**kwargs):
        calls.append(("motion", kwargs["base_gcs_path"]))
        kwargs["identity_out"].update(renderer_hash="runtime-hash", cache_identity="cache-identity")
        return "motion-base.mp4", "motion-base.mp4"

    monkeypatch.setattr(gb, "_ensure_visual_blocks_base", _visual)
    monkeypatch.setattr(gb, "_ensure_motion_base", _motion)

    result = gb._ensure_creator_layer_base(
        job_id="job-1",
        variant_id="variant-1",
        variant={},
        base_gcs_path="clean-base.mp4",
    )

    assert calls == [("visual", "clean-base.mp4"), ("motion", "visual-base.mp4")]
    assert result == (
        "motion-base.mp4",
        "visual-base.mp4",
        "motion-base.mp4",
        "visual-base.mp4",
        {"renderer_hash": "runtime-hash", "cache_identity": "cache-identity"},
    )


def test_creator_layer_base_removes_partial_cache_when_motion_fails(monkeypatch) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(
        gb,
        "_ensure_visual_blocks_base",
        lambda **_kwargs: ("new-visual.mp4", "new-visual.mp4"),
    )
    monkeypatch.setattr(
        gb,
        "_ensure_motion_base",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("motion failed")),
    )
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: deleted.append(path),
    )

    with pytest.raises(RuntimeError, match="motion failed"):
        gb._ensure_creator_layer_base(
            job_id="job-1",
            variant_id="variant-1",
            variant={"visual_blocks_base_path": "old-visual.mp4"},
            base_gcs_path="clean-base.mp4",
        )

    assert deleted == ["new-visual.mp4"]


def test_motion_object_identity_fails_closed_for_missing_or_oversized_resources(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: (_ for _ in ()).throw(FileNotFoundError("gone")),
    )
    with pytest.raises(RuntimeError, match="resource is missing"):
        gb._motion_object_identity("missing.mp4")

    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda path: SimpleNamespace(
            path=path,
            generation="generation",
            etag="etag",
            size=26 * 1024 * 1024,
            content_type="image/png",
        ),
    )
    with pytest.raises(RuntimeError, match="invalid size"):
        gb._motion_object_identity("too-large.png", image=True)


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
    assert calls[0]["source_generation"] == "generation:visual-base.mp4"
    assert calls[0]["asset_generations"] == {}


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
            "motion_applied_runtime_hash": MOTION_RUNTIME_HASH,
            "motion_cache_stale": False,
            "motion_cache_identity": _cache_identity(),
        },
        base_gcs_path="clean-base.mp4",
    )
    assert (render_base, cache) == ("fresh-cache.mp4", "fresh-cache.mp4")


def test_legacy_route_trace_cache_records_the_actual_current_renderer(monkeypatch) -> None:
    monkeypatch.setattr(settings, "motion_scenes_enabled", True)
    render_base, cache = gb._ensure_motion_base(
        job_id="job-1",
        variant_id="variant-1",
        variant={
            "motion_scenes": [_scene()],
            "motion_runtime_hash": LEGACY_MOTION_RUNTIME_HASH,
            "motion_base_path": "legacy-input-current-renderer.mp4",
            "motion_base_source_path": "clean-base.mp4",
            "motion_applied_runtime_hash": MOTION_RUNTIME_HASH,
            "motion_cache_stale": False,
            "motion_cache_identity": _cache_identity(),
        },
        base_gcs_path="clean-base.mp4",
    )
    assert (render_base, cache) == (
        "legacy-input-current-renderer.mp4",
        "legacy-input-current-renderer.mp4",
    )


@pytest.mark.parametrize(
    "persisted_runtime",
    [MOTION_RUNTIME_V2_HASH, MOTION_RUNTIME_V3_HASH],
)
def test_known_creator_runtime_is_rebuilt_with_current_renderer(
    monkeypatch,
    persisted_runtime: str,
) -> None:
    calls: list[dict] = []
    identity: dict = {}
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
            "motion_runtime_hash": persisted_runtime,
            "motion_base_path": "previous-cache.mp4",
            "motion_base_source_path": "clean-base.mp4",
            "motion_applied_runtime_hash": persisted_runtime,
            "motion_cache_stale": False,
        },
        base_gcs_path="clean-base.mp4",
        identity_out=identity,
    )

    assert render_base == cache
    assert render_base != "previous-cache.mp4"
    assert calls
    assert identity["renderer_hash"] == MOTION_RUNTIME_HASH


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
            "motion_cache_identity": _cache_identity(),
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
            "motion_applied_runtime_hash": MOTION_RUNTIME_HASH,
            "motion_cache_stale": False,
            "motion_cache_identity": _cache_identity(),
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


def test_worker_accepts_landscape_motion(monkeypatch) -> None:
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
            "orientation": "landscape",
        },
        base_gcs_path="clean-base.mp4",
    )
    assert render_base == cache
    assert calls[0]["base_gcs_path"] == "clean-base.mp4"


def test_motion_cache_identity_changes_with_scene_and_asset_identity() -> None:
    base = _cache_identity()
    changed_copy = [{**_scene(), "intensity": 0.4}]
    media = [
        {
            **_scene(),
            "preset_id": "card_stack",
            "params": {
                "assets": [
                    {"asset_id": "a", "gcs_path": "users/u/plan/i/pool/a.png"},
                    {"asset_id": "b", "gcs_path": "users/u/plan/i/pool/b.png"},
                ]
            },
        }
    ]
    changed_asset = [
        {
            **media[0],
            "params": {
                "assets": [
                    {"asset_id": "a", "gcs_path": "users/u/plan/i/pool/a-v2.png"},
                    {"asset_id": "b", "gcs_path": "users/u/plan/i/pool/b.png"},
                ]
            },
        }
    ]
    assert _cache_identity(scenes=changed_copy) != base
    assert _cache_identity(scenes=media) != _cache_identity(scenes=changed_asset)


def test_render_revalidates_media_assets_and_hashes_live_content(monkeypatch) -> None:
    job_id = uuid.uuid4()
    plan_item_id = uuid.uuid4()
    user_id = uuid.uuid4()
    asset_ids = [uuid.uuid4(), uuid.uuid4()]
    scenes = [
        {
            **_scene(),
            "preset_id": "card_stack",
            "params": {
                "assets": [
                    {
                        "asset_id": str(asset_id),
                        "gcs_path": f"users/{user_id}/plan/{plan_item_id}/pool/{asset_id}.png",
                    }
                    for asset_id in asset_ids
                ]
            },
        }
    ]
    rows = [
        SimpleNamespace(
            id=asset_id,
            user_id=user_id,
            status="ready",
            kind="image",
            gcs_path=scenes[0]["params"]["assets"][index]["gcs_path"],
            content_hash=f"hash-{index}",
        )
        for index, asset_id in enumerate(asset_ids)
    ]
    db = MagicMock()
    db.get.return_value = SimpleNamespace(
        content_plan_item_id=plan_item_id,
        user_id=user_id,
    )
    db.execute.return_value.scalars.return_value.all.return_value = rows
    monkeypatch.setattr(gb, "_sync_session", lambda: nullcontext(db))

    identities = gb._motion_asset_identities(job_id=str(job_id), scenes=scenes)
    assert {identity["content_hash"] for identity in identities} == {"hash-0", "hash-1"}
    original = gb._motion_cache_identity(
        source_path="base.mp4",
        runtime_hash=MOTION_RUNTIME_HASH,
        scenes=scenes,
        asset_identities=identities,
    )
    changed = gb._motion_cache_identity(
        source_path="base.mp4",
        runtime_hash=MOTION_RUNTIME_HASH,
        scenes=scenes,
        asset_identities=[{**identities[0], "content_hash": "replaced"}, identities[1]],
    )
    assert changed != original


def test_motion_cache_identity_changes_when_source_generation_is_replaced(monkeypatch) -> None:
    original = _cache_identity()
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda path: SimpleNamespace(
            path=path,
            generation="replacement-generation",
            etag="replacement-etag",
            size=2048,
            content_type="video/mp4",
        ),
    )
    assert _cache_identity() != original


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "failed"),
        ("kind", "video"),
        ("gcs_path", "users/other/plan/item/pool/wrong.png"),
        ("user_id", uuid.UUID(int=0)),
    ],
)
def test_render_fails_closed_when_media_asset_changed(monkeypatch, field, value) -> None:
    job_id = uuid.uuid4()
    plan_item_id = uuid.uuid4()
    user_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    second_asset_id = uuid.uuid4()
    path = f"users/{user_id}/plan/{plan_item_id}/pool/{asset_id}.png"
    scene = {
        **_scene(),
        "preset_id": "card_stack",
        "params": {
            "assets": [
                {"asset_id": str(asset_id), "gcs_path": path},
                {"asset_id": str(second_asset_id), "gcs_path": f"{path}.second.png"},
            ]
        },
    }
    row = SimpleNamespace(
        id=asset_id,
        user_id=user_id,
        status="ready",
        kind="image",
        gcs_path=path,
        content_hash="hash",
    )
    setattr(row, field, value)
    second_row = SimpleNamespace(
        id=second_asset_id,
        user_id=user_id,
        status="ready",
        kind="image",
        gcs_path=f"{path}.second.png",
        content_hash="hash-second",
    )
    db = MagicMock()
    db.get.return_value = SimpleNamespace(content_plan_item_id=plan_item_id, user_id=user_id)
    db.execute.return_value.scalars.return_value.all.return_value = [row, second_row]
    monkeypatch.setattr(gb, "_sync_session", lambda: nullcontext(db))

    with pytest.raises(RuntimeError, match="no longer an owned ready image"):
        gb._motion_asset_identities(job_id=str(job_id), scenes=[scene])


def test_retired_motion_cache_deletes_only_when_replaced(monkeypatch) -> None:
    deleted: list[str] = []
    monkeypatch.setattr("app.storage.delete_object_best_effort", deleted.append)
    gb._free_retired_motion_base({"motion_base_path": "old.mp4"}, "new.mp4")
    gb._free_retired_motion_base({"motion_base_path": "live.mp4"}, "live.mp4")
    assert deleted == ["old.mp4"]
