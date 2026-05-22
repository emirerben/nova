"""Unit tests for stage D: phrase reconstruction.

The end-of-file integration test replays the real OCR ground truth captured
during the investigation of template fdaf3bbc-2f4f-43bc-ba7c-e5cd819de102
(`/Users/emirerben/.claude/plans/template-text-overlay-layer-2-architecture.md`
"Smoke-test evidence" section). The current Gemini-only agent drops half
the words on this template; the test asserts that stages C+D together
produce the seven phrases a human reads on the screen.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.text_overlay_pipeline import Phrase
from app.pipeline.text_overlay_v2.grouping import group_detections_into_events
from app.pipeline.text_overlay_v2.phrases import (
    DEFAULT_X_BAND_THRESHOLD,
    reconstruct_phrases,
)

from .conftest import make_detection


def _events(*specs):
    """Build a list of events directly from (text, start, end, x, y) tuples.

    Skips the grouping stage for phrase-only tests. Each spec is
    (text, start_t_s, end_t_s, x_center, y_center).
    """
    from app.agents._schemas.text_overlay_pipeline import TextEvent

    return [
        TextEvent(
            text=text,
            start_t_s=start,
            end_t_s=end,
            aabb=(x - 0.1, y - 0.025, x + 0.1, y + 0.025),
            mean_confidence=1.0,
        )
        for text, start, end, x, y in specs
    ]


# ── Boundary cases ────────────────────────────────────────────────────────────


def test_empty_input_returns_empty():
    assert reconstruct_phrases([]) == []


def test_single_event_becomes_one_phrase():
    events = _events(("Hello", 1.0, 2.0, 0.5, 0.5))
    phrases = reconstruct_phrases(events)
    assert len(phrases) == 1
    assert phrases[0].lines == ["Hello"]
    assert phrases[0].sample_text == "Hello"
    assert phrases[0].start_t_s == 1.0
    assert phrases[0].end_t_s == 2.0


# ── Time / space merging ──────────────────────────────────────────────────────


def test_simultaneous_events_same_x_merge_into_one_phrase_y_sorted():
    # A two-line headline both appearing at t=0.0, one above the other.
    events = _events(
        ("Bottom line", 0.0, 2.0, 0.5, 0.55),
        ("Top line", 0.0, 2.0, 0.5, 0.45),
    )
    phrases = reconstruct_phrases(events)
    assert len(phrases) == 1
    # Y-sorted top to bottom
    assert phrases[0].lines == ["Top line", "Bottom line"]


def test_time_disjoint_events_become_separate_phrases():
    # Phrase 1 ends at 2.0; phrase 2 starts at 2.5 → no time overlap.
    events = _events(
        ("First", 0.0, 2.0, 0.5, 0.5),
        ("Second", 2.5, 4.0, 0.5, 0.5),
    )
    phrases = reconstruct_phrases(events)
    assert len(phrases) == 2
    assert phrases[0].sample_text == "First"
    assert phrases[1].sample_text == "Second"


def test_time_overlapping_but_x_disjoint_events_stay_separate():
    # A hook at center and a watermark at the corner overlap in time but
    # not in X. They should NOT merge into one phrase.
    events = _events(
        ("Hero caption", 0.0, 5.0, 0.5, 0.5),
        ("@handle", 0.0, 5.0, 0.1, 0.95),
    )
    phrases = reconstruct_phrases(events)
    assert len(phrases) == 2
    texts = {p.sample_text for p in phrases}
    assert texts == {"Hero caption", "@handle"}


def test_build_up_cascade_merges_into_one_phrase():
    # Classic build-up: "if" appears, then "you" while "if" is still visible,
    # then "put in" while both still visible. All same X-band.
    events = _events(
        ("if", 0.0, 3.0, 0.5, 0.45),
        ("you", 1.0, 3.0, 0.5, 0.50),
        ("put in", 2.0, 3.0, 0.5, 0.55),
    )
    phrases = reconstruct_phrases(events)
    assert len(phrases) == 1
    assert phrases[0].lines == ["if", "you", "put in"]
    assert phrases[0].sample_text == "if\nyou\nput in"
    assert phrases[0].start_t_s == 0.0
    assert phrases[0].end_t_s == 3.0


def test_phrase_reset_after_screen_clear():
    # Phrase 1 (3 lines build up), screen clears, phrase 2 (1 line).
    events = _events(
        ("It's", 0.0, 2.0, 0.5, 0.45),
        ("not", 0.5, 2.0, 0.5, 0.50),
        ("just luck", 1.0, 2.0, 0.5, 0.55),
        ("if", 2.5, 4.0, 0.5, 0.50),
    )
    phrases = reconstruct_phrases(events)
    assert len(phrases) == 2
    assert phrases[0].lines == ["It's", "not", "just luck"]
    assert phrases[1].lines == ["if"]


def test_phrase_aabb_is_union_of_constituent_events():
    events = _events(
        ("Top", 0.0, 1.0, 0.5, 0.4),
        ("Bottom", 0.0, 1.0, 0.5, 0.6),
    )
    phrases = reconstruct_phrases(events)
    assert len(phrases) == 1
    x_min, y_min, x_max, y_max = phrases[0].aabb
    assert x_min == pytest.approx(0.4)
    assert x_max == pytest.approx(0.6)
    assert y_min == pytest.approx(0.375)
    assert y_max == pytest.approx(0.625)


def test_phrase_confidence_is_event_weighted_mean():
    # Two events, mean confidences 0.8 and 1.0 → phrase mean 0.9.
    from app.agents._schemas.text_overlay_pipeline import TextEvent

    events = [
        TextEvent(
            text="A",
            start_t_s=0.0,
            end_t_s=1.0,
            aabb=(0.4, 0.4, 0.6, 0.5),
            mean_confidence=0.8,
        ),
        TextEvent(
            text="B",
            start_t_s=0.0,
            end_t_s=1.0,
            aabb=(0.4, 0.5, 0.6, 0.6),
            mean_confidence=1.0,
        ),
    ]
    phrases = reconstruct_phrases(events)
    assert phrases[0].mean_confidence == pytest.approx(0.9)


def test_output_sorted_by_start_t_s():
    # Late phrase first in input, early phrase second.
    events = _events(
        ("Late", 5.0, 6.0, 0.5, 0.5),
        ("Early", 0.0, 1.0, 0.5, 0.5),
    )
    phrases = reconstruct_phrases(events)
    assert phrases[0].sample_text == "Early"
    assert phrases[1].sample_text == "Late"


def test_x_band_threshold_is_tunable():
    # Two events whose X-centers are 0.25 apart.
    events = _events(
        ("Left", 0.0, 2.0, 0.4, 0.5),
        ("Right", 0.0, 2.0, 0.65, 0.5),
    )
    # Default threshold (0.15) keeps them separate.
    assert len(reconstruct_phrases(events)) == 2
    # Looser threshold (0.30) merges them.
    assert len(reconstruct_phrases(events, x_band_threshold=0.30)) == 1


def test_event_picks_spatially_closest_phrase_when_multiple_eligible():
    # Two phrases active simultaneously, far enough apart in X to stay
    # separate (P1 at 0.3, P2 at 0.7 — distance 0.4 exceeds threshold 0.30).
    # A third event lands closer to P2 (0.15) than to P1 (0.25); both
    # within the 0.30 threshold so both are eligible. Tiebreaker picks P2.
    events = _events(
        ("P1 a", 0.0, 5.0, 0.30, 0.45),
        ("P2 a", 0.0, 5.0, 0.70, 0.45),
        ("Joiner", 1.0, 2.0, 0.55, 0.55),
    )
    phrases = reconstruct_phrases(events, x_band_threshold=0.30)
    # Two starting phrases, three total events distributed by tiebreaker
    assert len(phrases) == 2
    joiner_phrase = next(p for p in phrases if "Joiner" in p.lines)
    assert "P2 a" in joiner_phrase.lines
    assert "P1 a" not in joiner_phrase.lines


def test_phrase_continues_through_event_overlap_chain():
    # A → B (A still up) → C (B still up, A ended)
    # All three should be one phrase even though A doesn't overlap C directly.
    events = _events(
        ("A", 0.0, 2.0, 0.5, 0.4),
        ("B", 1.5, 4.0, 0.5, 0.5),
        ("C", 3.0, 5.0, 0.5, 0.6),
    )
    phrases = reconstruct_phrases(events)
    assert len(phrases) == 1
    assert phrases[0].lines == ["A", "B", "C"]
    assert phrases[0].end_t_s == 5.0


def test_module_default_matches_phrases_module():
    assert DEFAULT_X_BAND_THRESHOLD == 0.15


def test_output_phrases_satisfy_schema_constraints():
    events = _events(("Hi", 0.0, 1.0, 0.5, 0.5))
    phrases = reconstruct_phrases(events)
    for p in phrases:
        assert isinstance(p, Phrase)
        assert p.lines
        assert p.start_t_s >= 0.0
        assert p.end_t_s >= p.start_t_s
        x0, y0, x1, y1 = p.aabb
        assert 0.0 <= x0 <= x1 <= 1.0
        assert 0.0 <= y0 <= y1 <= 1.0
        assert 0.0 <= p.mean_confidence <= 1.0


# ── Integration: full pipeline on real OCR ground truth ───────────────────────


def test_full_pipeline_on_not_just_luck_ocr_ground_truth():
    """End-to-end stages C+D on the real OCR ground truth from the
    investigation of template fdaf3bbc-... .

    Apple Vision per-frame OCR (captured at 0.5s intervals in
    `/tmp/frames/`) produced these detections. The current Gemini-only
    agent returns 10-12 fragmented overlays with half the words missing.
    Stages C+D together should produce SEVEN phrases — one per
    build-up sequence visible on screen — matching what a human reads.

    Source quote (from the voiceover):
      "it's not just luck if you put in the work to get there.
       luck is just a combination of hard work and good timing so...
       don't allow anyone to diminish your hard work."

    Observed phrase progression in the video:
      0.0-2.0s: "It's / not / just luck"
      2.3-4.5s: "if / you / put in"
      4.8-7.0s: "the / work / to get / there"
      7.0-7.4s: "luck / is just / a"           (no clear 7.3 frame)
      7.8-8.2s: "combination / of"
      8.3-9.2s: "and / good timing / so..."
      9.3-10.0s: "don't / allow anyone / to diminish"

    The test fixture below replays the per-frame OCR (sampled at 0.5s
    spacing matching `ffmpeg -ss N`). Each tuple is
    (frame_t_s, text, y_center). All X centers are ~0.5 (center column).
    """
    # Construct the detections from the captured OCR transcript.
    # Stack Y positions are approximate: each new line in the build-up
    # appears about 0.05 below the previous one.
    detections = []
    Y_BASE = 0.45  # first line of any phrase appears around y=0.45

    # Phrase 1: "It's / not / just luck"
    for t in [0.3, 0.8, 1.3, 1.8]:
        detections.append(make_detection(t, "It's", y_center=Y_BASE))
    for t in [1.3, 1.8]:
        detections.append(make_detection(t, "not", y_center=Y_BASE + 0.05))
    for t in [1.8]:
        detections.append(make_detection(t, "just luck", y_center=Y_BASE + 0.10))

    # Phrase 2: "if / you / put in"
    for t in [2.3, 2.8, 3.3, 3.8, 4.3]:
        detections.append(make_detection(t, "if", y_center=Y_BASE))
    for t in [3.3, 3.8, 4.3]:
        detections.append(make_detection(t, "you", y_center=Y_BASE + 0.05))
    for t in [3.8, 4.3]:
        detections.append(make_detection(t, "put in", y_center=Y_BASE + 0.10))

    # Phrase 3: "The / work / to get / there"  (script "the" → "The" in OCR)
    for t in [4.8, 5.3, 5.8, 6.3]:
        detections.append(make_detection(t, "The", y_center=Y_BASE))
    for t in [5.3, 5.8, 6.3]:
        detections.append(make_detection(t, "work", y_center=Y_BASE + 0.05))
    for t in [5.8, 6.3]:
        detections.append(make_detection(t, "to get", y_center=Y_BASE + 0.10))
    for t in [5.8, 6.3]:
        detections.append(make_detection(t, "there", y_center=Y_BASE + 0.15))

    # Phrase 4: "luck / is just / a"  (only one captured frame; phrase is brief)
    for t in [6.8]:
        detections.append(make_detection(t, "luck", y_center=Y_BASE))
        detections.append(make_detection(t, "is just", y_center=Y_BASE + 0.05))
        detections.append(make_detection(t, "a", y_center=Y_BASE + 0.10))

    # Phrase 5: "combination / of"
    for t in [7.8]:
        detections.append(make_detection(t, "combination", y_center=Y_BASE))
        detections.append(make_detection(t, "of", y_center=Y_BASE + 0.05))

    # Phrase 6: "and / good timing / so..."
    for t in [8.3, 8.8]:
        detections.append(make_detection(t, "and", y_center=Y_BASE))
    for t in [8.8]:
        detections.append(make_detection(t, "good timing", y_center=Y_BASE + 0.05))
        detections.append(make_detection(t, "so...", y_center=Y_BASE + 0.10))

    # Phrase 7: "don't / allow anyone / to diminish"
    for t in [9.3, 9.8]:
        detections.append(make_detection(t, "don't", y_center=Y_BASE))
    for t in [9.8]:
        detections.append(make_detection(t, "allow anyone", y_center=Y_BASE + 0.05))
        detections.append(make_detection(t, "to diminish", y_center=Y_BASE + 0.10))

    events = group_detections_into_events(detections)
    phrases = reconstruct_phrases(events)

    # Seven phrases, matching the human-readable progression.
    assert len(phrases) == 7, [p.sample_text for p in phrases]

    expected = [
        "It's\nnot\njust luck",
        "if\nyou\nput in",
        "The\nwork\nto get\nthere",
        "luck\nis just\na",
        "combination\nof",
        "and\ngood timing\nso...",
        "don't\nallow anyone\nto diminish",
    ]
    actual = [p.sample_text for p in phrases]
    assert actual == expected, f"mismatch:\nexpected={expected}\nactual={actual}"

    # Phrases are temporally ordered.
    for a, b in zip(phrases, phrases[1:]):
        assert a.start_t_s <= b.start_t_s


# ── atomize_per_event mode (used by the word-by-word pipeline) ────────────────


def test_atomize_per_event_produces_one_phrase_per_event():
    # Two events that overlap in time AND share an x-band. Default mode merges
    # them into one two-line phrase; atomize_per_event keeps them separate.
    # "if" lives 0.0-1.0 (3 frames); "you" appears at 0.5 while "if" is still up.
    detections = [
        make_detection(0.0, "if", x_center=0.5, y_center=0.45),
        make_detection(0.5, "if", x_center=0.5, y_center=0.45),
        make_detection(1.0, "if", x_center=0.5, y_center=0.45),
        make_detection(0.5, "you", x_center=0.5, y_center=0.50),
        make_detection(1.0, "you", x_center=0.5, y_center=0.50),
    ]
    events = group_detections_into_events(detections)
    assert len(events) == 2

    # Default mode: time-overlapping same-x-band events merge into one phrase.
    default_phrases = reconstruct_phrases(events)
    assert len(default_phrases) == 1
    assert default_phrases[0].sample_text == "if\nyou"

    # Atomize mode keeps each event as its own one-line phrase.
    atomic_phrases = reconstruct_phrases(events, atomize_per_event=True)
    assert len(atomic_phrases) == 2
    assert [p.sample_text for p in atomic_phrases] == ["if", "you"]
    # Each phrase preserves its event's own start/end times — no merging.
    assert atomic_phrases[0].start_t_s == 0.0
    assert atomic_phrases[0].end_t_s == 1.0
    assert atomic_phrases[1].start_t_s == 0.5
    assert atomic_phrases[1].end_t_s == 1.0


def test_atomize_per_event_keeps_same_line_words_separate():
    # Same-line same-time words (e.g. "if you" appearing as two word events at
    # the same frame) would normally cluster into one multi-line phrase by
    # x-band. Atomize keeps them separate so each word has its own overlay.
    detections = [
        # Two events at t=1.0, same y, different x — same horizontal line.
        make_detection(1.0, "if", x_center=0.42, y_center=0.5),
        make_detection(1.0, "you", x_center=0.58, y_center=0.5),
    ]
    events = group_detections_into_events(detections)
    atomic_phrases = reconstruct_phrases(events, atomize_per_event=True)
    assert len(atomic_phrases) == 2
    assert {p.sample_text for p in atomic_phrases} == {"if", "you"}
    # Both start at the same time (same frame appearance).
    assert all(p.start_t_s == 1.0 for p in atomic_phrases)


def test_atomize_per_event_empty_input_returns_empty():
    assert reconstruct_phrases([], atomize_per_event=True) == []


# ── Dedup of duplicate events within one phrase ───────────────────────────────
#
# Regression for prod job 87b7292b-3913-40b9-844f-835f7544063b: a phrase
# rendered as "if you if you put put in" because Stage D collected duplicate
# events (same OCR text re-detected as the template animated) into the same
# phrase's `lines` list with no dedup. After the fix, _finalize collapses
# same-text events by casefolded key.


def test_finalize_dedups_adjacent_duplicate_events():
    # Three events all reading "if" at the same Y (same on-screen line) but
    # different X positions — same word re-detected as the template animates.
    events = _events(
        ("if", 0.0, 1.0, 0.4, 0.5),
        ("if", 0.0, 1.0, 0.45, 0.5),
        ("if", 0.0, 1.0, 0.5, 0.5),
    )
    phrases = reconstruct_phrases(events)
    assert len(phrases) == 1
    assert phrases[0].lines == ["if"]
    assert phrases[0].sample_text == "if"


def test_finalize_dedups_non_adjacent_duplicates():
    # "if", "you", "if" — same word at two non-adjacent y-positions.
    # Dedup must catch the second "if" even though "you" is between them.
    events = _events(
        ("if", 0.0, 1.0, 0.5, 0.40),
        ("you", 0.0, 1.0, 0.5, 0.50),
        ("if", 0.0, 1.0, 0.5, 0.60),
    )
    phrases = reconstruct_phrases(events)
    assert len(phrases) == 1
    # Y-sort puts top-most first: if (0.40), you (0.50), then DROP duplicate if (0.60).
    assert phrases[0].lines == ["if", "you"]


def test_finalize_dedup_is_case_insensitive():
    events = _events(
        ("The", 0.0, 1.0, 0.5, 0.45),
        ("THE", 0.0, 1.0, 0.5, 0.55),
    )
    phrases = reconstruct_phrases(events)
    assert len(phrases) == 1
    # Casefolded dedup keeps the first occurrence's original casing.
    assert phrases[0].lines == ["The"]


def test_finalize_preserves_distinct_words_in_y_order():
    # Sanity: dedup does not collapse legitimately different words.
    events = _events(
        ("Top", 0.0, 1.0, 0.5, 0.40),
        ("Middle", 0.0, 1.0, 0.5, 0.50),
        ("Bottom", 0.0, 1.0, 0.5, 0.60),
    )
    phrases = reconstruct_phrases(events)
    assert phrases[0].lines == ["Top", "Middle", "Bottom"]


# ── Cross-phrase dedup in atomized mode ───────────────────────────────────────
#
# Regression for prod job 673d26d7-edbf-43a8-ac58-50dd604baae0 on template
# fdaf3bbc-2f4f-43bc-ba7c-e5cd819de102 (Not Just Luck, 2026-05-20). Atomized
# output contained "the" twice with overlapping intervals, "to" three times
# all at 5.5-6.0, "combination" three times spanning 8.0-9.5, and "and" five
# times in the 9.5-10.5 window. Renderer stacked all 21 overlays
# center-positioned. dedup_overlapping_atomized_phrases collapses these
# OCR re-detections back into one phrase per visible word.


def test_atomize_dedups_overlapping_same_text_phrases():
    # Two events with identical text whose time intervals overlap.
    # Atomized mode produces two phrases; cross-phrase dedup collapses them.
    events = _events(
        ("the", 4.5, 5.5, 0.5, 0.5),
        ("the", 5.0, 6.0, 0.5, 0.5),
    )
    phrases = reconstruct_phrases(events, atomize_per_event=True)
    assert len(phrases) == 1
    assert phrases[0].sample_text == "the"
    # Merged interval covers the union of both inputs.
    assert phrases[0].start_t_s == 4.5
    assert phrases[0].end_t_s == 6.0


def test_atomize_dedups_three_identical_overlapping_phrases():
    # Three "to" phrases at identical [5.5, 6.0] — the production "to/get/to"
    # pattern with one neighboring distinct word in between in time-sorted
    # order. The "get" must survive; the three "to"s collapse to one.
    events = _events(
        ("to", 5.5, 6.0, 0.5, 0.5),
        ("get", 5.5, 6.0, 0.4, 0.5),
        ("to", 5.5, 6.0, 0.6, 0.5),
        ("to", 5.5, 6.0, 0.5, 0.5),
    )
    phrases = reconstruct_phrases(events, atomize_per_event=True)
    sample_texts = sorted(p.sample_text for p in phrases)
    assert sample_texts == ["get", "to"], (
        f"three identical 'to' phrases must collapse to one alongside 'get'; got {sample_texts}"
    )


def test_atomize_preserves_legitimate_repeat_with_gap():
    # Same text said twice with a gap larger than the dedup threshold —
    # a real refrain or beat-synced re-display. Both must survive.
    events = _events(
        ("rain", 0.0, 1.0, 0.5, 0.5),
        ("rain", 3.0, 4.0, 0.5, 0.5),  # 2s gap, well above ATOMIZED_DEDUP_GAP_THRESHOLD_S
    )
    phrases = reconstruct_phrases(events, atomize_per_event=True)
    assert len(phrases) == 2
    assert [p.start_t_s for p in phrases] == [0.0, 3.0]
    assert [p.sample_text for p in phrases] == ["rain", "rain"]


def test_atomize_dedup_preserves_distinct_overlapping_words():
    # Two overlapping events with different text must NOT collapse.
    # Guards against the dedup over-reaching.
    events = _events(
        ("if", 0.0, 1.0, 0.4, 0.5),
        ("you", 0.5, 1.5, 0.6, 0.5),
    )
    phrases = reconstruct_phrases(events, atomize_per_event=True)
    assert len(phrases) == 2
    assert sorted(p.sample_text for p in phrases) == ["if", "you"]


def test_atomize_dedup_collapses_full_not_just_luck_run():
    # Exact replay of the prod 21-overlay output. Expected after dedup:
    # 1× "the" (4.5-6.0), 1× "work" (5.0-6.0), 1× "to" (5.5-6.0),
    # 1× "get" (5.5-6.0), 1× "luck" (6.5-8.0), 1× "combination" (8.0-9.5),
    # 1× "and" (9.5-10.5), 1× "don't" (10.0-10.5).
    # 8 distinct words from 11 input events; the missing rows below mirror
    # the prod set, abbreviated to keep the test focused.
    events = _events(
        ("the", 4.5, 5.5, 0.5, 0.5),
        ("the", 5.0, 6.0, 0.5, 0.5),
        ("work", 5.0, 6.0, 0.4, 0.5),
        ("to", 5.5, 6.0, 0.5, 0.5),
        ("get", 5.5, 6.0, 0.45, 0.5),
        ("to", 5.5, 6.0, 0.55, 0.5),
        ("luck", 6.5, 7.0, 0.5, 0.5),
        ("luck", 6.5, 8.0, 0.5, 0.5),
        ("combination", 8.0, 8.5, 0.5, 0.5),
        ("combination", 8.5, 9.0, 0.5, 0.5),
        ("combination", 8.5, 9.5, 0.5, 0.5),
        ("and", 9.5, 10.0, 0.5, 0.5),
        ("and", 10.0, 10.5, 0.5, 0.5),
        ("don't", 10.0, 10.5, 0.5, 0.5),
    )
    phrases = reconstruct_phrases(events, atomize_per_event=True)
    texts_by_start = [(p.start_t_s, p.sample_text) for p in phrases]
    # Each distinct word appears exactly once; total count drops from 14 → 8.
    assert len(phrases) == 8, (
        f"expected 8 phrases after dedup, got {len(phrases)}: {texts_by_start}"
    )
    sample_texts = sorted(p.sample_text for p in phrases)
    assert sample_texts == [
        "and",
        "combination",
        "don't",
        "get",
        "luck",
        "the",
        "to",
        "work",
    ]
