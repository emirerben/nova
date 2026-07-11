"""Tests for the deterministic phrase engine (app/pipeline/phrase_sequence.py).

The engine is the timing source of truth for the transcript-synced typographic
sequence: scene boundaries, display windows (D5 clean-cut/hold-cap rules —
reference-verified hard cuts with an all-clear gap; scenes never overlap), and
the eligibility gate (D8) must be deterministic and anchored on word START
times (D16 — the karaoke learning: end timestamps drift, starts don't).
"""

from __future__ import annotations

import pytest

from app.pipeline.phrase_sequence import (
    COVERAGE_MIN_FRAC,
    FADE_OUT_S,
    HOLD_CAP_S,
    MAX_PHRASE_WORDS,
    MIN_SCENE_GAP_S,
    MIN_SPEECH_WORDS,
    PAUSE_GAP_S,
    RHYTHM_LEAD_IN_S,
    RHYTHM_MIN_CHAR_WEIGHT,
    RHYTHM_SPEAK_FRAC,
    RHYTHM_TAIL_S,
    SCENE_CLEAR_GAP_S,
    rhythm_scenes,
    scenes_total_window,
    speech_eligibility,
    split_phrases,
    split_sentences,
    synthesize_phrase_timings,
)
from app.pipeline.transcribe import Word

_SCENE_KEYS = {
    "words",
    "word_roles",
    "speech_start_s",
    "speech_end_s",
    "start_s",
    "end_s",
    "fade_out",
}


def _w(text: str, start: float, end: float) -> dict:
    return {"text": text, "start": start, "end": end}


def _run(texts: str, *, start: float = 0.0, dur: float = 0.25, gap: float = 0.05) -> list[dict]:
    """Contiguous words at a fixed cadence; gap stays under PAUSE_GAP_S."""
    words, t = [], start
    for text in texts.split():
        words.append(_w(text, t, t + dur))
        t += dur + gap
    return words


# -- constants (doc-locked, shared with the renderer/orchestration) -------------


def test_module_constants_are_locked():
    assert PAUSE_GAP_S == 0.35
    assert MAX_PHRASE_WORDS == 6
    assert MIN_SPEECH_WORDS == 4
    assert HOLD_CAP_S == 4.0
    assert SCENE_CLEAR_GAP_S == 0.1
    assert MIN_SCENE_GAP_S == 0.034
    assert FADE_OUT_S == 0.4
    assert COVERAGE_MIN_FRAC == 0.5


# -- eligibility (D8) ------------------------------------------------------------


def test_empty_and_none_input():
    assert split_phrases([], video_duration_s=10.0) == []
    assert split_phrases(None, video_duration_s=10.0) == []
    elig = speech_eligibility([], video_duration_s=10.0)
    assert elig == {
        "eligible": False,
        "reason": "too_few_words",
        "word_count": 0,
        "coverage_frac": 0.0,
    }


def test_too_few_words_ineligible():
    words = _run("one two three", dur=2.0, gap=0.1)  # 3 < MIN_SPEECH_WORDS
    elig = speech_eligibility(words, video_duration_s=7.0)
    assert elig["eligible"] is False
    assert elig["reason"] == "too_few_words"
    assert elig["word_count"] == 3
    assert split_phrases(words, video_duration_s=7.0) == []


def test_low_coverage_ineligible():
    # 5 words crammed into the first 2s of a 60s video — speech is an aside,
    # not the spine of the edit.
    words = _run("a b c d e", dur=0.3, gap=0.1)  # span 0.0 → 1.9
    elig = speech_eligibility(words, video_duration_s=60.0)
    assert elig["eligible"] is False
    assert elig["reason"] == "low_coverage"
    assert elig["word_count"] == 5
    assert elig["coverage_frac"] == pytest.approx(1.9 / 60.0, abs=1e-3)
    assert split_phrases(words, video_duration_s=60.0) == []


def test_bad_duration_ineligible():
    words = _run("a b c d e")
    for duration in (0.0, -5.0):
        elig = speech_eligibility(words, video_duration_s=duration)
        assert elig["eligible"] is False
        assert elig["reason"] == "bad_duration"
        assert split_phrases(words, video_duration_s=duration) == []


