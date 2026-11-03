"""Insert clips in a phone Talking edit (parent KRI-1151).

An insert switches the picture to another clip, with that clip's own audio, while the
creator's take is PAUSED, then the take resumes where it stopped. Nothing he said is lost
and the video grows by the insert's length.

Failure-mode list (each one is a test below). Rule from the creator: an explicit instruction
wins over every heuristic; the heuristics only choose a position he did not name.

 1. an explicit second / phrase in the hook window or at the very start    -> honoured, receipt
    says it overrode the hook default
 2. an explicit second / phrase in the last second or at the very end      -> honoured
 3. an explicit second in the middle of a word                             -> cut there, as asked
 4. two explicit inserts next to each other, or at the same instant        -> both play, in order
 5. a fourth (or twentieth) insert                                         -> played; past the
    engine's MAX_INSERTS the rest are refused with that number
 6. a 70 s clip                                                            -> played; refused only
    past the engine's five minutes
 7. a second past the end of the take                                      -> the end of the take,
    said in the receipt
 8. the phrase is never said                                               -> refused here (the
    worker places it by meaning and says so, see test_talking_insert_topic.py)
 9. the same clip asked twice / a clip already in use / not a video        -> refused by name
10. a default (topic) position near another clip                           -> joins it
11. a caption / card / cutaway straddles the insert point                  -> split around it, never
    shown over the inserted clip
12. a second insert comes after a first                                    -> its position already
    includes the first's length (no double or missing shift)
13. the recompile of a pinned recipe (editor Save)                         -> the insert comes back
    as it was
14. Range after an insert                                                  -> refused with a named
    reason
15. flag off                                                               -> byte-identical recipe
"""

from __future__ import annotations

import pytest

from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_subtitled_lanes import (
    PhoneSubtitledLanes,
    SubtitledOverlayCard,
    SubtitledSoundEffect,
)
from app.pipeline.phone_subtitled_plan import (
    PhoneCutaway,
    PhoneInsert,
    additional_speakers_from_recipe,
    compile_phone_subtitled_plan,
    speaker_binding_from_recipe,
)
from app.pipeline.phone_talking_inserts import (
    ADJUSTED_JOINED,
    ADJUSTED_TAKE_END,
    MAX_INSERT_S,
    MAX_INSERTS,
    MIN_GAP_S,
    OVERRODE_HOOK,
    OVERRODE_MID_WORD,
    OVERRODE_NEAR_OTHER,
    OVERRODE_TAIL,
    TAIL_FREE_S,
    InsertAsk,
    insert_blocked_windows,
    inserts_from_recipe,
    phrase_end_s,
    place_inserts,
    shift_time,
    warp_for_inserts,
)
from tests.pipeline.test_phone_subtitled_plan import _binding, _resolved_sfx


def _words(spec):
    return [{"text": t, "start_s": s, "end_s": e} for t, s, e in spec]


# A 30 s take: two sentences with a clear pause after each.
TAKE_WORDS = _words(
    [
        ("Merhaba", 0.2, 0.8),
        ("ben", 0.9, 1.1),
        ("Emir", 1.2, 1.6),
        ("bugün", 3.0, 3.4),
        ("kongreyi", 3.5, 4.2),
        ("anlatacağım", 4.3, 5.2),
        ("Ortada", 8.0, 8.5),
        ("henüz", 8.6, 9.0),
        ("bir", 9.1, 9.3),
        ("karar", 9.4, 9.9),
        ("yok", 10.0, 10.4),
        ("Sonra", 14.0, 14.4),
        ("kulüp", 14.5, 15.0),
        ("açıkladı", 15.1, 15.8),
        ("Bitti", 25.0, 25.6),
    ]
)
TAKE_CUES = [
    {"text": "Merhaba ben Emir", "start_s": 0.2, "end_s": 1.6, "words": TAKE_WORDS[0:3]},
    {
        "text": "bugün kongreyi anlatacağım",
        "start_s": 3.0,
        "end_s": 5.2,
        "words": TAKE_WORDS[3:6],
    },
    {
        "text": "Ortada henüz bir karar yok",
        "start_s": 8.0,
        "end_s": 10.4,
        "words": TAKE_WORDS[6:11],
    },
    {"text": "Sonra kulüp açıkladı", "start_s": 14.0, "end_s": 15.8, "words": TAKE_WORDS[11:14]},
    {"text": "Bitti", "start_s": 25.0, "end_s": 25.6, "words": TAKE_WORDS[14:]},
]
TAKE_S = 30.0


def _ask(media_id="talk", *, phrase=None, at_s=None, keep_audio=True):
    return InsertAsk(media_id=media_id, at_phrase=phrase, at_s=at_s, keep_audio=keep_audio)


def _place(asks, *, bindings=None, in_use=(), words=TAKE_WORDS, take_s=TAKE_S, **extra):
    bindings = bindings if bindings is not None else {"talk": _binding("talk", duration_s=8.0)}
    return place_inserts(
        asks, bindings=bindings, words=words, take_s=take_s, in_use=in_use, **extra
    )


# --- placement ------------------------------------------------------------------------------


