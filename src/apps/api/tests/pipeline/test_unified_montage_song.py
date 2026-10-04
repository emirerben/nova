"""KRI-374 lane D1: the background-song mode of the unified montage planner."""

from __future__ import annotations

import hashlib
import json

import pytest

from app.pipeline.guided_story import compile_execution_plan, validate_execution_plan
from app.pipeline.unified_montage import BriefView, UnifiedClip, plan_unified_montage
from app.schemas.user_song import SongLine
from tests.pipeline.user_song_helpers import SONG_GENERATION, SONG_ITEM_ID, analysis


def clips(count: int = 6, duration: float = 7.0) -> list[UnifiedClip]:
    return [
        UnifiedClip(
            media_id=f"c{i}",
            proxy_path=f"users/u/analysis-proxy-c{i}.mp4",
            generation="1",
            duration_s=duration,
            width=1920,
            height=1080,
        )
        for i in range(1, count + 1)
    ]


def song_plan(items=None, *, beats=None, lines=None, song_duration=120.0, **kwargs):
    base = analysis(duration_s=song_duration)
    return plan_unified_montage(
        items or clips(),
        song_beats=base.beats_s if beats is None else beats,
        song_lines=base.lines if lines is None else lines,
        song_duration_s=song_duration,
        song_plan_item_id=SONG_ITEM_ID,
        song_generation=SONG_GENERATION,
        **kwargs,
    )


def boundaries(plan) -> list[float]:
    total = 0.0
    out = []
    for cut in plan.snapshot.fast_cuts[:-1]:
        total = round(total + cut.output_duration_s, 3)
        out.append(total)
    return out


def test_cut_boundaries_land_on_the_windows_beats():
    plan = song_plan()
    song = plan.user_song
    assert song is not None and song.mode == "background" and song.takes == {}
    beats = {round(b - song.window_start_s, 3) for b in analysis().beats_s}
    snapped = [b for b in boundaries(plan) if any(abs(b - beat) <= 0.001 for beat in beats)]
    assert len(snapped) == len(boundaries(plan))
    assert plan.song_receipt["beat_aligned_cuts"] == len(boundaries(plan))
    # The window starts at a song beat (auto_best_section) and matches the video length.
    assert any(abs(song.window_start_s - b) < 1e-6 for b in analysis().beats_s)
    assert song.window_duration_s == pytest.approx(plan.snapshot.duration_s, abs=0.001)
    assert plan.snapshot.user_song == song


def test_cuts_are_emitted_without_beat_align_so_the_compiler_leaves_them_alone():
    plan = song_plan()
    assert all(cut.beat_align is False for cut in plan.snapshot.fast_cuts)
    compiled = compile_execution_plan(plan.guided_edit(), track=None)
    assert compiled["user_song"]["mode"] == "background"
    assert compiled["resolved_duration_s"] == pytest.approx(plan.snapshot.duration_s)
    starts = [m["output_start_s"] for m in compiled["story_timeline"]]
    assert starts[1:] == pytest.approx(boundaries(plan))
    assert all(m["beat_align"] is False for m in compiled["story_timeline"])
    validate_execution_plan(compiled, plan.guided_edit())


def test_a_labels_reading_time_is_kept_when_cuts_snap_to_beats():
    from app.pipeline.unified_montage import min_display_s

    view = BriefView(
        wants_per_clip_text=True,
        clip_literals={"c1": "A rather long literal label that needs reading", "c3": "Km 3"},
    )
    plan = song_plan(view=view)
    by_id = {cut.media_id: cut for cut in plan.snapshot.fast_cuts}
    for label in plan.snapshot.clip_labels or []:
        assert by_id[label.media_id].output_duration_s >= min_display_s(len(label.text)) - 0.002
    assert min(c.output_duration_s for c in plan.snapshot.fast_cuts) >= 0.8 - 0.002


