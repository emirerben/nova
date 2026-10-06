"""KRI-374 D2: the whole backend path for a creator-uploaded song, no network.

synthetic song PCM -> synthetic takes cut from it (known offsets, one ambiguous
because its section repeats) -> the REAL aligner -> the creator's confirmed order
(real ``resolve_uncertain_takes`` + the worker's ``apply_resolved_song_takes``) ->
``plan_lipsync_montage`` / ``plan_unified_montage`` -> the strict guided compiler ->
``compile_phone_guided_plan`` with a ``PhoneSongBed`` -> ``validate_phone_pilot_recipe``
under the production verified-feature profile.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.config import settings
from app.pipeline.lipsync_montage import plan_lipsync_montage
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.song_alignment import align_takes
from app.pipeline.unified_montage import plan_unified_montage
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.song_order import (
    apply_resolved_song_takes,
    resolve_uncertain_takes,
    resolved_song_takes_payload,
)
from tests._prod_profile import PROD_VERIFIED_FEATURES
from tests.pipeline.test_song_alignment import SR, make_song, slice_take
from tests.pipeline.user_song_helpers import (
    SONG_GENERATION,
    SONG_ITEM_ID,
    analysis,
    bindings_for,
    compiled_plan,
    song_bed,
    take,
)

SONG_S = 90.0
# (media_id, start in the song, length): A and B are unique material (confident);
# C lies wholly inside the section that repeats at +20 s, so it matches twice.
TAKES = {"A": (5.0, 19.0), "B": (31.9, 30.0), "C": (21.0, 11.0)}


@pytest.fixture(scope="module")
def song() -> np.ndarray:
    pcm = make_song(SONG_S, seed=5)
    a, b = int(20 * SR), int(40 * SR)
    pcm[b : b + 12 * SR] = pcm[a : a + 12 * SR]  # the "chorus" comes round again
    return pcm


@pytest.fixture(scope="module")
def aligned(song):
    takes = {
        media_id: (slice_take(song, start, length), [], 1)
        for media_id, (start, length) in TAKES.items()
    }
    return align_takes(song, takes, [], SONG_GENERATION)


def _clips():
    return [take(media_id, TAKES[media_id][1]) for media_id in ("A", "B", "C")]


def _song_analysis():
    return analysis(duration_s=SONG_S, line_starts=tuple(float(i) for i in range(2, 88, 4)))


@pytest.fixture
def prod_profile(monkeypatch):
    monkeypatch.setattr(settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))


def test_the_real_aligner_places_unique_takes_and_flags_the_repeated_one(aligned):
    a, b, c = (aligned.takes[m] for m in "ABC")
    assert a.status == b.status == "confident"
    assert a.delta_s == pytest.approx(5.0, abs=0.002)
    assert b.delta_s == pytest.approx(31.9, abs=0.002)
    assert c.status == "ambiguous"
    found = [c.delta_s, *(alt.delta_s for alt in c.alternates)]
    assert any(abs(d - 21.0) < 0.005 for d in found)
    assert any(abs(d - 41.0) < 0.005 for d in found)


def test_an_unconfirmed_ambiguous_take_is_placed_by_likelihood_and_flagged_not_confirmed(aligned):
    result = plan_lipsync_montage(
        _clips(), aligned, _song_analysis(), plan_item_id=SONG_ITEM_ID, confirmed_order=None
    )
    # No status gate (KRI-471): C is placed at its best candidate, but never marked
    # as confirmed by the creator.
    assert set(result.user_song.takes) == {"A", "B", "C"}
    c = result.user_song.takes["C"]
    assert c.confirmed_by_creator is False
    assert c.position_basis == "tie_break"
    # The tie is broken toward the cluster: the repeat that fits between A and B,
    # not the one that would sit inside B's coverage.
    assert c.delta_s == pytest.approx(21.0, abs=0.01)
    assert "C" in result.song_receipt["low_confidence_ids"]


def test_lipsync_recipe_from_real_alignments_keeps_every_take_on_the_song_clock(
    aligned, prod_profile
):
    # The creator confirms the order A, C, B; the gate resolves C against its alternates.
    resolved = resolve_uncertain_takes(aligned, ["A", "C", "B"])
    payload = resolved_song_takes_payload(resolved)
    assert resolved["C"]["confirmed_by_creator"] is True
    assert resolved["C"]["delta_s"] == pytest.approx(21.0, abs=0.01)  # 41.0 is past B

    # What the worker does with that payload before planning.
    patched, order, choices = apply_resolved_song_takes(aligned, payload)
    assert patched is aligned  # rows are never rewritten any more
    assert order == ["A", "C", "B"]
    assert choices["C"]["position_basis"] == "creator_position"
    result = plan_lipsync_montage(
        _clips(),
        patched,
        _song_analysis(),
        plan_item_id=SONG_ITEM_ID,
        confirmed_order=order,
        creator_choices=choices,
    )
    song_plan = result.user_song
    assert set(song_plan.takes) == {"A", "B", "C"}
    assert song_plan.takes["C"].confirmed_by_creator
    assert song_plan.takes["C"].position_basis == "creator_position"
    assert song_plan.takes["A"].delta_s == pytest.approx(5.0, abs=0.002)

    footage, visuals = bindings_for(result)
    recipe = compile_phone_guided_plan(
        compiled_plan(result), footage, visuals, song=song_bed(duration_s=SONG_S)
    )
    validate_phone_pilot_recipe(recipe)  # PROD_VERIFIED_FEATURES: musicBed + audioMix

    # The song is the soundtrack: starts at the window, camera audio muted.
    track = next(t for t in recipe.tracks if t.id == "song")
    (bed,) = track.clips
    assert bed.source_start == pytest.approx(song_plan.window_start_s)
    assert bed.timeline_start == 0 and bed.rate == 1
    assert recipe.audio.original_volume == 0.0
    assert recipe.audio.music_asset_id == bed.source_asset_id

    # The one invariant that matters: source_start - timeline_start == window - delta.
    video = next(t for t in recipe.tracks if t.kind == "video")
    cut_media = {cut.cut_id: cut.media_id for cut in result.snapshot.fast_cuts}
    checked = 0
    for clip in video.clips:
        media_id = cut_media.get(clip.id)
        if media_id not in song_plan.takes:
            continue
        pinned = song_plan.takes[media_id]
        offset = clip.source_start - clip.timeline_start
        assert offset == pytest.approx(song_plan.window_start_s - pinned.delta_s, abs=0.001)
        checked += 1
    assert checked == 3
    assert recipe.duration == pytest.approx(song_plan.window_end_s - song_plan.window_start_s)


def test_background_song_recipe_cuts_on_the_beat_and_mutes_the_camera(prod_profile):
    info = _song_analysis()
    clips = [take(f"c{i}", 6.0) for i in range(1, 7)]
    result = plan_unified_montage(
        clips,
        song_beats=info.beats_s,
        song_lines=info.lines,
        song_duration_s=SONG_S,
        song_plan_item_id=SONG_ITEM_ID,
        song_generation=SONG_GENERATION,
    )
    song_plan = result.user_song
    assert song_plan.mode == "background"
    footage, visuals = bindings_for(result)
    recipe = compile_phone_guided_plan(
        compiled_plan(result), footage, visuals, song=song_bed(duration_s=SONG_S)
    )
    validate_phone_pilot_recipe(recipe)

    (bed,) = next(t for t in recipe.tracks if t.id == "song").clips
    assert bed.source_start == pytest.approx(song_plan.window_start_s)
    assert (bed.audio_fade_in, bed.audio_fade_out) == (0.5, 0.5)
    assert recipe.audio.original_volume == 0.0

    video = next(t for t in recipe.tracks if t.kind == "video")
    boundaries = [c.timeline_start + song_plan.window_start_s for c in video.clips[1:]]
    beats = np.asarray(info.beats_s)
    on_beat = [b for b in boundaries if np.min(np.abs(beats - b)) <= 0.04]
    assert on_beat, "no internal cut landed on a beat"
    assert len(on_beat) >= result.song_receipt["beat_aligned_cuts"]
    assert result.snapshot.duration_s <= SONG_S  # never longer than the song