def test_a_phrase_places_the_insert_exactly_at_the_end_of_its_last_word():
    (placed,) = _place([_ask(phrase="kongreyi anlatacağım")])
    assert placed.reason is None
    assert placed.insert is not None
    # "anlatacağım" ends 5.2: the take pauses exactly there.
    assert placed.insert.at_take_s == pytest.approx(5.2)
    assert (placed.basis, placed.snap, placed.overrode) == ("phrase", "phrase_end", ())


def test_a_phrase_in_the_middle_of_a_sentence_is_not_moved_to_a_later_pause_or_caption_end():
    # "henüz bir" ends at 9.3, mid-sentence; the caption ends at 10.4. The creator named the
    # words, so the take pauses at the end of "bir".
    (placed,) = _place([_ask(phrase="henüz bir")])
    assert placed.insert is not None
    assert placed.insert.at_take_s == pytest.approx(9.3)
    assert not any(w["start_s"] < 9.3 < w["end_s"] for w in TAKE_WORDS)


def test_a_second_in_the_middle_of_a_word_cuts_there_as_asked_and_says_so():
    (placed,) = _place([_ask(at_s=4.0)])  # inside "kongreyi" 3.5-4.2
    assert placed.insert is not None
    assert placed.insert.at_take_s == pytest.approx(4.0)
    assert (placed.basis, placed.snap) == ("second", "exact")
    assert placed.overrode == (OVERRODE_MID_WORD,)


def test_a_phrase_that_is_never_said_is_refused_here_the_worker_places_it_by_meaning():
    (placed,) = _place([_ask(phrase="bu hiç söylenmedi")])
    assert placed.insert is None
    assert placed.reason == "phrase_not_found"
    assert phrase_end_s("bu hiç söylenmedi", TAKE_WORDS, TAKE_S) is None


def test_a_phrase_in_a_cut_out_region_is_not_found_like_one_never_said():
    cut_time_words = [w for w in TAKE_WORDS if w["text"] != "kongreyi"]
    (placed,) = _place([_ask(phrase="kongreyi")], words=cut_time_words)
    assert placed.insert is None
    assert placed.reason == "phrase_not_found"


def test_an_explicit_second_in_the_first_second_or_at_the_very_start_is_honoured():
    for at in (0.0, 0.85, 2.0):
        (placed,) = _place([_ask(at_s=at)])
        assert placed.insert is not None, at
        assert placed.insert.at_take_s == pytest.approx(at)
        assert placed.overrode == (OVERRODE_HOOK,)
        assert placed.adjusted is None


def test_an_explicit_second_in_the_last_second_or_at_the_very_end_is_honoured():
    for at in (TAKE_S - TAIL_FREE_S + 0.1, TAKE_S):
        (placed,) = _place([_ask(at_s=at)])
        assert placed.insert is not None, at
        assert placed.insert.at_take_s == pytest.approx(at)
        assert placed.overrode == (OVERRODE_TAIL,)


def test_a_second_past_the_end_of_the_take_plays_at_its_end_and_says_what_moved():
    (placed,) = _place([_ask(at_s=TAKE_S + 12.0)])
    assert placed.insert is not None
    assert placed.insert.at_take_s == pytest.approx(TAKE_S)
    assert placed.adjusted == ADJUSTED_TAKE_END


def test_a_seventy_second_clip_is_played_not_refused():
    long = {"talk": _binding("talk", duration_s=70.0)}
    (placed,) = _place([_ask(at_s=10.0)], bindings=long)
    assert placed.reason is None and placed.insert is not None


def test_only_a_clip_over_the_engines_five_minutes_is_refused():
    long = {"talk": _binding("talk", duration_s=MAX_INSERT_S + 1)}
    (placed,) = _place([_ask(at_s=10.0)], bindings=long)
    assert (placed.insert, placed.reason) == (None, "too_long")


def test_the_same_clip_twice_places_once():
    first, second = _place([_ask(at_s=6.0), _ask(at_s=16.0)])
    assert first.insert is not None
    assert second.insert is None
    assert second.reason == "duplicate_clip"


def test_a_clip_already_used_as_speaker_cutaway_or_ending_is_refused():
    (placed,) = _place([_ask(at_s=6.0)], in_use={"talk"})
    assert placed.insert is None
    assert placed.reason == "clip_in_use"


def test_explicit_inserts_next_to_each_other_or_at_the_same_instant_all_play_in_order():
    bindings = {n: _binding(n, duration_s=4.0) for n in "abc"}
    near, same_as_near, apart = _place(
        [_ask("a", at_s=5.2), _ask("b", at_s=5.2 + 0.5), _ask("c", at_s=5.2 + 0.5)],
        bindings=bindings,
    )
    assert near.insert is not None and near.overrode == ()
    assert same_as_near.insert is not None
    assert same_as_near.overrode == (OVERRODE_NEAR_OTHER,)
    assert apart.insert is not None
    assert apart.insert.at_take_s == pytest.approx(same_as_near.insert.at_take_s)


def test_a_fourth_insert_and_a_twentieth_are_played_as_asked():
    ids = [f"c{i}" for i in range(MAX_INSERTS)]
    bindings = {i: _binding(i, duration_s=1.0) for i in ids}
    ats = [3.0 + 1.2 * n for n in range(len(ids))]
    placed = _place([_ask(i, at_s=a) for i, a in zip(ids, ats, strict=True)], bindings=bindings)
    assert all(p.insert is not None for p in placed)
    assert len(placed) == 20 and len(placed) > 3


