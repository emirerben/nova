"""KRI-374 lane D1: lip-sync tiling, creator-confirmed order, and the sync invariant."""

from __future__ import annotations

import pytest

from app.pipeline.guided_story import GuidedStoryError, GuidedStoryExecutionPlan
from app.pipeline.lipsync_montage import (
    LipsyncPlanError,
    LipsyncSyncError,
    lipsync_sync_error_s,
    plan_lipsync_montage,
    refuse_lipsync_rate_change,
    resync_lipsync_moments,
)
from app.schemas.edit_proposal import MAX_PROPOSAL_DURATION_S
from app.schemas.user_song import UserSongPlan, UserSongTake
from tests.pipeline.user_song_helpers import (
    SONG_ITEM_ID,
    alignment,
    ambiguous,
    analysis,
    compiled_plan,
    confident,
    photo,
    take,
    unmatched,
)


def plan(clips, rows, *, order=None, song=None):
    return plan_lipsync_montage(
        clips,
        alignment(*rows),
        song or analysis(),
        confirmed_order=order,
        plan_item_id=SONG_ITEM_ID,
    )


def blocks(result):
    return result.song_receipt["placed"]


def assert_in_sync(result, compiled: GuidedStoryExecutionPlan):
    song = result.user_song
    assert song is not None and song.mode == "lipsync"
    checked = 0
    for moment in compiled.story_timeline:
        pinned = song.takes.get(moment.media_id)
        if pinned is None:
            continue
        checked += 1
        assert (
            lipsync_sync_error_s(
                output_start_s=moment.output_start_s,
                source_start_s=moment.source_start_s,
                delta_s=pinned.delta_s,
                window_start_s=song.window_start_s,
            )
            <= 0.001
        )
    assert checked == len(song.takes)


def test_overlapping_takes_switch_inside_the_overlap_on_a_lyric_line():
    # A covers 10.3-29.7, B covers 25.3-44.7 (song time): they overlap 25.3-29.7.
    result = plan([take("A"), take("B")], [confident("A", 10), confident("B", 25)])
    a, b = blocks(result)
    assert (a["media_id"], b["media_id"]) == ("A", "B")
    assert a["song_end_s"] == b["song_start_s"]
    assert 25.3 <= a["song_end_s"] <= 29.7
    assert result.song_receipt["switches"][0]["snapped_to"] == "line"
    # Lyric lines start every 4 s from 2.0 s; 26.0 is the one inside the overlap.
    assert a["song_end_s"] == pytest.approx(26.0)
    assert result.user_song.window_start_s == pytest.approx(10.3)
    assert result.user_song.window_end_s == pytest.approx(44.7)
    assert result.snapshot.duration_s == pytest.approx(34.4)
    assert result.ordering_basis == "song_time"


def test_switch_falls_back_to_a_beat_when_the_song_has_no_lyric_lines():
    song = analysis(line_starts=())
    result = plan([take("A"), take("B")], [confident("A", 10), confident("B", 25)], song=song)
    assert result.song_receipt["switches"][0]["snapped_to"] == "beat"
    assert (result.song_receipt["placed"][0]["song_end_s"] - 0.25) % 0.5 == pytest.approx(
        0, abs=1e-6
    )


def test_a_take_inside_another_takes_coverage_is_left_out():
    contained = take("C", 5)  # covers 15.3-19.7, fully inside A
    result = plan(
        [take("A"), take("B"), contained],
        [confident("A", 10), confident("B", 25), confident("C", 15)],
    )
    assert [b["media_id"] for b in blocks(result)] == ["A", "B"]
    assert {"media_id": "C", "reason": "overlapped"} in result.song_receipt["dropped"]
    assert "C" not in result.user_song.takes


def test_each_take_is_used_once_in_song_order_whatever_the_attachment_order():
    clips = [take("B"), take("A"), take("C")]
    rows = [confident("A", 0), confident("B", 17), confident("C", 34)]
    result = plan(clips, rows)
    assert [b["media_id"] for b in blocks(result)] == ["A", "B", "C"]
    media = [cut.media_id for cut in result.snapshot.fast_cuts]
    assert len(media) == len(set(media))