def test_eligible_ok():
    words = _run("a b c d e f g h", dur=0.5, gap=0.2)  # span 0.0 → 5.4
    elig = speech_eligibility(words, video_duration_s=10.0)
    assert elig["eligible"] is True
    assert elig["reason"] == "ok"
    assert elig["word_count"] == 8
    assert elig["coverage_frac"] == pytest.approx(0.54, abs=1e-3)


def test_words_with_missing_timestamps_are_skipped():
    words = [
        _w("we", 0.0, 0.3),
        {"text": "um", "start": None, "end": None},  # defensive skip
        _w("waited", 0.4, 0.8),
        _w("for", 0.9, 1.2),
        _w("this", 1.3, 1.7),
    ]
    assert speech_eligibility(words, video_duration_s=3.0)["word_count"] == 4
    scenes = split_phrases(words, video_duration_s=3.0)
    assert len(scenes) == 1
    assert scenes[0]["words"] == ["we", "waited", "for", "this"]


# -- phrase splitting ------------------------------------------------------------


def test_pause_gap_splits_phrases():
    words = [
        _w("we", 0.0, 0.3),
        _w("waited", 0.4, 0.8),
        _w("for", 1.2, 1.5),  # 0.4s silence > PAUSE_GAP_S
        _w("this", 1.6, 2.0),
    ]
    scenes = split_phrases(words, video_duration_s=3.0)
    assert [s["words"] for s in scenes] == [["we", "waited"], ["for", "this"]]


def test_sub_threshold_gap_does_not_split():
    words = [
        _w("we", 0.0, 0.3),
        _w("waited", 0.6, 0.9),  # 0.3s gap < PAUSE_GAP_S
        _w("for", 1.0, 1.3),
        _w("this", 1.4, 1.8),
    ]
    scenes = split_phrases(words, video_duration_s=3.0)
    assert len(scenes) == 1


def test_terminal_punctuation_splits_phrases():
    words = [
        _w("Stop.", 0.0, 0.4),
        _w("right", 0.5, 0.9),
        _w("there", 1.0, 1.4),
        _w("now!", 1.5, 1.9),
        _w("okay?", 2.0, 2.4),
        _w("sure…", 2.5, 2.9),
        _w("then", 3.0, 3.4),
    ]
    scenes = split_phrases(words, video_duration_s=4.0)
    assert [s["words"] for s in scenes] == [
        ["Stop."],
        ["right", "there", "now!"],
        ["okay?"],
        ["sure…"],
        ["then"],
    ]


def test_cap_split_at_max_phrase_words():
    words = _run("one two three four five six seven eight")  # no punctuation, no pause
    scenes = split_phrases(words, video_duration_s=4.0)
    assert [len(s["words"]) for s in scenes] == [MAX_PHRASE_WORDS, 2]
    assert scenes[0]["words"] == ["one", "two", "three", "four", "five", "six"]
    assert scenes[1]["words"] == ["seven", "eight"]


def test_words_flatten_back_in_original_order():
    texts = "a b. c d e f g h i j k l m"  # punct split + cap split + tail
    words = _run(texts)
    scenes = split_phrases(words, video_duration_s=7.0)
    assert [w for s in scenes for w in s["words"]] == texts.split()


# -- display windows (D5) ---------------------------------------------------------


def test_scene_zero_clamps_to_first_frame_when_speech_starts_early():
    words = _run("a b c d", start=0.1)
    scenes = split_phrases(words, video_duration_s=2.0)
    assert scenes[0]["start_s"] == 0.0  # text there from the first frame
    assert scenes[0]["speech_start_s"] == pytest.approx(0.1)


def test_scene_zero_keeps_speech_start_when_speech_starts_late():
    words = _run("a b c d", start=0.5, dur=0.4, gap=0.1)
    scenes = split_phrases(words, video_duration_s=3.5)
    assert scenes[0]["start_s"] == pytest.approx(0.5)