def test_past_the_engine_limit_the_rest_are_refused_with_that_reason():
    ids = [f"c{i}" for i in range(MAX_INSERTS + 1)]
    bindings = {i: _binding(i, duration_s=1.0) for i in ids}
    placed = _place(
        [_ask(i, at_s=3.0 + n) for n, i in enumerate(ids)], bindings=bindings, take_s=60.0
    )
    assert placed[-1].reason == "too_many"
    assert all(p.insert is not None for p in placed[:-1])


def test_a_point_chosen_by_meaning_near_another_clip_joins_it_instead_of_leaving_a_sliver():
    bindings = {"a": _binding("a", duration_s=4.0), "b": _binding("b", duration_s=4.0)}
    first = InsertAsk("a", at_s=10.4)
    second = InsertAsk("b", at_s=10.4 + MIN_GAP_S - 0.5, basis="topic")
    placed_a, placed_b = _place([first, second], bindings=bindings)
    assert placed_b.insert is not None
    assert placed_b.insert.at_take_s == pytest.approx(10.4)
    assert (placed_b.adjusted, placed_b.overrode) == (ADJUSTED_JOINED, ())


def test_an_ask_with_no_position_or_no_such_clip_is_refused_with_a_reason():
    nothing, missing = _place([_ask(), _ask("ghost", at_s=6.0)])
    assert nothing.reason == "no_position"
    assert missing.reason == "clip_not_found"


def test_a_muted_ask_places_a_muted_insert():
    (placed,) = _place([_ask(at_s=6.0, keep_audio=False)])
    assert placed.insert is not None and placed.insert.keep_audio is False


# --- the time shift ------------------------------------------------------------------------


def _insert(media="talk", at=5.2, dur=8.0, keep_audio=True):
    return PhoneInsert(binding=_binding(media, duration_s=dur), at_take_s=at, keep_audio=keep_audio)


def test_shift_time_moves_only_what_comes_after_the_point():
    ins = (_insert(at=5.2, dur=8.0), _insert("b", at=10.4, dur=3.0))
    assert shift_time(5.1, ins) == pytest.approx(5.1)
    assert shift_time(5.2, ins) == pytest.approx(13.2)
    assert shift_time(10.3, ins) == pytest.approx(18.3)
    assert shift_time(10.4, ins) == pytest.approx(21.4)  # past both inserts


def test_captions_after_the_insert_shift_by_exactly_its_length():
    out = warp_for_inserts((_insert(at=5.2, dur=8.0),), cues=TAKE_CUES)
    before = [c for c in TAKE_CUES if c["end_s"] <= 5.2]
    after = [c for c in TAKE_CUES if c["start_s"] >= 5.2]
    shifted = out.cues
    assert [c for c in shifted if c["start_s"] < 5.2] == before
    moved = [c for c in shifted if c["start_s"] >= 13.2]
    assert [(c["start_s"], c["end_s"]) for c in moved] == [
        (pytest.approx(c["start_s"] + 8.0), pytest.approx(c["end_s"] + 8.0)) for c in after
    ]
    assert [w["start_s"] for w in moved[0]["words"]] == [
        pytest.approx(w["start_s"] + 8.0) for w in after[0]["words"]
    ]


def test_a_caption_that_straddles_the_point_is_split_around_it():
    cue = {
        "text": "bir iki üç dört",
        "start_s": 6.0,
        "end_s": 8.0,
        "words": _words(
            [("bir", 6.0, 6.4), ("iki", 6.5, 6.9), ("üç", 7.0, 7.4), ("dört", 7.5, 8.0)]
        ),
    }
    out = warp_for_inserts((_insert(at=6.95, dur=4.0),), cues=[cue])
    first, second = out.cues
    assert (first["start_s"], first["end_s"]) == (6.0, pytest.approx(6.9))
    assert first["text"] == "bir iki"
    assert second["start_s"] == pytest.approx(7.0 + 4.0)
    assert second["end_s"] == pytest.approx(8.0 + 4.0)
    assert second["text"] == "üç dört"


def test_text_rows_cards_effects_and_cutaways_follow_the_point():
    card = SubtitledOverlayCard(
        id="card-1",
        media_id="photo-1",
        gcs_path="p/photo-1.jpg",
        generation="1",
        start_s=4.0,
        end_s=7.0,
        kind="video",
        source_start_s=1.0,
    )
    lanes = PhoneSubtitledLanes(overlays=[card])
    cutaway = PhoneCutaway(binding=_binding("b-roll", duration_s=6.0), start_s=20.0, end_s=23.0)
    straddling = PhoneCutaway(
        binding=_binding("b-roll2", duration_s=6.0), start_s=4.0, end_s=6.0, source_start_s=0.5
    )
    rows = [{"id": "text-0", "text": "hi", "start_s": 9.0, "end_s": 12.0}]
    out = warp_for_inserts(
        (_insert(at=5.2, dur=8.0),),
        cues=[],
        text_rows=rows,
        lanes=lanes,
        cutaways=(cutaway, straddling),
    )
    assert [(r["start_s"], r["end_s"]) for r in out.text_rows] == [(17.0, 20.0)]
    cards = out.lanes.overlays
    assert [(c.start_s, c.end_s) for c in cards] == [(4.0, 5.2), (13.2, 15.0)]
    # The rest of a video card resumes where it stopped.
    assert cards[1].source_start_s == pytest.approx(1.0 + 1.2)
    assert cards[1].id != cards[0].id
    windows = sorted((c.start_s, c.end_s, c.source_start_s) for c in out.cutaways)
    assert windows[0] == (4.0, pytest.approx(5.2), 0.5)
    assert windows[1] == (pytest.approx(13.2), pytest.approx(14.0), pytest.approx(1.7))
    assert windows[2][:2] == (28.0, 31.0)