def test_gap_is_filled_with_muted_broll_from_unmatched_takes_and_visuals():
    clips = [take("A", 12), take("B", 20), take("U", 10), photo()]
    rows = [confident("A", 10), confident("B", 30), unmatched("U")]
    result = plan(clips, rows)
    ids = [cut.media_id for cut in result.snapshot.fast_cuts]
    assert ids[0] == "A" and ids[-1] == "B"
    assert result.song_receipt["broll_ids"] == [i for i in ids[1:-1]]
    assert set(result.song_receipt["broll_ids"]) <= {"U", photo().media_id}
    # The window spans both takes; B-roll covers the 21.7-30.3 gap exactly.
    assert result.user_song.window_start_s == pytest.approx(10.3)
    assert result.user_song.window_end_s == pytest.approx(49.7)
    assert sum(c.output_duration_s for c in result.snapshot.fast_cuts) == pytest.approx(39.4)
    # B-roll is never a pinned take: it has no song position.
    assert set(result.user_song.takes) == {"A", "B"}
    assert compiled_plan(result).resolved_duration_s == pytest.approx(39.4)


def test_gap_without_broll_shrinks_to_the_largest_covered_span():
    # A covers 11.4 s, B 19.4 s with a gap between them and nothing to fill it.
    result = plan([take("A", 12), take("B", 20)], [confident("A", 10), confident("B", 30)])
    assert [b["media_id"] for b in blocks(result)] == ["B"]
    assert result.user_song.window_start_s == pytest.approx(30.3)
    assert result.user_song.window_end_s == pytest.approx(49.7)
    assert {"media_id": "A", "reason": "disconnected"} in result.song_receipt["dropped"]


def test_small_gap_is_bridged_by_the_takes_own_spare_margin():
    # A ends at 22.3 (cover), B starts at 22.9: a 0.6 s gap closes into both margins.
    result = plan([take("A", 12.6), take("B", 20)], [confident("A", 10), confident("B", 22.6)])
    assert result.song_receipt["bridged_gaps"] == 1
    first, second = result.snapshot.fast_cuts
    assert first.media_id == "A" and second.media_id == "B"
    # Bridged cuts stay inside the real take: never before its first frame.
    assert first.source_start_s >= 0 and second.source_start_s >= 0
    assert first.source_end_s <= 12.6 + 1e-6


def test_window_is_capped_at_120_seconds_keeping_the_densest_stretch():
    song = analysis(duration_s=300.0)
    # Sparse start (two thin takes with a hole), then a dense continuous stretch.
    clips = [take("S1", 6), take("S2", 6)] + [take(f"D{i}", 30) for i in range(1, 8)]
    rows = [confident("S1", 5), confident("S2", 40)] + [
        confident(f"D{i}", 80 + (i - 1) * 25) for i in range(1, 8)
    ]
    result = plan(clips, rows, song=song)
    window = result.user_song.window_end_s - result.user_song.window_start_s
    assert window <= MAX_PROPOSAL_DURATION_S + 1e-6
    assert result.song_receipt["capped_at_s"] == MAX_PROPOSAL_DURATION_S
    assert result.user_song.window_start_s >= 79
    assert result.snapshot.duration_s <= MAX_PROPOSAL_DURATION_S
    assert_in_sync(result, compiled_plan(result))


def test_cap_never_leaves_a_sliver_segment():
    song = analysis(duration_s=400.0)
    clips = [take(f"T{i}", 40) for i in range(8)]
    rows = [confident(f"T{i}", i * 35) for i in range(8)]
    result = plan(clips, rows, song=song)
    for cut in result.snapshot.fast_cuts:
        assert cut.output_duration_s >= 1.0 - 1e-6
    assert result.snapshot.duration_s <= MAX_PROPOSAL_DURATION_S + 1e-6


