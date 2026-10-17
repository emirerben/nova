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

Speaking pace over the spoken span (T2 boat kit, measured with local
whisper.cpp medium), written before the change:
11. A real take with a quiet lead-in or tail (T2_01: 10 words in the first 8 s
    of a 15 s clip, 0.67 words/s over the whole clip) is muted as footage and
    the creator's words are lost.
12. Measuring over the spoken span lets a short hallucination burst (T2_03
    "Altyazı M.K.", T2_04 "İzlediğiniz için teşekkürler": 2-3 words inside 2 s)
    clear the pace bar -- the word floor must still keep them footage.
13. On a long ambient clip, two stock Whisper outros stacked together reach
    the word floor inside a few seconds, which the old whole-clip pace diluted
    away -- known outro phrases must not count as speech.
14. A tiny span (two words 50 ms apart) gives an absurd pace in the receipt.
15. Words scattered thinly across a long clip (crowd chatter) become a speaker
    once the span is used instead of the duration.
16. A clip without word timings (no span) silently changes behaviour; it must
    keep the whole-clip pace.
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
    speech_evidence,
)
from app.pipeline.transcribe import Word


def _clip(clip_id, duration_s, words, audible=True, span_s=None):
    return TalkingClipEvidence(
        clip_id=clip_id,
        duration_s=duration_s,
        word_count=words,
        audible=audible,
        speech_span_s=span_s,
    )


def _words(*timed):
    return [Word(text=text, start_s=start, end_s=end, confidence=1.0) for start, end, text in timed]


# The T2 boat kit, as local whisper.cpp medium heard it.
T2_01_WORDS = _words(
    (0.00, 0.72, "-Dönmek"),
    (0.72, 1.62, "istiyorsan"),
    (1.62, 2.00, "dön."),
    (2.00, 2.37, "-Aa"),
    (2.37, 3.60, "kaldık."),
    (3.60, 4.21, "Tekne"),
    (4.21, 6.00, "çalışmıyor."),
    (6.00, 6.50, "-Tekne"),
    (6.50, 7.68, "çalışmıyor"),
    (7.68, 8.00, "lan."),
)
T2_03_WORDS = _words((0.00, 0.97, "Altyazı"), (0.97, 2.00, "M.K."))
T2_04_WORDS = _words(
    (0.00, 0.81, "İzlediğiniz"), (0.81, 1.13, "için"), (1.13, 2.00, "teşekkürler.")
)


def _measured(clip_id, duration_s, words):
    count, span = speech_evidence(words)
    return _clip(clip_id, duration_s, count, span_s=span)


def test_t2_boat_take_with_a_quiet_tail_speaks_and_river_clips_stay_footage():
    roles = classify_talking_clips(
        [
            _measured("T2_01", 15.0, T2_01_WORDS),
            _measured("T2_03", 12.0, T2_03_WORDS),
            _measured("T2_04", 14.0, T2_04_WORDS),
        ]
    )
    assert roles.speakers == ("T2_01",)
    assert roles.footage == ("T2_03", "T2_04")
    t2_01 = roles.receipt[0]
    assert t2_01["word_count"] == 10
    assert t2_01["words_per_s"] == 1.25  # 10 words over the 8 s actually spoken
    assert "fallback" not in t2_01


def test_t2_take_scored_over_the_whole_clip_alone_would_be_footage():
    # The pre-fix measurement: 10 words / 15 s = 0.67 words/s. Without a span the
    # whole-clip pace still applies, so the boat take needs its span to speak.
    roles = classify_talking_clips([_clip("T2_01", 15.0, 10), _clip("other", 10.0, 30)])
    assert roles.footage == ("T2_01",)


def test_stock_whisper_outros_do_not_count_as_speech():
    assert speech_evidence(T2_04_WORDS) == (0, None)
    stacked = _words(
        (20.0, 20.8, "Abone"),
        (20.8, 21.4, "olmayı"),
        (21.4, 22.0, "unutmayın."),
        (22.0, 22.8, "İzlediğiniz"),
        (22.8, 23.1, "için"),
        (23.1, 23.6, "teşekkür"),
        (23.6, 24.0, "ederim."),
    )
    assert speech_evidence(stacked) == (0, None)
    roles = classify_talking_clips(
        [_measured("river", 30.0, stacked), _measured("T2_01", 15.0, T2_01_WORDS)]
    )
    assert roles.footage == ("river",)


def test_real_words_around_an_outro_still_count_with_their_own_span():
    words = _words(
        (1.0, 1.4, "Çocuklar"),
        (1.4, 1.8, "parkta"),
        (1.8, 2.3, "oynuyor"),
        (9.0, 9.8, "İzlediğiniz"),
        (9.8, 10.1, "için"),
        (10.1, 10.9, "teşekkürler."),
    )
    count, span = speech_evidence(words)
    assert count == 3
    assert span == pytest.approx(1.3)


def test_a_tiny_span_reports_a_sane_pace():
    roles = classify_talking_clips([_clip("blip", 10.0, 6, span_s=0.05)])
    # Floored to MIN_SPEECH_SPAN_S, not 120 words/s.
    assert roles.receipt[0]["words_per_s"] <= 6 / 2.0


def test_words_scattered_across_a_long_clip_stay_footage():
    # Crowd chatter: 8 words spread over 55 s of a 60 s clip.
    roles = classify_talking_clips(
        [_clip("talk", 10.0, 25, span_s=9.0), _clip("crowd", 60.0, 8, span_s=55.0)]
    )
    assert roles.footage == ("crowd",)


def test_no_words_means_no_span():
    assert speech_evidence([]) == (0, None)


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
