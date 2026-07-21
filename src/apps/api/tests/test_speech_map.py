"""speech_map + transcript_source speech branches — the copilot's word/pause vocabulary.

Pins the contracts the snapshot pipeline depends on:
  - pause derivation (inter-word gap ≥ 0.28s, leading silence ≥ 0.5s);
  - the coordinate invariant (assembled-timeline seconds, out-of-range dropped);
  - head-biased caps (early words survive — hook-window asks);
  - `speech_words_for_variant` precedence: persisted transcript first, caption
    cue words second, NEVER touching `words_from_variant` semantics (C19).
"""

from __future__ import annotations

from app.services.speech_map import (
    LEADING_PAUSE_MIN_S,
    PAUSE_MIN_GAP_S,
    PAUSES_CAP,
    WORDS_CAP,
    build_speech_map,
)
from app.services.transcript_source import (
    speech_words_for_variant,
    words_from_caption_cues,
    words_from_variant,
)


def _w(word: str, s: float, e: float) -> dict:
    return {"word": word, "start_s": s, "end_s": e}


class TestBuildSpeechMap:
    def test_pause_between_words_and_leading_silence(self) -> None:
        m = build_speech_map(
            [_w("hey", 0.8, 1.0), _w("so", 1.0 + PAUSE_MIN_GAP_S, 1.9), _w("fast", 1.95, 2.2)],
            30.0,
            "caption_words",
        )
        assert m is not None
        assert m["source"] == "caption_words"
        # Leading silence (0.8 ≥ LEADING_PAUSE_MIN_S) → pause with after=None.
        assert m["pauses"][0] == {"s": 0.0, "e": 0.8, "after": None}
        # Gap exactly at threshold counts; the 0.05s gap between "so"/"fast" does not.
        assert m["pauses"][1]["after"] == "hey"
        # Trailing silence (2.2 → 30.0) is a pause too — end-of-speech accents
        # need a mark ("punchline that ends the video").
        assert m["pauses"][2] == {"s": 2.2, "e": 30.0, "after": "fast"}
        assert len(m["pauses"]) == 3

    def test_short_leading_gap_is_not_a_pause(self) -> None:
        m = build_speech_map(
            [_w("hi", LEADING_PAUSE_MIN_S - 0.1, 1.0), _w("there", 1.05, 1.4)], 10.0, "x"
        )
        assert m is not None
        # Only the trailing-silence pause remains — the short leading gap and
        # the 0.05s inter-word gap don't qualify.
        assert m["pauses"] == [{"s": 1.4, "e": 10.0, "after": "there"}]

    def test_coordinate_invariant_drops_out_of_range_words(self) -> None:
        m = build_speech_map(
            [
                _w("good", 1.0, 1.3),
                _w("beyond-duration", 45.0, 45.5),
                _w("negative", -1.0, 0.5),
                _w("inverted", 2.0, 1.0),
                {"word": "nan", "start_s": float("nan"), "end_s": 2.0},
            ],
            10.0,
            "x",
        )
        assert m is not None
        assert [w["w"] for w in m["words"]] == ["good"]

    def test_none_when_no_usable_words(self) -> None:
        assert build_speech_map([], 10.0, "x") is None
        assert build_speech_map(None, 10.0, "x") is None
        assert build_speech_map([{"word": "", "start_s": 0, "end_s": 1}], 10.0, "x") is None

    def test_caps_are_head_biased(self) -> None:
        words = [_w(f"w{i}", i * 1.0, i * 1.0 + 0.2) for i in range(WORDS_CAP + 50)]
        m = build_speech_map(words, float(WORDS_CAP + 60), "x")
        assert m is not None
        assert len(m["words"]) == WORDS_CAP
        assert m["words"][0]["w"] == "w0"  # head survives, never strided
        assert len(m["pauses"]) <= PAUSES_CAP

    def test_words_sorted_by_start(self) -> None:
        m = build_speech_map([_w("b", 2.0, 2.2), _w("a", 0.6, 0.8)], 10.0, "x")
        assert m is not None
        assert [w["w"] for w in m["words"]] == ["a", "b"]


class TestSpeechWordsForVariant:
    def test_cue_words_branch(self) -> None:
        variant = {
            "caption_cues": [
                {
                    "text": "hello world",
                    "start_s": 0.0,
                    "end_s": 1.0,
                    "words": [
                        {"text": "hello", "start_s": 0.0, "end_s": 0.4},
                        {"text": "world", "start_s": 0.5, "end_s": 1.0},
                    ],
                }
            ]
        }
        got = speech_words_for_variant(variant)
        assert got is not None
        words, source = got
        assert source == "caption_words"
        assert [w["word"] for w in words] == ["hello", "world"]

    def test_persisted_transcript_wins_over_cue_words(self) -> None:
        variant = {
            "transcript": [{"word": "spoken", "start_s": 0.1, "end_s": 0.5}],
            "caption_cues": [
                {
                    "text": "x",
                    "start_s": 0,
                    "end_s": 1,
                    "words": [{"text": "cue", "start_s": 0.0, "end_s": 0.3}],
                }
            ],
        }
        got = speech_words_for_variant(variant)
        assert got is not None
        assert got[1] == "sequence_transcript"
        assert got[0][0]["word"] == "spoken"

    def test_overlay_transcript_labeled(self) -> None:
        variant = {"overlay_transcript": [{"word": "asr", "start_s": 0.1, "end_s": 0.5}]}
        got = speech_words_for_variant(variant)
        assert got is not None
        assert got[1] == "overlay_transcript"

    def test_none_when_no_source(self) -> None:
        assert speech_words_for_variant({}) is None
        assert (
            speech_words_for_variant({"caption_cues": [{"text": "x", "start_s": 0, "end_s": 1}]})
            is None
        )

    def test_malformed_cue_word_drops_whole_source(self) -> None:
        variant = {
            "caption_cues": [
                {
                    "text": "x",
                    "start_s": 0,
                    "end_s": 1,
                    "words": [{"text": "bad", "start_s": 1.0, "end_s": 0.2}],
                }
            ]
        }
        assert words_from_caption_cues(variant) is None

    def test_words_from_variant_untouched_by_cue_branch(self) -> None:
        # C19 guard: the cue-words branch must not leak into words_from_variant —
        # its `transcript` key presence doubles as a sequence-eligibility signal.
        variant = {
            "caption_cues": [
                {
                    "text": "x",
                    "start_s": 0,
                    "end_s": 1,
                    "words": [{"text": "cue", "start_s": 0.0, "end_s": 0.3}],
                }
            ]
        }
        assert words_from_variant(variant) is None