def test_every_segment_is_at_least_a_second():
    clips = [take(f"T{i}", 14) for i in range(6)]
    rows = [confident(f"T{i}", i * 11) for i in range(6)]
    result = plan(clips, rows)
    assert all(c.output_duration_s >= 1.0 - 1e-6 for c in result.snapshot.fast_cuts)


def test_ambiguous_and_unmatched_takes_are_never_placed_by_song_time():
    clips = [take("A", 30), take("X", 20), take("U", 20)]
    rows = [confident("A", 10), ambiguous("X", 60, 80), unmatched("U")]
    result = plan(clips, rows)
    assert set(result.user_song.takes) == {"A"}
    assert [b["media_id"] for b in blocks(result)] == ["A"]
    reasons = {row["media_id"]: row["reason"] for row in result.song_receipt["dropped"]}
    assert reasons == {"X": "ambiguous_unconfirmed", "U": "unmatched"}


def test_uncertain_take_may_serve_as_broll_but_is_not_pinned():
    clips = [take("A", 12), take("B", 20), take("X", 12)]
    rows = [confident("A", 10), confident("B", 30), ambiguous("X", 60, 80)]
    result = plan(clips, rows)
    assert "X" in result.song_receipt["broll_ids"]
    assert "X" not in result.user_song.takes


def test_creator_confirmed_order_resolves_an_ambiguous_take_to_its_fitting_alternate():
    clips = [take("A", 30), take("X", 20), take("B", 30)]
    # X's best guess (60) sits after B; the creator says A, X, B so the alternate
    # at 35 (between A's 10.3 and B's 50.3) is the one that fits.
    rows = [confident("A", 10), ambiguous("X", 60, 35, 80), confident("B", 50)]
    result = plan(clips, rows, order=["A", "X", "B"])
    assert [b["media_id"] for b in blocks(result)] == ["A", "X", "B"]
    pinned = result.user_song.takes["X"]
    assert pinned.delta_s == 35 and pinned.confirmed_by_creator is True
    assert result.user_song.takes["A"].confirmed_by_creator is False
    assert_in_sync(result, compiled_plan(result))


def test_confirmed_order_with_no_fitting_alternate_leaves_the_take_out_of_the_song():
    clips = [take("A", 30), take("X", 20), take("B", 30)]
    rows = [confident("A", 10), ambiguous("X", 60, 80), confident("B", 50)]
    result = plan(clips, rows, order=["A", "X", "B"])
    assert "X" not in result.user_song.takes
    # With nowhere on the song to put it, X only plays as muted B-roll in A->B's gap.
    assert result.song_receipt["broll_ids"] == ["X"]


def test_confirmed_order_does_not_place_an_unmatched_take():
    clips = [take("A", 30), take("U", 20), take("B", 30)]
    rows = [confident("A", 10), unmatched("U"), confident("B", 50)]
    result = plan(clips, rows, order=["A", "U", "B"])
    assert "U" not in result.user_song.takes


def test_a_take_missing_from_the_confirmed_order_stays_unplaced():
    clips = [take("A", 30), take("X", 20), take("B", 30)]
    rows = [confident("A", 10), ambiguous("X", 35), confident("B", 50)]
    result = plan(clips, rows, order=["A", "B"])
    assert "X" not in result.user_song.takes


def test_source_start_follows_the_song_clock_after_compile():
    clips = [take("A"), take("B"), take("C", 25)]
    rows = [confident("A", 10.0004), confident("B", 25.0006), confident("C", 41.123)]
    result = plan(clips, rows)
    compiled = compiled_plan(result)
    assert_in_sync(result, compiled)
    for moment in compiled.story_timeline:
        pinned = result.user_song.takes[moment.media_id]
        offset = moment.source_start_s - moment.output_start_s
        assert offset == pytest.approx(result.user_song.window_start_s - pinned.delta_s, abs=0.001)


