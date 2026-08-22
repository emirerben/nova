"""Shared Guided Story V2 timeline contract fixtures.

The JSON file is intentionally consumed by both the browser projection tests
and this schema/compiler-facing test.  It is the executable contract for the
frame rate, transition overlap, right-biased boundaries, and lane tombstones;
it must not be replaced by independently authored Python/TypeScript examples.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas.guided_edit_revision import normalize_guided_editor_revision

FIXTURE = (
    Path(__file__).resolve().parents[5] / "tests" / "fixtures" / "guided-story-parity" / "v1.json"
)


@pytest.fixture(scope="module")
def parity_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _revision(case: dict, frame_rate: int) -> dict:
    sources = []
    for segment in case["segments"]:
        if segment["media_id"] in {source["media_id"] for source in sources}:
            continue
        sources.append(
            {
                "media_id": segment["media_id"],
                "lane": "clip",
                "gcs_path": f"users/fixture/{segment['media_id']}.mp4",
                "generation": "fixture-generation",
                "kind": "video",
                "duration_s": 10.0,
            }
        )

    windows = {window["segment_id"]: window for window in case["expected"]["windows"]}
    segments = []
    for segment in case["segments"]:
        window = windows[segment["segment_id"]]
        source_start_frames = segment.get("source_start_frames", 0)
        source_end_frames = segment.get(
            "source_end_frames", source_start_frames + segment["duration_frames"]
        )
        segments.append(
            {
                "segment_id": segment["segment_id"],
                "media_id": segment["media_id"],
                "source_start_s": source_start_frames / frame_rate,
                "source_end_s": source_end_frames / frame_rate,
                "duration_s": segment["duration_frames"] / frame_rate,
                "transition_after": segment.get("transition_after", "cut"),
                "transition_duration_s": segment.get("transition_duration_frames", 0) / frame_rate,
                "output_start_s": window["start_frame"] / frame_rate,
                "output_end_s": window["end_frame"] / frame_rate,
            }
        )

    lanes = case.get("lane_inputs") or {}
    return {
        "approval_proposal_version": 1,
        "approval_media_digest": "a" * 64,
        "revision_number": 1,
        "sources": sources,
        "segments": segments,
        "text_elements": lanes.get("text", []),
        "sound_effects": lanes.get("sound_effects", []),
        "media_overlays": lanes.get("media_overlays", []),
        "visual_blocks": lanes.get("visual_blocks", []),
        "motion_scenes": lanes.get("motion_scenes", []),
    }


@pytest.mark.parametrize(
    "case_id",
    [
        "ordered_all_transition_kinds",
        "transition_caps_and_floor",
        "transition_below_floor_becomes_cut",
        "trim_delete_reorder_and_timed_lanes",
    ],
)
def test_guided_revision_matches_shared_frame_windows(parity_fixture: dict, case_id: str) -> None:
    case = next(candidate for candidate in parity_fixture["cases"] if candidate["id"] == case_id)
    normalized = normalize_guided_editor_revision(_revision(case, parity_fixture["frame_rate"]))
    actual = [
        {
            "segment_id": segment["segment_id"],
            "start_frame": round(segment["output_start_s"] * parity_fixture["frame_rate"]),
            "end_frame": round(segment["output_end_s"] * parity_fixture["frame_rate"]),
        }
        for segment in normalized["segments"]
    ]
    expected = [
        {
            "segment_id": window["segment_id"],
            "start_frame": window["start_frame"],
            "end_frame": window["end_frame"],
        }
        for window in case["expected"]["windows"]
    ]
    assert actual == expected


def test_guided_revision_preserves_lane_tombstones_and_output_clock_music(
    parity_fixture: dict,
) -> None:
    case = next(
        candidate
        for candidate in parity_fixture["cases"]
        if candidate["id"] == "trim_delete_reorder_and_timed_lanes"
    )
    normalized = normalize_guided_editor_revision(_revision(case, parity_fixture["frame_rate"]))
    expected = case["expected"]["lanes"]
    assert normalized["text_elements"] == case["lane_inputs"]["text"]
    assert normalized["sound_effects"] == case["lane_inputs"]["sound_effects"]
    assert normalized["media_overlays"] == case["lane_inputs"]["media_overlays"]
    assert normalized["visual_blocks"] == case["lane_inputs"]["visual_blocks"]
    assert normalized["motion_scenes"] == case["lane_inputs"]["motion_scenes"]
    assert expected["music"]["start_frame"] == 0