def test_clean_cut_gap_anchors_on_next_scene_start():
    words = [
        _w("we", 0.0, 0.3),
        _w("waited", 0.4, 0.8),
        _w("for", 2.0, 2.3),  # pause split; next scene starts at 2.0
        _w("this", 2.4, 2.8),
    ]
    scenes = split_phrases(words, video_duration_s=4.0)
    assert len(scenes) == 2
    # Reference-verified hard cut: the scene clears SCENE_CLEAR_GAP_S BEFORE
    # the NEXT scene's START (karaoke learning: the next word's end, 2.3,
    # plays no part in the anchor). No overlap, no crossfade.
    assert scenes[0]["end_s"] == pytest.approx(scenes[1]["start_s"] - SCENE_CLEAR_GAP_S)
    assert scenes[0]["end_s"] == pytest.approx(1.9)
    assert scenes[0]["fade_out"] is False  # hard cut — the renderer must not fade
    assert scenes[1]["fade_out"] is True  # last scene always fades out


def test_degenerate_back_to_back_phrases_keep_one_frame_gap():
    # Scene 0 is a single clipped word; the next scene starts only 0.06s after
    # scene 0's display start, so the full 0.1s clear gap would produce a
    # negative-duration window. The gap shrinks to one frame instead.
    words = [
        _w("Go.", 0.5, 0.55),  # punctuation split → one-word scene
        _w("now", 0.56, 0.9),
        _w("we", 1.0, 1.3),
        _w("run", 1.4, 1.8),
    ]
    scenes = split_phrases(words, video_duration_s=2.5)
    assert len(scenes) == 2
    assert scenes[0]["start_s"] == pytest.approx(0.5)
    assert scenes[1]["start_s"] == pytest.approx(0.56)
    assert scenes[0]["end_s"] == pytest.approx(scenes[1]["start_s"] - MIN_SCENE_GAP_S, abs=1e-3)
    assert scenes[0]["end_s"] > scenes[0]["start_s"]
    assert scenes[0]["fade_out"] is False


def test_hold_cap_end_is_clamped_to_the_clear_gap_ceiling():
    # Silence gap barely over HOLD_CAP_S: the unclamped hold-cap end
    # (speech_end + HOLD_CAP + FADE_OUT = 6.4) would overlap the next scene
    # (starts 6.2). The no-overlap invariant wins: end at the cut ceiling.
    words = [
        _w("we", 0.0, 0.4),
        _w("waited", 0.5, 1.0),
        _w("so", 1.1, 1.5),
        _w("long", 1.6, 2.0),
        _w("finally", 6.2, 6.6),  # 4.2s silence > HOLD_CAP_S
        _w("here", 6.7, 7.0),
    ]
    scenes = split_phrases(words, video_duration_s=8.0)
    assert len(scenes) == 2
    assert scenes[0]["end_s"] == pytest.approx(scenes[1]["start_s"] - SCENE_CLEAR_GAP_S)
    assert scenes[0]["end_s"] == pytest.approx(6.1)
    assert scenes[0]["fade_out"] is True  # still a hold-cap ending → soft fade


def test_trailing_silence_triggers_hold_cap_and_fade_out():
    words = [
        _w("we", 0.0, 0.3),
        _w("waited", 0.4, 0.8),
        _w("so", 0.9, 1.2),
        _w("long", 1.3, 2.0),
        _w("finally", 8.0, 8.4),  # 6s of silence > HOLD_CAP_S
        _w("here", 8.5, 8.8),
    ]
    scenes = split_phrases(words, video_duration_s=10.0)
    assert len(scenes) == 2
    # Scene 0 ends early: speech_end + HOLD_CAP + FADE_OUT, screen text-free after.
    assert scenes[0]["end_s"] == pytest.approx(2.0 + HOLD_CAP_S + FADE_OUT_S)
    assert scenes[0]["fade_out"] is True
    assert scenes[0]["end_s"] < scenes[1]["start_s"]


def test_last_scene_clamps_to_video_duration():
    words = [
        _w("we", 0.0, 0.3),
        _w("waited", 0.4, 0.8),
        _w("for", 2.0, 2.3),
        _w("this", 2.4, 2.8),
    ]
    scenes = split_phrases(words, video_duration_s=4.0)
    # speech_end 2.8 + HOLD_CAP would run past the 4.0s video → clamp.
    assert scenes[-1]["end_s"] == pytest.approx(4.0)
    assert scenes[-1]["fade_out"] is True