def test_two_inserts_shift_cumulatively_without_double_counting():
    ins = (_insert("a", at=5.2, dur=8.0), _insert("b", at=10.4, dur=3.0))
    out = warp_for_inserts(ins, cues=TAKE_CUES)
    last = out.cues[-1]
    assert last["start_s"] == pytest.approx(25.0 + 8.0 + 3.0)
    third = next(c for c in out.cues if c["text"].startswith("Ortada"))
    assert third["start_s"] == pytest.approx(8.0 + 8.0)


def test_sound_effects_move_by_their_placement_only():
    effect = _resolved_sfx(request=SubtitledSoundEffect(id="sfx-1", catalog_id="pop", at_s=9.0))
    out = warp_for_inserts(
        (_insert(at=5.2, dur=8.0),),
        cues=[],
        lanes=PhoneSubtitledLanes(sound_effects=[effect]),
    )
    assert out.lanes.sound_effects[0].request.at_s == pytest.approx(17.0)


def test_blocked_windows_keep_footage_cutaways_off_the_insert_point():
    ((lo, hi),) = insert_blocked_windows((_insert(at=5.2, dur=8.0),))
    assert lo < 5.2 < hi


# --- the compiler ---------------------------------------------------------------------------


def _compile(inserts, *, keep_segments=None, extras=(), cues=(), cutaways=()):
    return compile_phone_subtitled_plan(
        (_binding("take", duration_s=30.0),),
        caption_cues=list(cues),
        keep_segments=keep_segments,
        additional_speakers=extras,
        inserts=inserts,
        cutaways=cutaways,
    )


def test_flag_off_the_compiler_is_byte_identical():
    plain = compile_phone_subtitled_plan((_binding("take", duration_s=30.0),), caption_cues=[])
    assert _compile(()).model_dump_json() == plain.model_dump_json()


def test_the_insert_pauses_the_take_and_the_take_resumes_where_it_stopped():
    recipe = _compile((_insert(at=12.0, dur=8.0),))
    main = recipe.tracks[0].clips
    assert [
        (c.id, c.source_asset_id, c.source_start, c.timeline_start, c.source_duration) for c in main
    ] == [
        ("clip-0", "take", 0.0, 0.0, 12.0),
        ("insert-0", "talk", 0.0, 12.0, 8.0),
        ("clip-1", "take", 12.0, 20.0, 18.0),
    ]
    assert recipe.duration == pytest.approx(30.0 + 8.0)
    assert main[1].volume == 1
    assert "audioMix" not in recipe.required_capabilities


def test_a_muted_insert_plays_silent_and_declares_the_audio_mix():
    recipe = _compile((_insert(at=12.0, dur=8.0, keep_audio=False),))
    assert recipe.tracks[0].clips[1].volume == 0
    assert "audioMix" in recipe.required_capabilities


def test_an_insert_between_two_cleanup_segments_loses_no_speech():
    keep = [(0.0, 10.0), (14.0, 30.0)]
    recipe = _compile((_insert(at=10.0, dur=5.0),), keep_segments=keep)
    main = recipe.tracks[0].clips
    assert [c.id for c in main] == ["clip-0", "insert-0", "clip-1"]
    speaker = [
        (c.source_start, c.source_start + c.source_duration)
        for c in main
        if c.source_asset_id == "take"
    ]
    assert speaker == keep
    # And mid-segment: the segment is split, every kept second still plays in order.
    split = _compile((_insert(at=4.0, dur=5.0),), keep_segments=keep)
    played = [
        (c.source_start, c.source_start + c.source_duration)
        for c in split.tracks[0].clips
        if c.source_asset_id == "take"
    ]
    assert played == [(0.0, 4.0), (4.0, 10.0), (14.0, 30.0)]
    starts = [c.timeline_start for c in split.tracks[0].clips]
    assert starts == pytest.approx([0.0, 4.0, 9.0, 15.0])


def test_extra_speakers_and_the_ending_move_after_the_insert():
    recipe = _compile((_insert(at=12.0, dur=8.0),), extras=(_binding("second", duration_s=6.0),))
    main = recipe.tracks[0].clips
    assert [(c.id, c.timeline_start) for c in main] == [
        ("clip-0", 0.0),
        ("insert-0", 12.0),
        ("clip-1", 20.0),
        ("speaker-1", 38.0),
    ]
    assert recipe.duration == pytest.approx(44.0)