def _manual_plan(delta_a: float, delta_b: float, *, window_start: float = 20.0):
    """Two 5 s moments reading two 30 s takes that both cover the window."""
    media = ("A", "B")
    moments = [
        {
            "moment_id": f"m{i}",
            "beat_id": f"m{i}",
            "topic": "t",
            "media_id": media_id,
            "lane": "clip",
            "kind": "video",
            "gcs_path": f"users/u/analysis-proxy-{media_id}.mp4",
            "generation": "1",
            "layout": "fullscreen",
            "source_start_s": 0,
            "source_end_s": 5,
            "output_start_s": 5 * i,
            "output_end_s": 5 * i + 5,
            "duration_s": 5,
        }
        for i, media_id in enumerate(media)
    ]
    deltas = {"A": delta_a, "B": delta_b}
    song = UserSongPlan(
        mode="lipsync",
        plan_item_id=SONG_ITEM_ID,
        generation=7,
        duration_s=120,
        window_start_s=window_start,
        window_end_s=window_start + 10,
        takes={m: UserSongTake(delta_s=d) for m, d in deltas.items()},
    )
    return GuidedStoryExecutionPlan.model_validate(
        {
            "compiler_version": 7,
            "proposal_version": 1,
            "media_digest": "b" * 64,
            "direction": "fast_montage",
            "goal": "",
            "pace": "fast",
            "approved_duration_s": 10,
            "resolved_duration_s": 10,
            "selected_media_ids": list(media),
            "editor_revision_number": 1,
            "story_timeline": moments,
            "beat_windows": [
                {
                    "beat_id": f"m{i}",
                    "approved_duration_s": 5,
                    "resolved_duration_s": 5,
                    "start_s": 5 * i,
                    "end_s": 5 * i + 5,
                }
                for i in range(2)
            ],
            "text_elements": [],
            "transition_policy": {"type": "none", "duration_s": 0},
            "typography": {"style_id": "guided_story_v2", "font": "Inter"},
            "user_song": song.model_dump(mode="json"),
        }
    )


def test_resync_after_a_reorder_keeps_each_take_on_its_song_position():
    plan_ = _manual_plan(delta_a=10, delta_b=12)
    first = resync_lipsync_moments(plan_)
    reordered = first.model_copy(
        update={
            "selected_media_ids": ["B", "A"],
            "story_timeline": [
                first.story_timeline[1].model_copy(update={"output_start_s": 0, "output_end_s": 5}),
                first.story_timeline[0].model_copy(
                    update={"output_start_s": 5, "output_end_s": 10}
                ),
            ],
        }
    )
    fixed = resync_lipsync_moments(reordered)
    song = fixed.user_song
    for moment in fixed.story_timeline:
        assert (
            lipsync_sync_error_s(
                output_start_s=moment.output_start_s,
                source_start_s=moment.source_start_s,
                delta_s=song.takes[moment.media_id].delta_s,
                window_start_s=song.window_start_s,
            )
            <= 0.001
        )
    by_id = {m.media_id: m for m in fixed.story_timeline}
    assert by_id["B"].source_start_s == pytest.approx(8.0)  # 0 + 20 - 12
    assert by_id["A"].source_start_s == pytest.approx(15.0)  # 5 + 20 - 10
    assert by_id["A"].source_end_s - by_id["A"].source_start_s == pytest.approx(5.0)


def test_resync_refuses_a_move_that_would_start_before_the_take_began():
    plan_ = _manual_plan(delta_a=10, delta_b=40)  # B starts 20 s after the window opens
    with pytest.raises(LipsyncSyncError, match="before it was filmed"):
        resync_lipsync_moments(plan_)


def test_resync_accepts_the_dict_form_and_ignores_plans_without_a_lipsync_song():
    plan_ = _manual_plan(delta_a=10, delta_b=12)
    as_dict = resync_lipsync_moments(plan_.model_dump(mode="json"))
    assert as_dict["story_timeline"][0]["source_start_s"] == pytest.approx(10.0)
    assert resync_lipsync_moments(plan_.model_copy(update={"user_song": None})).user_song is None
    background = plan_.model_copy(
        update={"user_song": plan_.user_song.model_copy(update={"mode": "background"})}
    )
    assert resync_lipsync_moments(background) is background


