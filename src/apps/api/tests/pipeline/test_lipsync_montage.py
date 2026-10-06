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
    resync_moment_rows,
)
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.schemas.edit_proposal import MAX_PROPOSAL_DURATION_S
from app.schemas.user_song import PlacementCandidate, TakeAlignment, UserSongPlan, UserSongTake
from tests.pipeline.user_song_helpers import (
    SONG_ITEM_ID,
    alignment,
    ambiguous,
    analysis,
    bindings_for,
    compiled_plan,
    confident,
    photo,
    song_bed,
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


def test_a_take_inside_another_takes_coverage_is_never_placed_so_nothing_is_dropped_redundant():
    contained = take("C", 5)  # claims 15-20, fully inside A
    result = plan(
        [take("A"), take("B"), contained],
        [confident("A", 10), confident("B", 25), confident("C", 15)],
    )
    assert [b["media_id"] for b in blocks(result)] == ["A", "B"]
    # Assignment refuses the conflicting claim, so the tiler never meets a
    # redundant take: C is kept as muted B-roll with its assignment reason.
    reasons = {row["media_id"]: row["reason"] for row in result.song_receipt["dropped"]}
    assert reasons == {"C": "kept_as_broll"}
    assert "C" not in result.user_song.takes
    assert "overlapped" not in reasons.values()


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
    assert {"media_id": "A", "reason": "gap_unfillable"} in result.song_receipt["dropped"]
    assert result.song_receipt["placed_outside_ids"] == ["A"]


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


def test_ambiguous_takes_are_placed_by_likelihood_unmatched_ones_stay_broll():
    clips = [take("A", 30), take("X", 20), take("U", 20)]
    rows = [confident("A", 10), ambiguous("X", 60, 80), unmatched("U")]
    result = plan(clips, rows)
    # No status gates placement: X is placed at its candidate nearest A. Nothing
    # connects it to A, so the likelihood-weighted best span is A and X rides as
    # unused media with a named reason (never silently lost).
    assert [b["media_id"] for b in blocks(result)] == ["A"]
    assert result.song_receipt["placed_outside_ids"] == ["X"]
    reasons = {row["media_id"]: row["reason"] for row in result.song_receipt["dropped"]}
    assert reasons == {"X": "gap_unfillable", "U": "kept_as_broll"}
    assert result.song_receipt["kept_broll_ids"] == ["U"]
    assert [c.media_id for c in result.snapshot.fast_cuts] == ["A", "U"]
    assert {ref.media_id for ref in result.snapshot.media} == {"A", "X", "U"}
    assert set(result.user_song.takes) == {"A"}


def test_a_placed_take_is_never_reused_as_filler():
    clips = [take("A", 12), take("B", 20), take("X", 12)]
    rows = [confident("A", 10), confident("B", 30), ambiguous("X", 60, 80)]
    result = plan(clips, rows)
    # A->B has a gap only X could fill, but X is a placed take (at 60): it plays in
    # sync at its own delta only, so it is never muted filler between A and B.
    assert "X" not in result.song_receipt["broll_ids"]
    in_sync_ids = set(result.user_song.takes)
    for cut in result.snapshot.fast_cuts:
        if cut.media_id == "X":
            assert "X" in in_sync_ids


def test_creator_confirmed_order_resolves_an_ambiguous_take_to_its_fitting_alternate():
    clips = [take("A", 30), take("X", 20), take("B", 30)]
    # X's best guess (60) sits after B; the creator says A, X, B so the alternate
    # at 35 (between A's 10.3 and B's 50.3) is the one that fits.
    rows = [confident("A", 10), ambiguous("X", 60, 35, 80), confident("B", 50)]
    result = plan(clips, rows, order=["A", "X", "B"])
    assert [b["media_id"] for b in blocks(result)] == ["A", "X", "B"]
    pinned = result.user_song.takes["X"]
    assert pinned.delta_s == 35 and pinned.confirmed_by_creator is True
    assert pinned.position_basis == "creator_position"
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
    # U is never pinned to song time, but it still plays (muted) in the A->B gap.
    assert "U" in result.song_receipt["broll_ids"]
    assert "U" in {ref.media_id for ref in result.snapshot.media}


def test_a_take_missing_from_the_confirmed_order_is_still_placed_by_likelihood():
    clips = [take("A", 30), take("X", 20), take("B", 30)]
    rows = [confident("A", 10), ambiguous("X", 35), confident("B", 50)]
    result = plan(clips, rows, order=["A", "B"])
    assert result.user_song.takes["X"].confirmed_by_creator is False
    assert result.user_song.takes["X"].delta_s == 35


def test_every_take_with_a_candidate_is_placed_and_tiles_without_redundant_drops():
    clips = [take(f"T{i}", 14) for i in range(5)]
    rows = [confident("T0", 10), ambiguous("T1", 22), confident("T2", 33)]
    rows += [ambiguous("T3", 44, 90), confident("T4", 55)]
    result = plan(clips, rows)
    assert [b["media_id"] for b in blocks(result)] == [f"T{i}" for i in range(5)]
    assert set(result.user_song.takes) == {f"T{i}" for i in range(5)}
    assert all(r["reason"] != "overlapped" for r in result.song_receipt["dropped"])
    assert result.song_receipt["placed_outside_ids"] == []
    assert_in_sync(result, compiled_plan(result))


def test_own_footage_bridge_cuts_on_a_lyric_line_inside_the_gap():
    # A matched 0-12 s (song 10.3-22), B matched from 3 s in (song 33-45); both
    # have spare footage, so the 11 s gap closes with their own frames. Lines start
    # every 4 s from 2.0: 30.0 is the one inside the reachable cut range.
    clips = [take("A", 25), take("B", 20)]
    rows = [lyric_row("A", 10, 0, 12), lyric_row("B", 29, 3, 15)]
    result = plan(clips, rows)
    assert result.song_receipt["bridged_gaps"] == 1
    a, b = blocks(result)
    assert a["song_end_s"] == b["song_start_s"]
    assert result.song_receipt["switches"][-1]["snapped_to"] == "line"
    assert a["song_end_s"] % 4 == pytest.approx(2.0)
    assert_in_sync(result, compiled_plan(result))


def test_a_gap_beyond_own_footage_is_filled_from_visuals_not_a_placed_take():
    pics = [photo(f"2222222{i}-2222-4222-8222-222222222222") for i in range(3)]
    clips = [take("A", 12), take("B", 20), *pics]
    result = plan(clips, [confident("A", 10), confident("B", 30)])
    ids = [c.media_id for c in result.snapshot.fast_cuts]
    assert ids[0] == "A" and ids[-1] == "B" and {p.media_id for p in pics} <= set(ids)
    assert result.song_receipt["placed_outside_ids"] == []


def test_irreducible_gap_keeps_the_best_likelihood_span_and_names_the_rest():
    # A is a sure take; B is a weak long guess far away. The weighted span picks A
    # even though B covers more seconds.
    weak = TakeAlignment(
        media_id="B",
        status="ambiguous",
        delta_s=60,
        confidence=0.2,
        candidates=[PlacementCandidate(delta_s=60, likelihood=0.2, method="lyrics")],
    )
    clips = [take("A", 14), take("B", 30)]
    result = plan(clips, [confident("A", 10), weak])
    assert [b["media_id"] for b in blocks(result)] == ["A"]
    assert result.song_receipt["placed_outside_ids"] == ["B"]
    assert {"media_id": "B", "reason": "gap_unfillable"} in result.song_receipt["dropped"]
    assert "B" in {ref.media_id for ref in result.snapshot.media}  # still editable
    assert "B" not in result.song_receipt["broll_ids"]


def test_receipt_carries_likelihood_margin_basis_and_low_confidence_ids():
    tied = TakeAlignment(
        media_id="X",
        status="ambiguous",
        delta_s=28,
        confidence=0.6,
        candidates=[
            PlacementCandidate(delta_s=28, likelihood=0.6, method="lyrics"),
            PlacementCandidate(delta_s=95, likelihood=0.6, method="lyrics"),
        ],
        margin=0.0,
        likelihood=0.6,
    )
    result = plan([take("A", 20), take("X", 20)], [confident("A", 10), tied])
    rows = {b["media_id"]: b for b in blocks(result)}
    assert rows["A"]["position_basis"] == "aligner" and rows["A"]["likelihood"] == 0.95
    assert rows["X"]["position_basis"] == "tie_break" and rows["X"]["margin"] == 0
    assert rows["X"]["confirmed_by_creator"] is False
    assert result.song_receipt["low_confidence_ids"] == ["X"]
    assert result.user_song.takes["X"].position_basis == "tie_break"
    assert result.user_song.takes["X"].likelihood == pytest.approx(0.6)
    assert result.user_song.takes["X"].delta_s == 28  # inside the cluster, not 95


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
        plan([take("X")], [unmatched("X")])
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


# ── KRI-466: trimmed takes, kept B-roll, editable unused takes ───────────────


def lyric_row(media_id, delta_s, start_s, end_s, method="lyrics"):
    return TakeAlignment(
        media_id=media_id,
        status="confident",
        delta_s=delta_s,
        confidence=0.7,
        method=method,
        match_start_s=start_s,
        match_end_s=end_s,
    )


def test_take_is_trimmed_to_its_match_but_the_full_source_is_kept():
    result = plan([take("A", 30)], [lyric_row("A", 10, 8, 20)])
    (cut,) = result.snapshot.fast_cuts
    # song 18-30 -> source 8-20 (block - delta), not the whole 0.3-29.7.
    assert (cut.source_start_s, cut.source_end_s) == (8.0, 20.0)
    assert result.user_song.window_start_s == pytest.approx(18.0)
    assert result.user_song.window_end_s == pytest.approx(30.0)
    (ref,) = result.snapshot.media
    assert ref.duration_s == 30  # full clip, so the editor can extend it
    assert result.snapshot.media_scope == "all"


def test_match_range_keeps_the_cover_margins():
    # Match starts 0.1 s into the take and ends 0.1 s before its end.
    result = plan([take("A", 20)], [lyric_row("A", 10, 0.1, 19.9)])
    (cut,) = result.snapshot.fast_cuts
    assert cut.source_start_s == pytest.approx(0.3)
    assert cut.source_end_s == pytest.approx(19.7)


def test_trimmed_tail_extended_in_the_editor_stays_in_sync():
    result = plan([take("A", 30)], [lyric_row("A", 10, 8, 20)])
    footage, visuals = bindings_for(result)
    guided = compiled_plan(result)
    recipe = compile_phone_guided_plan(guided, footage, visuals, song=song_bed())
    assert recipe.duration == pytest.approx(12.0)
    # The editor drags the tail out by 6 s into the take's own unmatched footage.
    rows = [m.model_dump() for m in guided.story_timeline]
    rows[0]["duration_s"] += 6
    rows[0]["output_end_s"] += 6
    fixed = resync_moment_rows(rows, result.user_song, source_durations={"A": 30})
    assert fixed[0]["source_start_s"] == pytest.approx(8.0)
    assert fixed[0]["source_end_s"] == pytest.approx(26.0)
    # Past the take's real end it is refused instead of silently desyncing.
    rows[0]["duration_s"] += 10
    with pytest.raises(LipsyncSyncError, match="past its end"):
        resync_moment_rows(rows, result.user_song, source_durations={"A": 30})


def test_a_gap_is_closed_with_the_takes_own_footage_before_any_broll():
    # A is matched 0-12 s of a 25 s take (song 10.3-22), B matched from 3 s into its
    # take (song 33-45): a 11 s gap, but A has 12.7 s and B 2.7 s of spare footage.
    clips = [take("A", 25), take("B", 20), take("U", 10)]
    rows = [lyric_row("A", 10, 0, 12), lyric_row("B", 30, 3, 15), unmatched("U")]
    result = plan(clips, rows)
    assert result.song_receipt["bridged_gaps"] == 1
    assert [c.media_id for c in result.snapshot.fast_cuts[:2]] == ["A", "B"]
    first, second = result.snapshot.fast_cuts[:2]
    assert first.source_end_s <= 25 - 0.3 + 1e-6 and second.source_start_s >= 0.3 - 1e-6
    # No muted B-roll sits between them; U is only kept after the last take.
    assert result.song_receipt["kept_broll_ids"] == ["U"]
    assert [c.media_id for c in result.snapshot.fast_cuts] == ["A", "B", "U"]
    assert_in_sync(result, compiled_plan(result))


def test_unplaced_takes_become_short_muted_broll_after_the_last_sung_take():
    clips = [take("A", 30), take("U1", 20), take("U2", 2.0), take("U3", 20)]
    rows = [confident("A", 10), unmatched("U1"), unmatched("U2"), unmatched("U3")]
    result = plan(clips, rows)
    assert result.song_receipt["kept_broll_ids"] == ["U1", "U2", "U3"]
    cuts = result.snapshot.fast_cuts
    assert [c.media_id for c in cuts] == ["A", "U1", "U2", "U3"]
    assert cuts[1].output_duration_s == pytest.approx(3.0)  # BROLL_HOLD
    assert cuts[2].output_duration_s == pytest.approx(2.0)  # shorter than the hold
    assert result.user_song.window_end_s == pytest.approx(39.7 + 3.0 + 2.0 + 3.0)
    assert set(result.user_song.takes) == {"A"}
    assert_in_sync(result, compiled_plan(result))


def test_kept_broll_never_runs_past_the_song_end():
    song = analysis(duration_s=40.0)
    result = plan(
        [take("A", 30), take("U1", 20), take("U2", 20)],
        [confident("A", 10), unmatched("U1"), unmatched("U2")],
        song=song,
    )
    # A covers 10.3-39.7: only 0.3 s of song is left, below a B-roll piece.
    assert result.song_receipt["kept_broll_ids"] == []
    assert result.user_song.window_end_s <= 40.0
    # The leftover takes are still editable media.
    assert {"U1", "U2"} <= {ref.media_id for ref in result.snapshot.media}
    reasons = {r["media_id"]: r["reason"] for r in result.song_receipt["dropped"]}
    assert reasons == {"U1": "no_evidence", "U2": "no_evidence"}


def test_kept_broll_stays_inside_the_120_second_cap():
    song = analysis(duration_s=300.0)
    clips = [take("A", 119.0), take("U", 20)]
    result = plan(clips, [confident("A", 10), unmatched("U")], song=song)
    window = result.user_song.window_end_s - result.user_song.window_start_s
    assert window <= MAX_PROPOSAL_DURATION_S + 1e-6
    assert result.snapshot.duration_s <= MAX_PROPOSAL_DURATION_S + 1e-6


def test_every_unplaced_take_is_editable_unused_media_and_compiles():
    # A and B sit far apart and nothing fills the gap, so the weaker side rides
    # along as unused media (placed_outside), as does a conflicting take.
    song = analysis(duration_s=100.0)
    clips = [take("A", 30), take("B", 20), take("C", 5), take("X", 20)]
    result = plan(
        clips,
        [confident("A", 10), confident("B", 70), confident("C", 15), unmatched("X")],
        song=song,
    )
    cut_ids = {c.media_id for c in result.snapshot.fast_cuts}
    media_ids = {ref.media_id for ref in result.snapshot.media}
    assert media_ids == {"A", "B", "C", "X"}
    unused = media_ids - cut_ids
    assert unused  # at least B (outside the span) or C (conflict) is not in a cut
    assert set(result.clip_ids) == cut_ids  # used media only
    assert set(result.snapshot.selected_media_ids) == cut_ids
    assert result.snapshot.media_scope == "selected"
    # The strict compiler still accepts the plan.
    compiled = compiled_plan(result)
    assert {m.media_id for m in compiled.story_timeline} == cut_ids


def test_lyric_placed_take_compiles_on_the_song_clock():
    clips = [take("A", 30), take("B", 30)]
    rows = [lyric_row("A", 10.0, 2, 14), lyric_row("B", 21.0, 5, 20, method="audio")]
    result = plan(clips, rows)
    assert set(result.user_song.takes) == {"A", "B"}
    compiled = compiled_plan(result)
    assert_in_sync(result, compiled)
    footage, visuals = bindings_for(result)
    recipe = compile_phone_guided_plan(compiled, footage, visuals, song=song_bed())
    video = next(t for t in recipe.tracks if t.kind == "video")
    song_plan = result.user_song
    cut_media = {cut.cut_id: cut.media_id for cut in result.snapshot.fast_cuts}
    for clip in video.clips:
        pinned = song_plan.takes[cut_media[clip.id]]
        assert clip.source_start - clip.timeline_start == pytest.approx(
            song_plan.window_start_s - pinned.delta_s, abs=0.001
        )


def _stack_choice(delta_s: float) -> dict:
    return {
        "S": {
            "delta_s": delta_s,
            "place": "stack",
            "position_basis": "creator_stack",
            "confirmed_by_creator": True,
        }
    }


def test_a_stacked_take_that_splits_the_montage_is_retried_as_gap_filler():
    # A covers 10.3-21.7, B 34.3-45.7 (a 12.6 s hole); S is a guessed (stacked) take
    # far away. As a pinned take it strands A and B; as muted B-roll it fills the hole.
    result = plan_lipsync_montage(
        [take("A", 12), take("B", 12), take("S", 15)],
        alignment(confident("A", 10), confident("B", 34), unmatched("S")),
        analysis(),
        plan_item_id=SONG_ITEM_ID,
        creator_choices=_stack_choice(80),
    )
    assert [b["media_id"] for b in blocks(result)] == ["A", "B"]
    assert result.song_receipt["broll_ids"] == ["S"]
    assert result.song_receipt["placed_outside_ids"] == []
    assert set(result.user_song.takes) == {"A", "B"}
    assert result.user_song.window_start_s == pytest.approx(10.3)
    assert result.user_song.window_end_s == pytest.approx(45.7)
    assert_in_sync(result, compiled_plan(result))


def test_the_stack_retry_is_dropped_when_it_does_not_help():
    # Nothing can fill the hole (S is only 3 s): the original layout stands and S
    # keeps its stacked position.
    result = plan_lipsync_montage(
        [take("A", 12), take("B", 12), take("S", 3)],
        alignment(confident("A", 10), confident("B", 34), unmatched("S")),
        analysis(),
        plan_item_id=SONG_ITEM_ID,
        creator_choices=_stack_choice(80),
    )
    assert "S" not in result.song_receipt["broll_ids"]
    reasons = {row["media_id"]: row["reason"] for row in result.song_receipt["dropped"]}
    assert "stack_as_broll" not in reasons.values()