def test_an_insert_at_the_very_start_or_end_or_beside_another_plays_as_asked():
    start = _compile((_insert(at=0.0, dur=3.0),))
    ids = [c.source_asset_id for c in start.tracks[0].clips]
    assert ids == ["talk", "take"]
    assert start.duration == pytest.approx(33.0)
    end = _compile((_insert(at=30.0, dur=3.0),))
    assert [c.source_asset_id for c in end.tracks[0].clips] == ["take", "talk"]
    assert end.duration == pytest.approx(33.0)
    # Two at one instant play one after the other, in the order given; the take resumes after.
    same = _compile((_insert("a", at=10.0, dur=3.0), _insert("b", at=10.0, dur=2.0)))
    assert [c.source_asset_id for c in same.tracks[0].clips] == ["take", "a", "b", "take"]
    assert same.duration == pytest.approx(35.0)
    both_ends = _compile((_insert("a", at=0.0, dur=3.0), _insert("b", at=30.0, dur=2.0)))
    assert [c.source_asset_id for c in both_ends.tracks[0].clips] == ["a", "take", "b"]


def test_what_the_engine_cannot_play_is_still_refused_by_name():
    with pytest.raises(UnsupportedPhonePlan):  # past the end of the take
        _compile((_insert(at=31.0, dur=3.0),))
    with pytest.raises(UnsupportedPhonePlan):  # the take cannot play twice
        _compile((_insert("take", at=5.0, dur=3.0),))
    with pytest.raises(UnsupportedPhonePlan):
        _compile(
            (_insert("second", at=5.0, dur=3.0),), extras=(_binding("second", duration_s=6.0),)
        )
    with pytest.raises(UnsupportedPhonePlan):  # one clip twice
        _compile((_insert("x", at=5.0, dur=3.0), _insert("x", at=15.0, dur=3.0)))
    too_many = tuple(_insert(f"c{k}", at=1.0 + k, dur=1.0) for k in range(MAX_INSERTS + 1))
    with pytest.raises(UnsupportedPhonePlan):
        _compile(too_many)


def test_the_most_inserts_the_engine_plays_compile_and_round_trip():
    inserts = tuple(_insert(f"c{k}", at=1.0 + 1.3 * k, dur=1.0) for k in range(MAX_INSERTS))
    recipe = _compile(inserts)
    assert recipe.duration == pytest.approx(30.0 + MAX_INSERTS * 1.0)


def test_the_pinned_recipe_gives_the_insert_back_and_a_recompile_is_identical():
    first = _compile((_insert(at=12.0, dur=8.0), _insert("b", at=20.0, dur=3.0)))
    bindings = (
        _binding("take", duration_s=30.0),
        _binding("talk", duration_s=8.0),
        _binding("b", duration_s=3.0),
    )
    # The take is not an extra speaker and the inserts are not extra speakers.
    assert speaker_binding_from_recipe(first, bindings).media_id == "take"
    assert additional_speakers_from_recipe(first, bindings) == ()
    again_inserts = inserts_from_recipe(first, bindings)
    assert [(i.binding.media_id, round(i.at_take_s, 3), i.keep_audio) for i in again_inserts] == [
        ("talk", 12.0, True),
        ("b", 20.0, True),
    ]
    keep = [
        (c.source_start, c.source_start + c.source_duration)
        for c in first.tracks[0].clips
        if c.source_asset_id == "take"
    ]
    again = _compile(again_inserts, keep_segments=keep)
    assert again.model_dump_json() == first.model_dump_json()


def test_edge_and_same_instant_inserts_come_back_from_the_pinned_recipe_unchanged():
    chosen = (
        _insert("a", at=0.0, dur=3.0),
        _insert("b", at=10.0, dur=2.0),
        _insert("c", at=10.0, dur=4.0),
        _insert("d", at=30.0, dur=1.0),
    )
    first = _compile(chosen)
    bindings = (
        _binding("take", duration_s=30.0),
        *(_binding(n, duration_s=d) for n, d in (("a", 3.0), ("b", 2.0), ("c", 4.0), ("d", 1.0))),
    )
    back = inserts_from_recipe(first, bindings)
    assert [(i.binding.media_id, round(i.at_take_s, 3)) for i in back] == [
        ("a", 0.0),
        ("b", 10.0),
        ("c", 10.0),
        ("d", 30.0),
    ]
    keep = [
        (c.source_start, c.source_start + c.source_duration)
        for c in first.tracks[0].clips
        if c.source_asset_id == "take"
    ]
    assert _compile(back, keep_segments=keep).model_dump_json() == first.model_dump_json()


def test_a_sound_cutaway_never_mutes_the_inserted_clip():
    # The cutaway is warped clear of the insert, so no main clip but the take is muted.
    ins = _insert(at=12.0, dur=8.0)
    cutaway = PhoneCutaway(
        binding=_binding("b-roll", duration_s=6.0), start_s=3.0, end_s=6.0, sound=True
    )
    warped = warp_for_inserts((ins,), cues=[], cutaways=(cutaway,))
    recipe = _compile((ins,), cutaways=warped.cutaways)
    assert recipe.audio.mute_windows
    clip_ids = {cid for w in recipe.audio.mute_windows for cid in w.clip_ids}
    assert "insert-0" not in clip_ids


# --- a spoken caption never comes back after the insert (review of #2058, MAJOR 2) ----------


def _cue_words(cues):
    return [w for cue in cues for w in cue.get("words") or []]


