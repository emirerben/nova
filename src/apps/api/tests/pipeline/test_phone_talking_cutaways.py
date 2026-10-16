"""KRI-719: multi-clip Talking to camera -- speaker sorting and footage timing.

Ways this can go wrong (written before the code, per the repo testing rules):
1. Ambient b-roll with a stray Whisper "Thank you." is treated as a speaker,
   so the creator's footage plays as a talking clip with its noise.
2. A silent clip on which Whisper hallucinates words is treated as a speaker.
3. No clip clears the bar and the edit ends with no voice at all.
4. Speakers or footage lose their upload order.
5. A cutaway lands in the hook (first 2.5 s) or the last second.
6. A cutaway runs longer than its own footage (the phone never freezes a clip,
   so the speaker would pop back mid-window) or longer than 4 s.
7. Two cutaways overlap -- the compiler rejects the whole recipe.
8. A cutaway covers a full-screen Visual already placed (``blocked``).
9. Too many clips for a short take: some silently vanish.
10. A clip shorter than the 0.5 s flicker floor is "placed" and then dropped
    by the compiler without a receipt.
"""

from __future__ import annotations

import pytest

from app.pipeline.phone_talking_cutaways import (
    HOOK_FREE_S,
    MAX_WINDOW_S,
    TAIL_FREE_S,
    TalkingClipEvidence,
    classify_talking_clips,
    schedule_footage_cutaways,
)


def _clip(clip_id, duration_s, words, audible=True):
    return TalkingClipEvidence(
        clip_id=clip_id, duration_s=duration_s, word_count=words, audible=audible
    )


def test_two_talking_clips_and_two_footage_clips_keep_upload_order():
    roles = classify_talking_clips(
        [
            _clip("street", 8.0, 0),
            _clip("take1", 20.0, 50),
            _clip("market", 6.0, 2),  # a stray "Thank you." in the noise
            _clip("take2", 15.0, 40),
        ]
    )
    assert roles.speakers == ("take1", "take2")
    assert roles.footage == ("street", "market")
    assert [row["role"] for row in roles.receipt] == ["footage", "speaker", "footage", "speaker"]


def test_a_silent_clip_is_never_a_speaker_even_with_hallucinated_words():
    roles = classify_talking_clips([_clip("dead", 30.0, 90, audible=False), _clip("talk", 10, 20)])
    assert roles.speakers == ("talk",)
    assert roles.footage == ("dead",)


def test_slow_sparse_speech_stays_footage():
    # 8 words over 60 s of ambience is not someone talking to camera.
    roles = classify_talking_clips([_clip("talk", 10.0, 25), _clip("crowd", 60.0, 8)])
    assert roles.footage == ("crowd",)


def test_no_clear_speaker_falls_back_to_the_wordiest_clip():
    roles = classify_talking_clips([_clip("a", 10.0, 3), _clip("b", 10.0, 5), _clip("c", 10, 0)])
    assert roles.speakers == ("b",)
    assert roles.footage == ("a", "c")
    assert next(r for r in roles.receipt if r["clip_id"] == "b")["fallback"] is True


def test_empty_clip_list_is_a_caller_error():
    with pytest.raises(ValueError):
        classify_talking_clips([])


def _assert_well_formed(windows, *, duration_s, footage):
    previous_end = 0.0
    for window in sorted(windows, key=lambda w: w.start_s):
        assert window.start_s >= HOOK_FREE_S - 1e-9
        assert window.end_s <= duration_s - TAIL_FREE_S + 1e-9
        length = window.end_s - window.start_s
        assert 0.5 - 1e-9 <= length <= MAX_WINDOW_S + 1e-9
        assert length <= footage[window.clip_id] + 1e-9
        assert window.start_s >= previous_end - 1e-9, "cutaways must never overlap"
        previous_end = window.end_s


def test_footage_spreads_over_the_talk_in_upload_order_without_overlap():
    footage = {"a": 10.0, "b": 2.0, "c": 6.0}
    windows, unplaced = schedule_footage_cutaways(list(footage.items()), duration_s=40.0)
    assert unplaced == []
    assert [w.clip_id for w in windows] == ["a", "b", "c"]
    _assert_well_formed(windows, duration_s=40.0, footage=footage)
    # Short footage plays for its own length, never more.
    assert next(w for w in windows if w.clip_id == "b").end_s - next(
        w for w in windows if w.clip_id == "b"
    ).start_s == pytest.approx(2.0, abs=1e-3)


def test_cutaway_starts_snap_to_a_spoken_word():
    windows, _ = schedule_footage_cutaways(
        [("a", 10.0)], duration_s=20.0, word_starts=[3.0, 9.0, 15.0]
    )
    assert windows[0].start_s == pytest.approx(9.0)  # nominal 8.75


def test_blocked_full_screen_visuals_keep_their_slot():
    footage = {"a": 10.0, "b": 10.0}
    blocked = [(6.0, 10.0), (14.0, 18.0)]
    windows, unplaced = schedule_footage_cutaways(
        list(footage.items()), duration_s=24.0, blocked=blocked
    )
    assert unplaced == []
    _assert_well_formed(windows, duration_s=24.0, footage=footage)
    for window in windows:
        for b_start, b_end in blocked:
            assert window.end_s <= b_start + 1e-9 or window.start_s >= b_end - 1e-9


def test_too_many_clips_for_a_short_take_are_reported_not_dropped():
    footage = [(f"c{i}", 5.0) for i in range(6)]
    windows, unplaced = schedule_footage_cutaways(footage, duration_s=8.0)
    # 8 s talk -> 2.5 s hook, 1 s tail -> 4.5 s free -> three 1.5 s slots.
    assert [w.clip_id for w in windows] == ["c0", "c1", "c2"]
    assert unplaced == [
        {"clip_id": "c3", "reason": "no_room_in_timeline"},
        {"clip_id": "c4", "reason": "no_room_in_timeline"},
        {"clip_id": "c5", "reason": "no_room_in_timeline"},
    ]
    _assert_well_formed(windows, duration_s=8.0, footage=dict(footage))


def test_a_flicker_length_clip_is_reported_too_short():
    windows, unplaced = schedule_footage_cutaways([("blink", 0.3), ("ok", 5.0)], duration_s=20.0)
    assert [w.clip_id for w in windows] == ["ok"]
    assert unplaced == [{"clip_id": "blink", "reason": "clip_too_short"}]


def test_a_take_shorter_than_the_hook_gets_no_cutaway():
    windows, unplaced = schedule_footage_cutaways([("a", 5.0)], duration_s=3.0)
    assert windows == []
    assert unplaced == [{"clip_id": "a", "reason": "no_room_in_timeline"}]