def test_last_scene_holds_then_fades_when_video_is_long_enough():
    words = _run("a b c d e f", dur=0.6, gap=0.3)  # one scene, speech 0.0 → 5.1
    scenes = split_phrases(words, video_duration_s=10.0)
    assert len(scenes) == 1
    assert scenes[0]["end_s"] == pytest.approx(5.1 + HOLD_CAP_S + FADE_OUT_S)
    assert scenes[0]["fade_out"] is True


_NO_OVERLAP_CASES = {
    "punct_and_cap_splits": (_run("a b. c d e f g h i j k l m"), 7.0),
    "pause_splits": (
        [_w("we", 0.0, 0.3), _w("waited", 0.4, 0.8), _w("for", 2.0, 2.3), _w("this", 2.4, 2.8)],
        4.0,
    ),
    "hold_cap_long_silence": (
        [
            _w("we", 0.0, 0.3),
            _w("waited", 0.4, 0.8),
            _w("so", 0.9, 1.2),
            _w("long", 1.3, 2.0),
            _w("finally", 8.0, 8.4),
            _w("here", 8.5, 8.8),
        ],
        10.0,
    ),
    "hold_cap_marginal_gap": (
        [
            _w("we", 0.0, 0.4),
            _w("waited", 0.5, 1.0),
            _w("so", 1.1, 1.5),
            _w("long", 1.6, 2.0),
            _w("finally", 6.2, 6.6),
            _w("here", 6.7, 7.0),
        ],
        8.0,
    ),
    "degenerate_back_to_back": (
        [_w("Go.", 0.5, 0.55), _w("now", 0.56, 0.9), _w("we", 1.0, 1.3), _w("run", 1.4, 1.8)],
        2.5,
    ),
    "late_start": (_run("a b c d", start=0.5, dur=0.4, gap=0.1), 3.5),
    "messy_float_timestamps": (
        [
            _w("Hmm.", 0.12345, 0.41234),
            _w("we", 0.51234, 0.91234),
            _w("kept", 1.00012, 1.40009),
            _w("going", 1.50007, 1.90012),
            _w("anyway", 2.30009, 2.70013),
        ],
        3.21234,
    ),
}


@pytest.mark.parametrize("words, duration", _NO_OVERLAP_CASES.values(), ids=_NO_OVERLAP_CASES)
def test_scene_windows_never_overlap(words: list[dict], duration: float):
    """Property (D5 revised): hard cuts — every scene clears the screen at
    least one frame before the next starts, and every window has positive
    duration, across the splitter's whole behavior space."""
    scenes = split_phrases(words, video_duration_s=duration)
    assert len(scenes) >= 1  # guard against vacuous pass
    for cur, nxt in zip(scenes, scenes[1:]):
        assert cur["end_s"] > cur["start_s"]
        # One-frame floor (1e-3 slack: end and next-start round independently
        # to 3 decimals).
        assert nxt["start_s"] - cur["end_s"] >= MIN_SCENE_GAP_S - 1e-3
    assert scenes[-1]["end_s"] > scenes[-1]["start_s"]
    assert scenes[-1]["end_s"] <= duration + 1e-9


# -- input formats / determinism ---------------------------------------------------


def test_dict_and_dataclass_words_produce_identical_scenes():
    dicts = [
        _w("we", 0.0, 0.3),
        _w("waited", 0.4, 0.8),
        _w("for", 1.2, 1.5),
        _w("this", 1.6, 2.0),
    ]
    dataclasses = [
        Word(text=d["text"], start_s=d["start"], end_s=d["end"], confidence=1.0) for d in dicts
    ]
    assert speech_eligibility(dicts, video_duration_s=3.0) == speech_eligibility(
        dataclasses, video_duration_s=3.0
    )
    scenes = split_phrases(dicts, video_duration_s=3.0)
    assert scenes == split_phrases(dataclasses, video_duration_s=3.0)
    assert scenes  # parity of two empty lists proves nothing


def test_output_is_deterministic():
    words = _run("the city never sleeps. we kept walking through the rain", dur=0.4, gap=0.2)
    a = split_phrases(words, video_duration_s=11.0)
    b = split_phrases(words, video_duration_s=11.0)
    assert a == b
    assert a  # guard against vacuous pass


def test_start_times_are_non_decreasing():
    words = _run("a b. c d e f g h i j k l m")  # punct + cap + tail splits
    scenes = split_phrases(words, video_duration_s=7.0)
    assert len(scenes) >= 3
    starts = [s["start_s"] for s in scenes]
    assert starts == sorted(starts)


