"""KRI-989: a Visuals-pool photo (``asset-*``) in the selection is attached, never "missing".

Before the fix every explicit selection that named a pool photo produced the
``no_usable_sources`` conflict ("Some of the selected clips are no longer attached"), because
the photo has a manifest entry but no clip row.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.agents._schemas.creator_agent import CreatorMediaRef
from app.kria.planner import _attached_visual_ids, _enrich_best_parts_snapshot
from app.services.length_planning import build_length_plan

PHOTO = "asset-cfb745e0-513a-4ade-b2c0-8b1b34e54585"
CLIPS = ("clip-a", "clip-b", "clip-c")


def _snapshot() -> dict:
    return {
        "clip_assignments": [
            {"media_id": media_id, "kind": "video", "duration_s": duration, "analysis": {}}
            for media_id, duration in zip(CLIPS, (11.0, 3.0, 4.67), strict=True)
        ]
    }


def _strategy() -> dict:
    return {
        "edit_format": "montage",
        "audio_strategy": "licensed_music",
        "media_scope": "selected",
        "selected_media_ids": [*CLIPS, PHOTO],
        "creator_duration_s": 10,
    }


def _plan(**extra) -> dict:
    strategy = _strategy()
    return build_length_plan(
        strategy,
        _snapshot(),
        creator_target_s=10.0,
        selected_ids=strategy["selected_media_ids"],
        **extra,
    )


def test_attached_pool_photo_is_neither_missing_nor_a_source():
    plan = _plan(attached_visual_ids=[PHOTO])
    assert plan["conflicts"] == []
    assert [source["media_id"] for source in plan["sources"]] == list(CLIPS)
    assert "missing_source_ids" not in plan


def test_unattached_asset_id_is_reported_missing_by_name():
    plan = _plan()
    assert [conflict["code"] for conflict in plan["conflicts"]] == ["no_usable_sources"]
    assert plan["missing_source_ids"] == [PHOTO]


def test_plan_without_missing_ids_has_no_missing_source_ids_key():
    strategy = {**_strategy(), "selected_media_ids": list(CLIPS)}
    plan = build_length_plan(
        strategy, _snapshot(), creator_target_s=10.0, selected_ids=strategy["selected_media_ids"]
    )
    assert "missing_source_ids" not in plan


def test_enrich_snapshot_with_manifest_photo_has_no_conflicts():
    manifest = SimpleNamespace(
        media=[
            *(CreatorMediaRef(media_id=media_id, kind="video") for media_id in CLIPS),
            CreatorMediaRef(media_id=PHOTO, kind="image"),
        ]
    )
    assert _attached_visual_ids(manifest) == (PHOTO,)
    assert _attached_visual_ids(None) == ()
    snapshot = _enrich_best_parts_snapshot(
        _strategy(),
        _snapshot(),
        None,
        attached_visual_ids=_attached_visual_ids(manifest),
    )
    assert snapshot["length_plan"]["conflicts"] == []