def test_editor_can_refuse_a_non_unit_rate_on_a_lipsync_moment():
    plan_ = _manual_plan(delta_a=10, delta_b=12)
    refuse_lipsync_rate_change(plan_)
    retimed = plan_.model_copy(
        update={
            "story_timeline": [
                plan_.story_timeline[0].model_copy(update={"playback_rate": 2.0}),
                plan_.story_timeline[1],
            ]
        }
    )
    with pytest.raises(LipsyncSyncError, match="normal speed"):
        refuse_lipsync_rate_change(retimed)
    # B-roll (no pinned delta) may still be retimed.
    song = plan_.user_song.model_copy(update={"takes": {"B": plan_.user_song.takes["B"]}})
    refuse_lipsync_rate_change(song, retimed.story_timeline)


def test_a_stale_alignment_or_unready_song_is_refused():
    clips = [take("A")]
    stale = alignment(confident("A", 10)).model_copy(update={"song_generation": 99})
    with pytest.raises(LipsyncPlanError) as stale_error:
        plan_lipsync_montage(clips, stale, analysis(), plan_item_id=SONG_ITEM_ID)
    assert stale_error.value.code == "stale_alignment"
    pending = analysis().model_copy(update={"status": "pending"})
    with pytest.raises(LipsyncPlanError) as pending_error:
        plan_lipsync_montage(clips, alignment(confident("A", 10)), pending, plan_item_id="x")
    assert pending_error.value.code == "song_not_analyzed"


def test_nothing_placeable_is_an_explicit_error_not_a_guess():
    with pytest.raises(LipsyncPlanError) as error:
        plan([take("X")], [ambiguous("X", 30, 50)])
    assert error.value.code == "no_synced_takes"


def test_a_take_too_short_for_a_segment_is_not_placed():
    with pytest.raises(LipsyncPlanError):
        plan([take("S", 1.5)], [confident("S", 10)])


def test_compile_rejects_a_plan_whose_song_window_disagrees_with_its_duration():
    result = plan([take("A"), take("B")], [confident("A", 10), confident("B", 25)])
    guided = result.guided_edit()
    guided["approved_proposal"]["user_song"]["window_end_s"] += 0.5
    from app.pipeline.guided_story import compile_execution_plan

    with pytest.raises(GuidedStoryError):
        compile_execution_plan(guided, track=None)


@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
def test_the_creators_output_shape_is_pinned_on_the_lipsync_snapshot(orientation):
    """KRI-374 review: lip-sync dropped the creator's shape and inferred one from footage."""
    result = plan_lipsync_montage(
        [take("A"), take("B")],  # portrait footage: the creator's choice must still win
        alignment(confident("A", 10), confident("B", 25)),
        analysis(),
        plan_item_id=SONG_ITEM_ID,
        output_orientation=orientation,
    )
    assert result.snapshot.output_orientation == orientation
    assert result.snapshot.output_orientation_reason == "The creator selected this output format."
    assert result.guided_edit()["approved_proposal"]["output_orientation"] == orientation


def test_no_creator_shape_keeps_the_lipsync_orientation_inferred():
    result = plan_lipsync_montage(
        [take("A"), take("B")],
        alignment(confident("A", 10), confident("B", 25)),
        analysis(),
        plan_item_id=SONG_ITEM_ID,
        output_orientation="square",  # an unknown value is never trusted
    )
    assert result.snapshot.output_orientation == "portrait"
    assert result.snapshot.output_orientation_reason.startswith("Auto-selected")


def test_a_matched_catalog_track_never_lands_beside_a_lipsync_song():
    from app.pipeline.guided_story import compile_execution_plan
    from tests.pipeline.test_unified_montage_song import CATALOG_TRACK

    result = plan([take("A"), take("B")], [confident("A", 10), confident("B", 25)])
    compiled = compile_execution_plan(result.guided_edit(), track=dict(CATALOG_TRACK))
    assert compiled["user_song"]["mode"] == "lipsync"
    assert compiled.get("song_reference") is None
    assert compiled.get("music") is None