def test_scene_shape_and_role_placeholder():
    scenes = split_phrases(_run("a b c d"), video_duration_s=2.0)
    for scene in scenes:
        assert set(scene) == _SCENE_KEYS
        assert len(scene["word_roles"]) == len(scene["words"])
        assert scene["word_roles"].count("hero") == 1
        assert scene["speech_start_s"] <= scene["speech_end_s"]
        assert scene["start_s"] < scene["end_s"]
        assert isinstance(scene["fade_out"], bool)


def test_scene_roles_mark_one_deterministic_keyword():
    scenes = split_phrases(_run("we found the quiet library."), video_duration_s=2.0)
    assert scenes[0]["words"] == ["we", "found", "the", "quiet", "library."]
    assert scenes[0]["word_roles"] == ["connector", "connector", "connector", "connector", "hero"]


def test_times_are_rounded_to_3_decimals():
    words = [
        _w("a", 0.123456, 0.412345),
        _w("b", 0.51234, 0.912345),
        _w("c", 1.0001, 1.40009),
        _w("d", 1.50007, 1.9001),
    ]
    scenes = split_phrases(words, video_duration_s=3.0)
    assert scenes
    for scene in scenes:
        for key in ("speech_start_s", "speech_end_s", "start_s", "end_s"):
            assert scene[key] == round(scene[key], 3)


# -- rhythm mode (synthesized timings, no ASR) ---------------------------------------

# The user-approved demo render: this quote at this duration produced exactly
# these scene windows. The golden test below pins demo == production.
_GOLDEN_QUOTE = (
    'It\'s not "just luck". If you put in the work. To get there. Luck is just. '
    "A combination of hard work. And good timing. So… don't allow anyone. "
    "To diminish your hard work."
)
_GOLDEN_DURATION_S = 10.4
# Approved windows (3-decimal): window [0.3, 9.6], 9 equal sentence slots.
_GOLDEN_WINDOWS = [
    (0.3, 1.233),
    (1.333, 2.267),
    (2.367, 3.3),
    (3.4, 4.333),
    (4.433, 5.367),
    (5.467, 6.4),
    (6.5, 7.433),
    (7.533, 8.467),
    (8.567, 10.4),
]


def test_rhythm_constants_are_locked():
    assert RHYTHM_LEAD_IN_S == 0.3
    assert RHYTHM_TAIL_S == 0.8
    assert RHYTHM_SPEAK_FRAC == 0.62
    assert RHYTHM_MIN_CHAR_WEIGHT == 2


def test_golden_demo_quote_reproduces_approved_scene_windows():
    """The exact approved demo: quote + 10.4s video → EXACTLY these 9 windows.

    3-decimal equality (the engine rounds to 3 decimals); any drift here means
    production no longer matches the render the user approved.
    """
    scenes = rhythm_scenes(_GOLDEN_QUOTE, video_duration_s=_GOLDEN_DURATION_S)
    assert len(scenes) == 9
    assert [(s["start_s"], s["end_s"]) for s in scenes] == _GOLDEN_WINDOWS
    # One scene per sentence, words intact and in order.
    assert [" ".join(s["words"]) for s in scenes] == [
        'It\'s not "just luck".',
        "If you put in the work.",
        "To get there.",
        "Luck is just.",
        "A combination of hard work.",
        "And good timing.",
        "So…",
        "don't allow anyone.",
        "To diminish your hard work.",
    ]
    # Clean cuts between scenes; only the last scene fades.
    assert [s["fade_out"] for s in scenes] == [False] * 8 + [True]


def test_rhythm_output_is_deterministic():
    a = synthesize_phrase_timings(_GOLDEN_QUOTE, video_duration_s=_GOLDEN_DURATION_S)
    b = synthesize_phrase_timings(_GOLDEN_QUOTE, video_duration_s=_GOLDEN_DURATION_S)
    assert a == b
    assert a  # guard against vacuous pass
    assert rhythm_scenes(_GOLDEN_QUOTE, video_duration_s=_GOLDEN_DURATION_S) == rhythm_scenes(
        _GOLDEN_QUOTE, video_duration_s=_GOLDEN_DURATION_S
    )