def _assert_each_caption_word_once_and_clear_of_inserts(spoken, warped, inserts):
    got = [w["text"] for w in _cue_words(warped)]
    assert sorted(got) == sorted(w["text"] for w in spoken), "a caption word was repeated or lost"
    windows = []
    shift = 0.0
    for i in sorted(inserts, key=lambda i: i.at_take_s):
        length = i.binding.original.duration_s
        windows.append((i.at_take_s + shift, i.at_take_s + shift + length))
        shift += length
    for w in _cue_words(warped):
        for lo, hi in windows:
            assert w["end_s"] <= lo + 1e-6 or w["start_s"] >= hi - 1e-6, (w, lo, hi)
    for cue in warped:
        for lo, hi in windows:
            assert cue["end_s"] <= lo + 1e-6 or cue["start_s"] >= hi - 1e-6, (cue, lo, hi)


def test_a_caption_spoken_before_the_insert_does_not_come_back_after_it():
    from app.pipeline.captions import (
        PHONE_CUE_BUDGET,
        build_plain_cues,
        resplit_cues_into_sentences,
    )
    from app.pipeline.transcribe import Word

    spec = [
        ("Bugün", 3.0, 3.3),
        ("size", 3.4, 3.6),
        ("kongrede", 3.7, 4.2),
        ("olanları", 4.3, 4.9),
        ("anlatacağım,", 5.4, 5.9),
        ("sonra", 6.0, 6.3),
        ("kulübün", 6.4, 6.8),
        ("kararını", 6.9, 7.4),
        ("açıklayacağım", 7.5, 8.2),
        ("ve", 8.3, 8.4),
        ("bitireceğim.", 8.5, 9.2),
        ("Son", 12.0, 12.3),
        ("söz.", 12.4, 12.8),
    ]
    words = [Word(text=t, start_s=s, end_s=e, confidence=1.0) for t, s, e in spec]
    cues = resplit_cues_into_sentences(
        build_plain_cues(words, attach_words=True, budget=PHONE_CUE_BUDGET)
    )
    flat = _cue_words(cues)
    (placed,) = place_inserts(
        [_ask(phrase="kongrede olanları")],
        bindings={"talk": _binding("talk", duration_s=6.0)},
        words=flat,
        take_s=20.0,
    )
    assert placed.insert is not None
    warped = warp_for_inserts((placed.insert,), cues=cues).cues
    _assert_each_caption_word_once_and_clear_of_inserts(flat, warped, (placed.insert,))
    for style in ("sentence", "word"):
        compile_phone_subtitled_plan(
            (_binding("take", duration_s=20.0),),
            caption_cues=warped,
            inserts=(placed.insert,),
            caption_style=style,
        )


def test_a_cue_with_all_its_words_on_one_side_is_never_duplicated_across_the_point():
    cue = {
        "text": "bir iki üç",
        "start_s": 6.0,
        "end_s": 9.0,  # held to the next block: the pause after "üç" is inside it
        "words": _words([("bir", 6.0, 6.4), ("iki", 6.5, 6.9), ("üç", 7.0, 7.4)]),
    }
    out = warp_for_inserts((_insert(at=7.4, dur=5.0),), cues=[cue]).cues
    assert [w["text"] for w in _cue_words(out)] == ["bir", "iki", "üç"]
    assert all(c["end_s"] <= 7.4 + 1e-6 for c in out)
    # All of its words after the point: it plays after the insert, once.
    late = {**cue, "start_s": 6.0, "words": _words([("a", 7.45, 7.9), ("b", 8.0, 8.4)])}
    out = warp_for_inserts((_insert(at=7.5, dur=5.0),), cues=[late]).cues
    assert [w["text"] for w in _cue_words(out)] == ["a", "b"]
    assert all(c["start_s"] >= 12.5 - 1e-6 for c in out)


def test_random_cue_and_insert_layouts_show_each_caption_word_exactly_once():
    import random

    rng = random.Random(20261102)
    for trial in range(300):
        t, spoken = 1.0, []
        for k in range(rng.randint(6, 30)):
            t += rng.choice([0.05, 0.1, 0.1, 0.3, 0.6, 1.4])
            dur = rng.uniform(0.15, 0.7)
            spoken.append({"text": f"w{k}", "start_s": round(t, 3), "end_s": round(t + dur, 3)})
            t += dur
        cues, i = [], 0
        while i < len(spoken):
            n = rng.randint(1, 5)
            cues.append(spoken[i : i + n])
            i += n
        rows = []
        for k, group in enumerate(cues):
            end = cues[k + 1][0]["start_s"] if k + 1 < len(cues) else group[-1]["end_s"]
            rows.append(
                {
                    "text": " ".join(w["text"] for w in group),
                    "start_s": group[0]["start_s"],
                    "end_s": end,
                    "words": [dict(w) for w in group],
                }
            )
        take_s = t + 3.0
        points = sorted(
            {round(rng.uniform(2.5, take_s - 1.0), 3) for _ in range(rng.randint(1, 3))}
        )
        picked = [p for k, p in enumerate(points) if all(abs(p - q) >= 2.0 for q in points[:k])]
        inserts = tuple(
            PhoneInsert(binding=_binding(f"i{k}", duration_s=rng.choice([3.0, 6.0])), at_take_s=p)
            for k, p in enumerate(picked)
        )
        # Through the real placement too: an explicit second is cut exactly there.
        asks = [_ask(f"i{k}", at_s=p) for k, p in enumerate(picked)]
        placed = place_inserts(
            asks,
            bindings={f"i{k}": _binding(f"i{k}", duration_s=6.0) for k in range(len(picked))},
            words=spoken,
            take_s=take_s,
        )
        snapped = tuple(p.insert for p in placed if p.insert is not None)
        for chosen in (inserts, snapped):
            warped = warp_for_inserts(chosen, cues=rows).cues
            _assert_each_caption_word_once_and_clear_of_inserts(spoken, warped, chosen)


