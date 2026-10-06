"""KRI-466: TakeAlignment gained method + match range; v1 rows must keep parsing."""

import pytest
from pydantic import ValidationError

from app.schemas.user_song import SONG_ALIGNMENT_VERSION, SongAlignment, TakeAlignment


def test_v1_take_alignment_rows_still_parse():
    row = TakeAlignment.model_validate(
        {"media_id": "a", "status": "confident", "delta_s": 1.5, "confidence": 0.9}
    )
    assert row.method is None
    assert row.match_start_s is None and row.match_end_s is None


def test_new_take_fields_are_omitted_when_unset():
    dumped = TakeAlignment(media_id="a", status="unmatched").model_dump(mode="json")
    assert "method" not in dumped
    assert "match_start_s" not in dumped
    assert "match_end_s" not in dumped


def test_new_take_fields_round_trip_when_set():
    row = TakeAlignment(
        media_id="a",
        status="confident",
        delta_s=2.0,
        method="lyrics",
        match_start_s=1.0,
        match_end_s=6.0,
    )
    again = TakeAlignment.model_validate(row.model_dump(mode="json"))
    assert (again.method, again.match_start_s, again.match_end_s) == ("lyrics", 1.0, 6.0)


def test_match_range_must_be_positive():
    with pytest.raises(ValidationError):
        TakeAlignment(
            media_id="a", status="confident", delta_s=2.0, match_start_s=5.0, match_end_s=5.0
        )


def test_alignment_version_is_bumped_so_stale_rows_recompute():
    assert SONG_ALIGNMENT_VERSION == 2
    assert SongAlignment(song_generation=1).version == 2