def test_sentence_split_covers_all_terminal_punctuation():
    # . ! ? and … all end sentences; each sentence gets an equal slot.
    text = "Stop right there! Are you sure? Maybe… I am sure. truly sure"
    timings = synthesize_phrase_timings(text, video_duration_s=20.0)
    window_start, window_end = RHYTHM_LEAD_IN_S, 20.0 - RHYTHM_TAIL_S
    slot_s = (window_end - window_start) / 5  # 5 sentences
    first_words = ["Stop", "Are", "Maybe…", "I", "truly"]
    firsts = [t for t in timings if t["word"] in first_words]
    assert [t["word"] for t in firsts] == first_words
    for i, t in enumerate(firsts):
        assert t["start_s"] == pytest.approx(window_start + i * slot_s, abs=1e-3)


def test_sentence_split_requires_whitespace_after_punctuation():
    # "U.S. style" abbreviations mid-token don't split ("3.5" has no
    # punct-then-whitespace boundary); only punct followed by whitespace does.
    timings = synthesize_phrase_timings("It cost 3.5 dollars. Worth it.", video_duration_s=12.0)
    words = [t["word"] for t in timings]
    assert words == ["It", "cost", "3.5", "dollars.", "Worth", "it."]
    # Two sentences → second slot starts at the window midpoint.
    window_start, window_end = RHYTHM_LEAD_IN_S, 12.0 - RHYTHM_TAIL_S
    worth = next(t for t in timings if t["word"] == "Worth")
    assert worth["start_s"] == pytest.approx((window_start + window_end) / 2, abs=1e-3)


def test_longer_words_get_longer_spans():
    timings = synthesize_phrase_timings(
        "a combination of extraordinary luck", video_duration_s=10.0
    )
    spans = {t["word"]: t["end_s"] - t["start_s"] for t in timings}
    assert spans["extraordinary"] > spans["combination"] > spans["of"]
    # Min char weight: "a" (1 char) is padded to weight 2, same as "of" (2 chars).
    assert spans["a"] == pytest.approx(spans["of"], abs=2e-3)


def test_words_fill_speak_frac_of_each_slot():
    text = "One sentence here. Another sentence follows."
    timings = synthesize_phrase_timings(text, video_duration_s=10.0)
    window_start, window_end = RHYTHM_LEAD_IN_S, 10.0 - RHYTHM_TAIL_S
    slot_s = (window_end - window_start) / 2
    # First sentence: starts at window start, last word ends at slot * SPEAK_FRAC.
    assert timings[0]["start_s"] == pytest.approx(window_start, abs=1e-3)
    assert timings[2]["end_s"] == pytest.approx(window_start + slot_s * RHYTHM_SPEAK_FRAC, abs=1e-3)
    # Air after each sentence exceeds PAUSE_GAP_S → pause-splits even without punct.
    air_s = slot_s * (1 - RHYTHM_SPEAK_FRAC)
    assert air_s > PAUSE_GAP_S
    # Words within a sentence are contiguous (no intra-sentence pause splits).
    assert timings[1]["start_s"] == pytest.approx(timings[0]["end_s"], abs=2e-3)


@pytest.mark.parametrize("text", ["", "   ", "\n\t", None])
def test_rhythm_degenerate_text_returns_empty(text):
    assert synthesize_phrase_timings(text, video_duration_s=10.0) == []
    assert rhythm_scenes(text, video_duration_s=10.0) == []


@pytest.mark.parametrize("duration", [0.0, -1.0, 1.0, RHYTHM_LEAD_IN_S + RHYTHM_TAIL_S + 0.5])
def test_rhythm_too_short_video_returns_empty(duration):
    assert synthesize_phrase_timings(_GOLDEN_QUOTE, video_duration_s=duration) == []
    assert rhythm_scenes(_GOLDEN_QUOTE, video_duration_s=duration) == []


def test_rhythm_single_sentence_is_ok():
    timings = synthesize_phrase_timings("Luck is just hard work.", video_duration_s=8.0)
    assert [t["word"] for t in timings] == ["Luck", "is", "just", "hard", "work."]
    assert timings[0]["start_s"] == pytest.approx(RHYTHM_LEAD_IN_S, abs=1e-3)
    speak_end = RHYTHM_LEAD_IN_S + (8.0 - RHYTHM_TAIL_S - RHYTHM_LEAD_IN_S) * RHYTHM_SPEAK_FRAC
    assert timings[-1]["end_s"] == pytest.approx(speak_end, abs=1e-3)
    scenes = rhythm_scenes("Luck is just hard work.", video_duration_s=8.0)
    assert len(scenes) == 1
    assert scenes[0]["words"] == ["Luck", "is", "just", "hard", "work."]


