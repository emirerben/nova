"""Insert clips in a phone Talking edit (parent KRI-1151).

An insert switches the picture to another clip, with that clip's own audio, while the
creator's take is PAUSED, then the take resumes where it stopped. Nothing he said is lost
and the video grows by the insert's length.

Failure-mode list, written before the code (each one is a test below):

 1. insert at the very start / inside the hook window       -> refused, `in_hook_window`
 2. insert at the very end / inside the last second          -> refused, `in_tail_window`
 3. two inserts closer than MIN_GAP_S on the take            -> the later one refused
 4. the phrase is never said                                 -> refused, `phrase_not_found`
 5. the phrase sits in a region speech cleanup cut out       -> the same refusal (cut-time
    words never contain it), never placed at a guessed time
 6. the insert is longer than MAX_INSERT_S                   -> refused, `too_long`
 7. the same clip asked twice                                -> the second refused
 8. the clip is already a speaker / cutaway / the ending     -> refused, `clip_in_use`
 9. more than MAX_INSERTS                                    -> the rest refused
10. the position lands inside a word                         -> moved to a word boundary
11. a caption / card / cutaway straddles the insert point    -> split around it, never
    shown over the inserted clip
12. a second insert comes after a first                      -> its position already includes
    the first's length (no double or missing shift)
13. the recompile of a pinned recipe (editor Save)           -> the insert comes back as it was
14. Range after an insert                                    -> refused with a named reason
15. flag off                                                 -> byte-identical recipe
"""

from __future__ import annotations

import pytest
from app.pipeline.phone_talking_inserts import (
    HOOK_FREE_S,
    MAX_INSERT_S,
    MAX_INSERTS,
    MIN_GAP_S,
    TAIL_FREE_S,
    InsertAsk,
    insert_blocked_windows,
    inserts_from_recipe,
    place_inserts,
    shift_time,
    warp_for_inserts,
)

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


def _place(asks, *, bindings=None, in_use=(), words=TAKE_WORDS, cues=TAKE_CUES, take_s=TAKE_S):
    bindings = bindings if bindings is not None else {"talk": _binding("talk", duration_s=8.0)}
    return place_inserts(
        asks, bindings=bindings, words=words, cues=cues, take_s=take_s, in_use=in_use
    )


# --- placement ------------------------------------------------------------------------------


def test_a_phrase_places_the_insert_at_the_end_of_the_caption_that_says_it():
    (placed,) = _place([_ask(phrase="kongreyi anlatacağım")])
    assert placed.reason is None
    assert placed.insert is not None
    # "anlatacağım" ends 5.2, the caption ends 5.2: the take pauses there.
    assert placed.insert.at_take_s == pytest.approx(5.2)
    assert placed.basis == "phrase"
    assert placed.snap == "cue_end"


def test_a_phrase_inside_a_sentence_moves_to_the_pause_after_the_word_never_mid_word():
    # "henüz bir" ends at 9.3, mid-caption; the next gap is after "yok" (10.4 -> 14.0), which
    # is the caption end, so it snaps there. The point is never inside a word.
    (placed,) = _place([_ask(phrase="henüz bir")])
    assert placed.insert is not None
    point = placed.insert.at_take_s
    assert not any(w["start_s"] < point < w["end_s"] for w in TAKE_WORDS)
    assert point == pytest.approx(10.4)


def test_a_second_in_the_middle_of_a_word_moves_to_the_word_boundary():
    (placed,) = _place([_ask(at_s=4.0)])  # inside "kongreyi" 3.5-4.2
    assert placed.insert is not None
    point = placed.insert.at_take_s
    assert not any(w["start_s"] < point < w["end_s"] for w in TAKE_WORDS)
    assert placed.basis == "second"


def test_a_phrase_that_is_never_said_is_refused_not_guessed():
    (placed,) = _place([_ask(phrase="bu hiç söylenmedi")])
    assert placed.insert is None
    assert placed.reason == "phrase_not_found"


def test_a_phrase_in_a_cut_out_region_is_refused_like_one_never_said():
    cut_time_words = [w for w in TAKE_WORDS if w["text"] != "kongreyi"]
    (placed,) = _place([_ask(phrase="kongreyi")], words=cut_time_words)
    assert placed.insert is None
    assert placed.reason == "phrase_not_found"


def test_an_insert_at_the_start_or_in_the_hook_window_is_refused():
    for at in (0.0, HOOK_FREE_S - 0.1):
        (placed,) = _place([_ask(at_s=at)])
        assert placed.insert is None
        assert placed.reason == "in_hook_window"


def test_an_insert_at_the_end_or_in_the_last_second_is_refused():
    for at in (TAKE_S, TAKE_S - TAIL_FREE_S + 0.1):
        (placed,) = _place([_ask(at_s=at)])
        assert placed.insert is None
        assert placed.reason == "in_tail_window"


def test_an_insert_longer_than_the_cap_is_refused():
    long = {"talk": _binding("talk", duration_s=MAX_INSERT_S + 1)}
    (placed,) = _place([_ask(at_s=10.0)], bindings=long)
    assert placed.insert is None
    assert placed.reason == "too_long"


def test_the_same_clip_twice_places_once():
    first, second = _place([_ask(at_s=6.0), _ask(at_s=16.0)])
    assert first.insert is not None
    assert second.insert is None
    assert second.reason == "duplicate_clip"


def test_a_clip_already_used_as_speaker_cutaway_or_ending_is_refused():
    (placed,) = _place([_ask(at_s=6.0)], in_use={"talk"})
    assert placed.insert is None
    assert placed.reason == "clip_in_use"


def test_adjacent_inserts_closer_than_the_gap_refuse_the_later_one():
    bindings = {"a": _binding("a", duration_s=4.0), "b": _binding("b", duration_s=4.0)}
    first, second = _place(
        [_ask("a", at_s=5.2), _ask("b", at_s=5.2 + MIN_GAP_S - 0.5)], bindings=bindings
    )
    assert first.insert is not None
    assert second.insert is None
    assert second.reason == "too_close_to_previous"


def test_the_count_is_capped():
    ids = [f"c{i}" for i in range(MAX_INSERTS + 1)]
    bindings = {i: _binding(i, duration_s=3.0) for i in ids}
    ats = [5.2, 10.4, 15.8, 25.6]
    placed = _place([_ask(i, at_s=a) for i, a in zip(ids, ats, strict=True)], bindings=bindings)
    assert [p.insert is not None for p in placed] == [True, True, True, False]
    assert placed[-1].reason == "too_many"


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


def test_an_insert_at_the_edges_or_a_clip_already_playing_is_refused():
    with pytest.raises(ValueError):  # the model itself refuses a point at the very start
        _insert(at=0.0, dur=3.0)
    for at in (30.0, 31.0):
        with pytest.raises(UnsupportedPhonePlan):
            _compile((_insert(at=at, dur=3.0),))
    with pytest.raises(UnsupportedPhonePlan):
        _compile((_insert("take", at=5.0, dur=3.0),))
    with pytest.raises(UnsupportedPhonePlan):
        _compile(
            (_insert("second", at=5.0, dur=3.0),), extras=(_binding("second", duration_s=6.0),)
        )
    with pytest.raises(UnsupportedPhonePlan):
        _compile((_insert("x", at=5.0, dur=3.0), _insert("x", at=15.0, dur=3.0)))


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
