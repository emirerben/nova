from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from app.pipeline.guided_story import GuidedStoryError
from app.services.guided_narration_focus import (
    _contact_sheet,
    analyze_guided_participant_focus,
    sample_times,
)


def shot(kind="video"):
    return {
        "media_id": "one",
        "kind": kind,
        "gcs_path": "users/u/source.jpg",
        "generation": "7",
        "source_start_s": 2,
        "source_end_s": 4,
    }


def test_focus_samples_stay_inside_the_selected_source_window():
    assert sample_times(shot()) == [2.2, 3, 3.8]
    assert sample_times(shot("image")) == [2]


def test_photo_contact_sheet_preserves_aspect_and_names_evidence(tmp_path):
    source = tmp_path / "source.jpg"
    Image.new("RGB", (100, 200), "red").save(source)
    result = tmp_path / "sheet.jpg"
    frames = _contact_sheet(source, shot("image"), result)
    assert [(row.sample_id, row.source_time_s) for row in frames] == [("frame-1", 2)]
    with Image.open(result) as image:
        assert image.size == (480, 512)
        assert image.getpixel((240, 240))[0] > 200


def test_stale_focus_source_cannot_reach_the_visual_provider():
    with (
        patch("app.storage.object_metadata", return_value=SimpleNamespace(generation="8")),
        patch("app.services.guided_narration_focus.default_client") as client,
    ):
        with pytest.raises(GuidedStoryError, match="could not be checked"):
            analyze_guided_participant_focus([shot()], job_id="job")
    client.assert_not_called()