def test_synthesized_timings_shape_feeds_split_phrases():
    """Shape contract: {"word", "start_s", "end_s"} rounded to 3 decimals, with
    non-decreasing starts — exactly what split_phrases consumes. rhythm_scenes
    must equal manual split_phrases over the synthesized timings."""
    timings = synthesize_phrase_timings(_GOLDEN_QUOTE, video_duration_s=_GOLDEN_DURATION_S)
    assert timings
    for t in timings:
        assert set(t) == {"word", "start_s", "end_s"}
        assert t["start_s"] == round(t["start_s"], 3)
        assert t["end_s"] == round(t["end_s"], 3)
        assert t["start_s"] < t["end_s"]
    starts = [t["start_s"] for t in timings]
    assert starts == sorted(starts)
    scenes = split_phrases(timings, video_duration_s=_GOLDEN_DURATION_S)
    assert scenes == rhythm_scenes(_GOLDEN_QUOTE, video_duration_s=_GOLDEN_DURATION_S)
    for scene in scenes:
        assert set(scene) == _SCENE_KEYS
        assert len(scene["word_roles"]) == len(scene["words"])
        assert scene["word_roles"].count("hero") == 1


def test_rhythm_scene_windows_never_overlap():
    scenes = rhythm_scenes(_GOLDEN_QUOTE, video_duration_s=_GOLDEN_DURATION_S)
    assert len(scenes) >= 2
    for cur, nxt in zip(scenes, scenes[1:]):
        assert cur["end_s"] > cur["start_s"]
        assert nxt["start_s"] - cur["end_s"] >= MIN_SCENE_GAP_S - 1e-3
    assert scenes[-1]["end_s"] <= _GOLDEN_DURATION_S + 1e-9


# -- scenes_total_window ------------------------------------------------------------


def test_scenes_total_window():
    assert scenes_total_window([]) == (0.0, 0.0)
    scenes = split_phrases(
        [
            _w("we", 0.1, 0.3),
            _w("waited", 0.4, 0.8),
            _w("for", 1.2, 1.5),
            _w("this", 1.6, 2.0),
        ],
        video_duration_s=3.5,
    )
    first, last = scenes_total_window(scenes)
    assert first == scenes[0]["start_s"] == 0.0  # scene-0 clamp included
    assert last == scenes[-1]["end_s"]


# -- splitter single-source (the agent validator must see what the engine sees) --


@pytest.mark.parametrize(
    "quote",
    [
        "Work hard.No breaks.Stay sharp.Win big now.",  # no space after period
        "Save 3.5 hours. Every single day. That is real.",  # decimal mid-sentence
        'It\'s not "just luck". The work. Hard work pays.',  # quoted span
        "So… don't stop. Keep going. You got this.",  # ellipsis run
    ],
)
def test_split_sentences_is_the_single_source_for_engine_and_agent(quote):
    """`split_sentences` is the one splitter both the rhythm engine and the
    sequence-quote agent's validator use, so a quote's agent-side sentence count
    is exactly the sentence basis the engine builds scenes from — they can never
    diverge (the regex-divergence corruption class where the agent over-counts a
    decimal/no-space quote and the engine silently glues it on screen)."""
    from app.agents.sequence_quote_writer import split_quote_sentences

    assert split_quote_sentences(quote) == split_sentences(quote)
    # A well-formed quote (4 spaced sentences) yields one scene per sentence
    # before the 6-word cap can split further; a malformed one collapses to <4
    # for BOTH, so the agent's >=4 gate rejects it rather than gluing on screen.
    sentences = split_sentences(quote)
    scenes = rhythm_scenes(quote, video_duration_s=10.4)
    if len(sentences) >= MIN_SPEECH_WORDS // 1 and all(
        len(s.split()) <= MAX_PHRASE_WORDS for s in sentences
    ):
        assert len(scenes) == len(sentences)
