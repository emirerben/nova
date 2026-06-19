from __future__ import annotations

from app.pipeline.narrated_alignment import (
    StepScript,
    align_script_to_voiceover,
)
from app.pipeline.transcribe import Word


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