def test_total_is_capped_at_the_song_length_and_dropped_clips_are_named():
    plan = song_plan(clips(10, 7.0), song_duration=6.0, view=BriefView(target_duration_s=30))
    assert plan.snapshot.duration_s <= 6.0
    assert plan.user_song.window_end_s <= 6.0 + 1e-6
    assert plan.user_song.window_start_s == 0
    assert plan.song_receipt["dropped_clip_ids"]
    kept = {cut.media_id for cut in plan.snapshot.fast_cuts}
    assert kept.isdisjoint(plan.song_receipt["dropped_clip_ids"])
    assert plan.snapshot.selected_media_ids == [c for c in plan.clip_ids]


def test_a_song_shorter_than_a_montage_is_refused():
    with pytest.raises(ValueError, match="too short"):
        song_plan(song_duration=2.0)


def test_sparse_or_missing_beats_keep_a_valid_plan():
    plan = song_plan(beats=[])
    assert plan.user_song.window_start_s == 0
    assert plan.song_receipt["beat_aligned_cuts"] == 0
    assert plan.snapshot.duration_s >= 3


def test_lyric_lines_are_accepted_as_models_or_dicts():
    lines_as_models = [
        SongLine(start_s=10, end_s=14, text="x"),
        SongLine(start_s=20, end_s=24, text="y"),
    ]
    as_dicts = [line.model_dump() for line in lines_as_models]
    assert (
        song_plan(lines=lines_as_models).user_song.window_start_s
        == song_plan(lines=as_dicts).user_song.window_start_s
    )


def test_the_song_needs_its_plan_item_and_generation():
    with pytest.raises(ValueError, match="plan item and generation"):
        plan_unified_montage(clips(), song_duration_s=60.0)


def test_the_receipt_names_the_song_only_when_there_is_one():
    assert "user_song" in song_plan().record()
    assert "user_song" not in plan_unified_montage(clips()).record()


# ── replay: a montage with no song is byte-identical to before KRI-374 ─────────

# Digests of the SAME fixture planned and compiled by the commit this lane was cut
# from (1245c401a, before any user_song code existed).
GOLDEN = {
    "snapshot": "ee0857b7b5bbf69f46dd16f3cf4e0ef50d34e2aa2a5da5c5f8ed9e6798cd6b47",
    "guided_edit": "9893772944d353234ff68b8225f1c0b87ebbc28ee24555d3f1b35f345d34e926",
    "compiled": "e8c3b0a8ecb3c9ea0f91f98097067f1c4286108c28c68c6b24194414a75bc911",
    "record": "7a8facc0d06739edb56ea02b1fd4c534b106f08df832ac2b5e0222ab035df429",
}


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_a_montage_without_a_song_replays_byte_identically():
    items = [
        UnifiedClip(
            media_id=f"c{i}",
            proxy_path=f"users/u/analysis-proxy-c{i}.mp4",
            generation="1",
            duration_s=d,
            width=1920,
            height=1080,
        )
        for i, d in enumerate([5.0, 6.5, 4.2, 8.0, 3.1], start=1)
    ]
    plan = plan_unified_montage(
        items, BriefView(target_duration_s=9), strategy={"opening_title": "Sahne"}
    )
    guided = plan.guided_edit()
    compiled = compile_execution_plan(guided, track=None)
    assert "user_song" not in plan.snapshot.model_dump(mode="json")
    assert "user_song" not in compiled
    assert {
        "snapshot": _digest(plan.snapshot.model_dump(mode="json")),
        "guided_edit": _digest(guided),
        "compiled": _digest(compiled),
        "record": _digest(plan.record()),
    } == GOLDEN


CATALOG_TRACK = {
    "track_id": "library-track-1",
    "title": "Library Song",
    "artist": "Somebody",
    "start_s": 0.0,
    "catalog_duration_s": 200.0,
    "beat_timestamps_s": [0.5 * i for i in range(1, 80)],
}


def test_a_matched_catalog_track_never_lands_beside_a_creator_song():
    """Production incident: the worker matches a library track for a montage without
    narration and hands it to the compiler, which made a reference-only `song_reference`
    that the creator-song validator refuses. Every other test compiled with `track=None`."""
    plan = song_plan()
    compiled = compile_execution_plan(plan.guided_edit(), track=dict(CATALOG_TRACK))
    assert compiled["user_song"]["mode"] == "background"
    assert compiled.get("song_reference") is None
    assert compiled.get("music") is None
    validate_execution_plan(compiled, plan.guided_edit())
