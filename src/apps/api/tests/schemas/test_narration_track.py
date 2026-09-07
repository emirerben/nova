import math

import pytest
from pydantic import ValidationError

from app.schemas.edit_proposal import NarrationTrack


def test_narration_track_forbids_extra_fields_and_non_finite_values() -> None:
    with pytest.raises(ValidationError):
        NarrationTrack(
            gcs_path="voiceover/a.m4a",
            generation="7",
            duration_s=2.0,
            words=[],
            unexpected="ignored",
        )
    with pytest.raises(ValidationError):
        NarrationTrack(gcs_path="voiceover/a.m4a", generation="7", duration_s=math.inf)


def test_narration_words_only_require_monotonic_starts() -> None:
    track = NarrationTrack(
        gcs_path="voiceover/a.m4a",
        generation="7",
        duration_s=2.0,
        words=[
            {"text": "one", "start_s": 0.0, "end_s": 0.8},
            # Whisper streams can overlap word end times slightly.
            {"text": "two", "start_s": 0.7, "end_s": 1.2},
        ],
    )
    assert len(track.words) == 2


def test_point_timestamps_preserve_words_in_adjacent_caption_cues():
    from types import SimpleNamespace

    from app.pipeline.guided_story import _narration_caption_elements

    track = NarrationTrack(
        gcs_path="voice",
        generation="1",
        duration_s=3,
        words=[
            {"text": "In", "start_s": 0, "end_s": 0.5},
            {"text": "Michigan.", "start_s": 0.5, "end_s": 0.5},
            {"text": "We", "start_s": 1, "end_s": 1},
            {"text": "played.", "start_s": 1, "end_s": 1.5},
        ],
    )
    cues = _narration_caption_elements(SimpleNamespace(narration=track, font_family=None))
    assert [cue["text"] for cue in cues] == ["In Michigan.", "We played."]
    assert [(cue["start_s"], cue["end_s"]) for cue in cues] == [(0, 0.5), (1, 1.5)]
    with pytest.raises(ValidationError):
        track.model_validate(
            {**track.model_dump(), "words": [{"text": "bad", "start_s": 1, "end_s": 0.5}]}
        )
