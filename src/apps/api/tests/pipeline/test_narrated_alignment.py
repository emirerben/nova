from __future__ import annotations

from app.pipeline.narrated_alignment import (
    StepScript,
    align_script_to_voiceover,
    contiguous_step_timings,
)
from app.pipeline.transcribe import Word


def test_contiguous_step_timings_tile_full_voiceover_from_zero() -> None:
    """REGRESSION: narrated_ready captions led the audio because segment 0 began
    at the speech onset (leaving leading silence out), compressing the timeline.
    Contiguous timings must start at 0 and tile the FULL voiceover, so the
    assembled visual matches the voiceover's natural timing (captions stay synced)."""
    # speech starts at 2.24s (leading silence); buckets begin at 2.24, 13.92, 25.18.
    timings = contiguous_step_timings([2.24, 13.92, 25.18], timeline_end=40.5)

    assert [t.step_id for t in timings] == ["seg_0", "seg_1", "seg_2"]
    # seg 0 absorbs the leading silence — starts at 0, NOT 2.24 (the bug).
    assert timings[0].start_s == 0.0
    # contiguous: each segment starts where the previous ends, no gaps.
    for prev, nxt in zip(timings, timings[1:]):
        assert prev.end_s == nxt.start_s
    # boundaries sit at each next bucket's speech onset.
    assert timings[0].end_s == 13.92
    assert timings[1].end_s == 25.18
    # last segment runs to the full voiceover length (no audio truncation).
    assert timings[-1].end_s == 40.5
    # durations tile the whole timeline.
    assert abs(sum(t.end_s - t.start_s for t in timings) - 40.5) < 1e-6


def test_contiguous_step_timings_single_segment() -> None:
    timings = contiguous_step_timings([3.0], timeline_end=12.0)
    assert len(timings) == 1
    assert (timings[0].start_s, timings[0].end_s) == (0.0, 12.0)


def test_contiguous_timings_keep_captions_in_sync_with_audio() -> None:
    """End-to-end alignment: with full-voiceover timings, rebasing a spoken word
    onto the assembled timeline is the IDENTITY — the caption time equals the
    time the word is heard (no early-caption drift)."""
    from app.pipeline.narrated_assembler import _rebase_words_to_assembled

    timings = contiguous_step_timings([2.24, 13.92, 25.18], timeline_end=40.5)
    words = [
        Word("first", 2.24, 2.6, 1.0),  # first spoken word, after 2.24s silence
        Word("middle", 14.0, 14.4, 1.0),
        Word("last", 39.8, 40.2, 1.0),
    ]
    rebased = _rebase_words_to_assembled(words, timings)
    # caption times == spoken times (identity), so read == heard.
    assert [round(w.start_s, 2) for w in rebased] == [2.24, 14.0, 39.8]


def test_align_script_high_confidence_covers_voiceover_duration() -> None:
    steps = [
        StepScript(step_id="s1", text="open the app"),
        StepScript(step_id="s2", text="tap the profile"),
        StepScript(step_id="s3", text="save your changes"),
    ]
    words = [
        Word("open", 0.0, 0.2, 1.0),
        Word("the", 0.2, 0.35, 1.0),
        Word("app", 0.35, 0.8, 1.0),
        Word("tap", 0.8, 1.0, 1.0),
        Word("the", 1.0, 1.15, 1.0),
        Word("profile", 1.15, 1.7, 1.0),
        Word("save", 1.7, 1.95, 1.0),
        Word("your", 1.95, 2.1, 1.0),
        Word("changes", 2.1, 2.6, 1.0),
    ]

    timings = align_script_to_voiceover(steps, words)

    assert [t.step_id for t in timings] == ["s1", "s2", "s3"]
    assert all(t.confidence >= 0.9 for t in timings)
    total = sum(t.end_s - t.start_s for t in timings)
    assert abs(total - 2.6) <= 0.1
    assert [(t.start_s, t.end_s) for t in timings] == [(0.0, 0.8), (0.8, 1.7), (1.7, 2.6)]


def test_align_script_low_confidence_step_falls_back_to_even_split() -> None:
    steps = [
        StepScript(step_id="s1", text="open the app"),
        StepScript(step_id="s2", text="tap the profile"),
        StepScript(step_id="s3", text="save your changes"),
    ]
    words = [
        Word("open", 0.0, 0.2, 1.0),
        Word("the", 0.2, 0.35, 1.0),
        Word("app", 0.35, 0.8, 1.0),
        Word("totally", 0.8, 1.0, 1.0),
        Word("different", 1.0, 1.35, 1.0),
        Word("words", 1.35, 1.7, 1.0),
        Word("save", 1.7, 1.95, 1.0),
        Word("your", 1.95, 2.1, 1.0),
        Word("changes", 2.1, 2.7, 1.0),
    ]

    timings = align_script_to_voiceover(steps, words)

    assert timings[1].step_id == "s2"
    assert timings[1].confidence < 0.5
    assert timings[1].start_s == 0.9
    assert timings[1].end_s == 1.8
