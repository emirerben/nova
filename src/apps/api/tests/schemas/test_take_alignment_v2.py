"""KRI-466: TakeAlignment gained method + match range; v1 rows must keep parsing."""

import pytest
from pydantic import ValidationError

from app.schemas.user_song import (
    SONG_ALIGNMENT_VERSION,
    PlacementCandidate,
    SongAlignment,
    TakeAlignment,
    UserSongTake,
)


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
    assert SONG_ALIGNMENT_VERSION == 3
    assert SongAlignment(song_generation=1).version == 3


def test_v3_fields_are_omitted_when_unset():
    dumped = TakeAlignment(media_id="a", status="unmatched").model_dump(mode="json")
    assert not {"likelihood", "margin", "candidates"} & set(dumped)
    take = UserSongTake(delta_s=1.0).model_dump(mode="json")
    assert "likelihood" not in take and "position_basis" not in take


def test_v3_candidates_round_trip_and_win_over_legacy_fields():
    row = TakeAlignment(
        media_id="a",
        status="ambiguous",
        delta_s=5.0,
        likelihood=0.8,
        margin=0.0,
        candidates=[
            PlacementCandidate(delta_s=5.0, likelihood=0.8, method="both", matched_words=6),
            PlacementCandidate(delta_s=9.0, likelihood=0.8, method="lyrics"),
        ],
    )
    again = TakeAlignment.model_validate(row.model_dump(mode="json"))
    assert [c.delta_s for c in again.candidates_or_legacy()] == [5.0, 9.0]
    assert again.likelihood == 0.8 and again.margin == 0.0


def test_candidate_rejects_unknown_fields_and_out_of_range_likelihood():
    with pytest.raises(ValidationError):
        PlacementCandidate(delta_s=1.0, likelihood=1.5, method="audio")
    with pytest.raises(ValidationError):
        PlacementCandidate.model_validate(
            {"delta_s": 1.0, "likelihood": 0.5, "method": "audio", "bogus": 1}
        )


def test_legacy_rows_synthesize_candidates():
    confident = TakeAlignment(media_id="a", status="confident", delta_s=3.0, confidence=0.7)
    assert [(c.delta_s, c.likelihood) for c in confident.candidates_or_legacy()] == [(3.0, 0.7)]
    bare = TakeAlignment(media_id="a", status="confident", delta_s=3.0)
    assert bare.candidates_or_legacy()[0].likelihood == 0.9
    ambiguous = TakeAlignment.model_validate(
        {
            "media_id": "a",
            "status": "ambiguous",
            "delta_s": 3.0,
            "confidence": 0.4,
            "alternates": [{"delta_s": 3.0, "score": 9}, {"delta_s": 8.0, "score": 9}],
        }
    )
    ties = ambiguous.candidates_or_legacy()
    assert [c.delta_s for c in ties] == [3.0, 8.0]
    assert len({c.likelihood for c in ties}) == 1
    assert TakeAlignment(media_id="a", status="unmatched").candidates_or_legacy() == []