# --- no sliver of speaker next to a cut seam (review of #2058, minor c) ---------------------

_ONE_FRAME_S = 1 / 30


def _speaker_pieces_of(recipe):
    return [c for c in recipe.tracks[0].clips if c.source_asset_id == "take"]


def test_an_insert_a_hair_after_a_cleanup_seam_lands_on_the_seam_not_a_sliver_after_it():
    keep = [(0.0, 10.0), (14.0, 30.0)]
    # Take seconds: the seam between the two segments is at 10.0.
    for hair in (0.009, 0.021, -0.012):
        recipe = _compile((_insert(at=10.0 + hair, dur=5.0),), keep_segments=keep)
        pieces = _speaker_pieces_of(recipe)
        assert all(p.source_duration >= _ONE_FRAME_S for p in pieces), hair
        assert [(p.source_start, p.source_start + p.source_duration) for p in pieces] == keep


def test_the_placement_snaps_to_a_nearby_cleanup_seam_without_rounding_it():
    seam = 12.3456789
    words = _words([("bir", 3.0, 3.4), ("iki", 3.5, seam - 0.02), ("üç", seam + 0.5, seam + 0.9)])
    (placed,) = _place(
        [_ask(phrase="bir iki")],
        words=words,
        seams=[seam],
        take_s=30.0,
    )
    assert placed.insert is not None
    assert placed.insert.at_take_s == seam


# --- a no-op Save changes nothing, to the last bit (review of #2058, minor d) ---------------


def test_a_save_round_trip_of_awkward_numbers_is_byte_identical():
    import random

    rng = random.Random(7)
    differing = 0
    for _ in range(200):
        cuts = sorted(rng.uniform(1.0, 38.0) for _ in range(rng.choice([0, 2, 4])))
        keep, cursor = [], 0.0
        for a, b in zip(cuts[::2], cuts[1::2], strict=False):
            if a > cursor:
                keep.append((cursor, a))
            cursor = b
        keep.append((cursor, 40.0))
        take = sum(b - a for a, b in keep)
        points = sorted({round(rng.uniform(2.5, take - 1.0), 3) for _ in range(rng.randint(1, 3))})
        points = [p for k, p in enumerate(points) if all(p - q >= 2.0 for q in points[:k])]
        inserts = tuple(
            PhoneInsert(
                binding=_binding(f"i{k}", duration_s=rng.choice([3.16, 5.537, 8.2501])),
                at_take_s=p,
            )
            for k, p in enumerate(points)
        )
        first = compile_phone_subtitled_plan(
            (_binding("take", duration_s=40.0),),
            caption_cues=[],
            keep_segments=keep,
            inserts=inserts,
        )
        bindings = (_binding("take", duration_s=40.0), *(i.binding for i in inserts))
        back = inserts_from_recipe(first, bindings)
        again_keep = [
            (c.source_start, c.source_start + c.source_duration)
            for c in first.tracks[0].clips
            if c.source_asset_id == "take"
        ]
        again = compile_phone_subtitled_plan(
            (_binding("take", duration_s=40.0),),
            caption_cues=[],
            keep_segments=again_keep,
            inserts=back,
        )
        differing += again.model_dump_json() != first.model_dump_json()
    assert differing == 0


# --- text rows split around an insert keep a minimum part (review of #2058, minor f) --------


def _rows_around(row, at=10.0, dur=6.0):
    return warp_for_inserts((_insert(at=at, dur=dur),), cues=[], text_rows=[row]).text_rows


def test_a_text_row_that_barely_reaches_into_either_side_is_not_split_into_a_flicker():
    started_just_before = {"id": "t", "text": "x", "start_s": 9.8, "end_s": 14.0}
    (only,) = _rows_around(started_just_before)
    # The 0.2 s head is not worth a layer: the row plays once, after the insert, whole.
    assert (only["start_s"], only["end_s"]) == (pytest.approx(16.0), pytest.approx(20.0))
    ends_just_after = {"id": "t", "text": "x", "start_s": 6.0, "end_s": 10.2}
    (only,) = _rows_around(ends_just_after)
    assert (only["start_s"], only["end_s"]) == (6.0, pytest.approx(10.0))
    wide = {"id": "t", "text": "x", "start_s": 6.0, "end_s": 14.0}
    head, tail = _rows_around(wide)
    assert (head["end_s"], tail["start_s"]) == (10.0, pytest.approx(16.0))


