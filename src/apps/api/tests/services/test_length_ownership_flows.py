"""KRI-492: approved length envelopes survive planner-to-render boundaries."""

from __future__ import annotations

import json

import pytest

from app.kria.planner import _enrich_best_parts_snapshot
from app.pipeline.unified_montage import UnifiedClip, plan_unified_montage
from app.services.creator_render_contract import build_render_contract
from app.services.length_policy_state import read_length_policy, stamp_length_policy


def _snapshot(*, voiceover_s: float | None = None) -> dict:
    snapshot = {
        "clip_assignments": [
            {
                "media_id": "first",
                "duration_s": 18.0,
                "generation": "first-generation",
                "gcs_path": "users/u/first.mp4",
            },
            {
                "media_id": "middle",
                "duration_s": 9.0,
                "generation": "middle-generation",
                "gcs_path": "users/u/middle.mp4",
            },
            {
                "media_id": "last",
                "duration_s": 12.0,
                "generation": "last-generation",
                "gcs_path": "users/u/last.mp4",
            },
        ]
    }
    if voiceover_s is not None:
        snapshot["voiceover"] = {"duration_s": voiceover_s}
    return snapshot


def _clips(plan: dict) -> list[UnifiedClip]:
    return [
        UnifiedClip(
            media_id=row["media_id"],
            proxy_path=row["gcs_path"],
            generation=row["generation"],
            duration_s=row["duration_s"],
        )
        for row in plan["sources"]
    ]


# Value: protects=model-selected 24-second targets from becoming creator promises;
# fails_when=planner/policy/compiler promote target_duration_s without creator proof;
# why_new=joins draft planning, approved-policy reload, and the portable compiler;
# seam=none; no_claim=does not exercise a database transaction or device export.
def test_model_target_keeps_voiceover_natural_owner_through_policy_reload_and_compiler() -> None:
    strategy = {"audio_strategy": "voiceover", "target_duration_s": 24.0}
    enriched = _enrich_best_parts_snapshot(strategy, _snapshot(voiceover_s=18.0), None)
    assert enriched is not None
    envelope = enriched["length_plan"]

    assert envelope["owner"] == envelope["natural_owner"] == "voiceover"
    assert envelope["target_duration_s"] is None
    assert envelope["natural_duration_s"] == 18.0

    contract = build_render_contract(strategy, generation_id="approved-generation")
    assert contract is not None
    assert contract.duration_s is None
    saved = json.loads(json.dumps(stamp_length_policy({}, strategy, contract, mode="enforce")))
    policy = read_length_policy(saved, contract)
    assert policy is not None
    assert policy.owner == policy.natural_owner == "voiceover"

    compiled = plan_unified_montage(
        _clips(envelope), strategy=strategy, approved_length_plan=envelope
    )
    assert [
        (cut.media_id, cut.source_start_s, cut.source_end_s) for cut in compiled.snapshot.fast_cuts
    ] == [
        (row["media_id"], row["source_start_s"], row["source_end_s"]) for row in envelope["windows"]
    ]


# Value: protects=the recorded voiceover's capacity and distinguishes a missing receipt;
# fails_when=planner invents a 24-second voice duration or silently records one as present;
# why_new=tests both persisted snapshot shapes at the actual planner boundary;
# seam=none; no_claim=does not validate audio-file probing.
@pytest.mark.parametrize(("recorded_s", "expected_natural_s"), [(18.0, 18.0), (None, None)])
def test_voiceover_planning_records_present_or_missing_duration_capacity(
    recorded_s: float | None, expected_natural_s: float | None
) -> None:
    enriched = _enrich_best_parts_snapshot(
        {"audio_strategy": "voiceover", "target_duration_s": 24.0},
        _snapshot(voiceover_s=recorded_s),
        None,
    )
    assert enriched is not None
    envelope = enriched["length_plan"]

    assert envelope["natural_owner"] == "voiceover"
    assert envelope["natural_duration_s"] == expected_natural_s
    assert envelope["capacity_raw_s"] == 39.0
    assert envelope["capacity_s"] == 39.0


# Value: protects=the exact approved licensed-song section as the natural duration source;
# fails_when=section bounds are discarded, widened to catalog duration, or model target wins;
# why_new=travels through snapshot enrichment rather than testing music-window helpers alone;
# seam=none; no_claim=does not query the live catalog row.
def test_frozen_licensed_song_section_is_the_planned_capacity() -> None:
    strategy = {
        "audio_strategy": "licensed_music",
        "target_duration_s": 24.0,
        "selected_music_section": {
            "status": "selected",
            "track_id": "track-1",
            "start_s": 12.25,
            "end_s": 31.75,
            "catalog_duration_s": 90.0,
        },
    }
    enriched = _enrich_best_parts_snapshot(strategy, _snapshot(), None)
    assert enriched is not None
    envelope = enriched["length_plan"]

    assert envelope["owner"] == envelope["natural_owner"] == "song"
    assert envelope["target_duration_s"] is None
    assert envelope["natural_duration_s"] == 19.5
    assert envelope["licensed_music"] == strategy["selected_music_section"]
    assert sum(row["source_end_s"] - row["source_start_s"] for row in envelope["windows"]) == 19.5


# Value: protects=creator-selected source membership, attachment order, and immutable identities;
# fails_when=planner includes unselected media, reorders pins, or drops generation/path provenance;
# why_new=checks the serialized envelope consumed by both render routes;
# seam=none; no_claim=does not assert final visual ordering inside either renderer.
def test_planner_enrichment_preserves_selected_sources_order_and_provenance() -> None:
    snapshot = _snapshot()
    strategy = {
        "audio_strategy": "licensed_music",
        "selected_media_ids": ["last", "first"],
    }
    enriched = _enrich_best_parts_snapshot(strategy, snapshot, None)
    assert enriched is not None
    envelope = enriched["length_plan"]

    # The attachment sequence remains the approved ordering basis; selected IDs only scope it.
    assert [row["media_id"] for row in envelope["sources"]] == ["first", "last"]
    assert [row["media_id"] for row in envelope["windows"]] == ["first", "last"]
    assert [(row["generation"], row["gcs_path"]) for row in envelope["sources"]] == [
        ("first-generation", "users/u/first.mp4"),
        ("last-generation", "users/u/last.mp4"),
    ]