def test_a_row_shorter_than_two_parts_keeps_its_longer_side_once():
    brief = {"id": "t", "text": "x", "start_s": 9.8, "end_s": 10.4}
    rows = _rows_around(brief)
    assert len(rows) == 1
    assert rows[0]["start_s"] != rows[0]["end_s"]


# --- the take end and the take start are seams too (review of #2086, finding 1) ------------
# About a third of frame-aligned durations round UP at the millisecond, so a point clamped to
# the take and then rounded landed past it and the compile raised, failing the render.

_AWKWARD_TAKES = (34.1336, 34.1334, 20.0005, 20.0004, 12.3456789, 9.9996, 7.0004999, 61.0006)


def _take_words(take_s):
    return _words([("hi", 0.2, 0.8), ("end.", max(1.0, take_s - 3.0), take_s)])


def _compile_take(take_s, inserts, *, keep_segments=None):
    return compile_phone_subtitled_plan(
        (_binding("take", duration_s=take_s),),
        caption_cues=[],
        keep_segments=keep_segments,
        inserts=inserts,
    )


def _assert_plays_cleanly(recipe, take_s, inserts):
    clips = sorted(recipe.tracks[0].clips, key=lambda c: c.timeline_start)
    speaker = [c for c in clips if c.source_asset_id == "take"]
    assert all(c.source_duration >= _ONE_FRAME_S for c in clips)
    assert sum(c.source_duration for c in speaker) == pytest.approx(take_s, abs=1e-4)
    assert recipe.duration == pytest.approx(
        take_s + sum(i.binding.original.duration_s for i in inserts), abs=1e-4
    )


@pytest.mark.parametrize("take_s", _AWKWARD_TAKES)
@pytest.mark.parametrize("how", ["second_past_end", "last_word_phrase", "second_at_end"])
def test_an_insert_at_the_take_end_compiles_for_any_take_length(take_s, how):
    ask = {
        "second_past_end": _ask(at_s=take_s + 3.0),
        "last_word_phrase": _ask(phrase="end"),
        "second_at_end": _ask(at_s=take_s),
    }[how]
    (placed,) = _place(
        [ask],
        words=_take_words(take_s),
        take_s=take_s,
        bindings={"talk": _binding("talk", duration_s=3.0)},
    )
    assert placed.insert is not None
    assert placed.insert.at_take_s <= take_s
    recipe = _compile_take(take_s, (placed.insert,))
    _assert_plays_cleanly(recipe, take_s, (placed.insert,))
    last = max(recipe.tracks[0].clips, key=lambda c: c.timeline_start)
    assert last.source_asset_id == "talk"  # no speaker sliver after the pause


@pytest.mark.parametrize("take_s", _AWKWARD_TAKES)
def test_an_insert_a_hair_from_the_take_start_or_end_leaves_no_sliver(take_s):
    for at in (0.0004, 0.02, take_s - 0.0004, take_s - 0.02):
        insert = _insert(at=at, dur=3.0)
        recipe = _compile_take(take_s, (insert,))
        _assert_plays_cleanly(recipe, take_s, (insert,))


def test_random_takes_asks_and_cleanups_always_compile_without_a_sliver():
    import random

    rng = random.Random(2086)
    for _ in range(400):
        full = round(rng.uniform(12.0, 70.0), rng.choice([3, 4, 6]))
        cuts = sorted(rng.uniform(1.0, full - 1.0) for _ in range(rng.choice([0, 2, 4])))
        keep, cursor = [], 0.0
        for a, b in zip(cuts[::2], cuts[1::2], strict=False):
            if a > cursor:
                keep.append((cursor, a))
            cursor = b
        keep.append((cursor, full))
        take_s = sum(b - a for a, b in keep)
        words = _words(
            [
                ("hi", 0.2, 0.8),
                ("mid", take_s / 2, take_s / 2 + 0.4),
                ("end.", take_s - 1.0, take_s),
            ]
        )
        seams = []
        total = 0.0
        for a, b in keep[:-1]:
            total += b - a
            seams.append(total)
        asks = []
        for k in range(rng.randint(1, 4)):
            kind = rng.choice(["early", "late", "mid", "past", "phrase_last", "phrase_first"])
            at = {
                "early": rng.uniform(0.0, 0.3),
                "late": take_s - rng.uniform(0.0, 0.3),
                "mid": rng.uniform(0.0, take_s),
                "past": take_s + rng.uniform(0.0, 9.0),
            }.get(kind)
            phrase = {"phrase_last": "end", "phrase_first": "hi"}.get(kind)
            asks.append(InsertAsk(f"c{k}", at_s=at, at_phrase=phrase))
        bindings = {f"c{k}": _binding(f"c{k}", duration_s=rng.uniform(1.0, 9.0)) for k in range(4)}
        placed = place_inserts(asks, bindings=bindings, words=words, take_s=take_s, seams=seams)
        inserts = tuple(p.insert for p in placed if p.insert is not None)
        assert inserts
        recipe = _compile_take(full, inserts, keep_segments=keep)
        _assert_plays_cleanly(recipe, take_s, inserts)
        back = inserts_from_recipe(
            recipe, (_binding("take", duration_s=full), *(i.binding for i in inserts))
        )
        assert [i.binding.media_id for i in back] == [
            i.binding.media_id for i in sorted(inserts, key=lambda i: i.at_take_s)
        ]
