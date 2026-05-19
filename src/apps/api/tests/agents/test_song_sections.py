"""Unit tests for SongSectionsAgent.parse() — focuses on the cross-field
validation, version clamp, overlap drop, rank dedup, and refusal path.
"""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import RefusalError, SchemaError
from app.agents._schemas.song_sections import CURRENT_SECTION_VERSION
from app.agents.song_sections import SongSectionsAgent, SongSectionsInput


def _input(duration_s: float = 180.0) -> SongSectionsInput:
    return SongSectionsInput(
        file_uri="files/test-track",
        duration_s=duration_s,
        beat_timestamps_s=[0.5 * i for i in range(360)],
        audio_template_output={},
    )


def _section(
    rank: int,
    start_s: float,
    end_s: float,
    label: str = "chorus",
    energy: str = "high",
    use: str = "hook",
) -> dict:
    return {
        "rank": rank,
        "start_s": start_s,
        "end_s": end_s,
        "label": label,
        "energy": energy,
        "suggested_use": use,
        "rationale": "a real reason",
    }


def _agent() -> SongSectionsAgent:
    # parse() doesn't touch the model client. Pass None to bypass __init__.
    return SongSectionsAgent.__new__(SongSectionsAgent)


def test_happy_path_3_sections() -> None:
    raw = json.dumps(
        {
            "sections": [
                _section(1, 60.0, 90.0, label="chorus"),
                _section(2, 120.0, 150.0, label="drop", use="climax"),
                _section(3, 10.0, 30.0, label="hook", energy="medium"),
            ],
            "section_version": CURRENT_SECTION_VERSION,
        }
    )
    out = _agent().parse(raw, _input())
    assert len(out.sections) == 3
    assert out.section_version == CURRENT_SECTION_VERSION
    assert [s.rank for s in out.sections] == [1, 2, 3]


def test_version_clamp_forces_current() -> None:
    raw = json.dumps(
        {
            "sections": [_section(1, 60.0, 90.0)],
            "section_version": "ancient-version-string",
        }
    )
    out = _agent().parse(raw, _input())
    assert out.section_version == CURRENT_SECTION_VERSION


def test_bad_enum_label_drops_section() -> None:
    raw = json.dumps(
        {
            "sections": [
                _section(1, 60.0, 90.0, label="chorus"),
                _section(2, 120.0, 150.0, label="outropolis"),  # invalid enum
            ],
            "section_version": CURRENT_SECTION_VERSION,
        }
    )
    out = _agent().parse(raw, _input())
    assert len(out.sections) == 1
    assert out.sections[0].label == "chorus"


def test_four_sections_keeps_top_three_by_rank() -> None:
    raw = json.dumps(
        {
            "sections": [
                _section(1, 20.0, 40.0),
                _section(2, 60.0, 80.0),
                _section(3, 100.0, 120.0),
                # Fourth — Pydantic max_length=3 would error, but parse
                # truncates to top-3-by-rank after sorting.
            ]
            + [
                {
                    "rank": 3,
                    "start_s": 140.0,
                    "end_s": 160.0,
                    "label": "bridge",
                    "energy": "medium",
                    "suggested_use": "build",
                    "rationale": "duplicate rank",
                }
            ],
            "section_version": CURRENT_SECTION_VERSION,
        }
    )
    out = _agent().parse(raw, _input())
    # The duplicate rank=3 was dropped (rank dedup), so 3 survive.
    assert len(out.sections) == 3


def test_overlapping_sections_drop_lower_rank() -> None:
    # Rank 2 overlaps rank 1 by 20s (> 5s tolerance) — should be dropped.
    raw = json.dumps(
        {
            "sections": [
                _section(1, 60.0, 90.0),
                _section(2, 70.0, 100.0),  # overlaps rank 1 by 20s
                _section(3, 140.0, 165.0),  # disjoint
            ],
            "section_version": CURRENT_SECTION_VERSION,
        }
    )
    out = _agent().parse(raw, _input())
    assert len(out.sections) == 2
    ranks = sorted(s.rank for s in out.sections)
    assert ranks == [1, 3]


def test_duration_below_min_drops() -> None:
    # 10s window — under MIN_SECTION_DURATION_S (15s).
    raw = json.dumps(
        {
            "sections": [
                _section(1, 60.0, 70.0),  # 10s — too short
                _section(2, 100.0, 120.0),  # 20s — OK
            ],
            "section_version": CURRENT_SECTION_VERSION,
        }
    )
    out = _agent().parse(raw, _input())
    assert len(out.sections) == 1
    assert out.sections[0].rank == 2


def test_duration_above_max_drops() -> None:
    raw = json.dumps(
        {
            "sections": [
                _section(1, 30.0, 100.0),  # 70s — too long
                _section(2, 110.0, 130.0),  # OK
            ],
            "section_version": CURRENT_SECTION_VERSION,
        }
    )
    out = _agent().parse(raw, _input())
    assert len(out.sections) == 1


def test_negative_start_drops() -> None:
    # Pydantic ge=0 rejects negative start_s; section drops at the
    # Pydantic step, not the cross-field step.
    raw = json.dumps(
        {
            "sections": [
                _section(1, -5.0, 30.0),  # invalid
                _section(2, 60.0, 90.0),
            ],
            "section_version": CURRENT_SECTION_VERSION,
        }
    )
    out = _agent().parse(raw, _input())
    assert len(out.sections) == 1


def test_end_s_past_duration_drops() -> None:
    # Duration is 180s; end_s=200 is past tolerance (180+1).
    raw = json.dumps(
        {
            "sections": [
                _section(1, 120.0, 200.0),  # invalid
                _section(2, 60.0, 90.0),
            ],
            "section_version": CURRENT_SECTION_VERSION,
        }
    )
    out = _agent().parse(raw, _input(duration_s=180.0))
    assert len(out.sections) == 1


def test_end_s_within_float_tolerance_kept() -> None:
    # duration_s=180.0, end_s=180.5 → within 1s tolerance → keep.
    raw = json.dumps(
        {
            "sections": [_section(1, 130.0, 180.5)],
            "section_version": CURRENT_SECTION_VERSION,
        }
    )
    out = _agent().parse(raw, _input(duration_s=180.0))
    assert len(out.sections) == 1


def test_duplicate_ranks_keep_first() -> None:
    raw = json.dumps(
        {
            "sections": [
                _section(1, 20.0, 40.0, label="chorus"),
                _section(1, 100.0, 130.0, label="drop"),  # dup rank
                _section(2, 60.0, 85.0, label="hook"),
            ],
            "section_version": CURRENT_SECTION_VERSION,
        }
    )
    out = _agent().parse(raw, _input())
    assert len(out.sections) == 2
    # First-rank-1 wins.
    rank_1 = next(s for s in out.sections if s.rank == 1)
    assert rank_1.label == "chorus"


def test_all_sections_invalid_raises_refusal() -> None:
    raw = json.dumps(
        {
            "sections": [
                _section(1, 60.0, 70.0),  # too short
                _section(2, 30.0, 110.0),  # too long
            ],
            "section_version": CURRENT_SECTION_VERSION,
        }
    )
    with pytest.raises(RefusalError):
        _agent().parse(raw, _input())


def test_empty_sections_raises_refusal() -> None:
    raw = json.dumps({"sections": [], "section_version": CURRENT_SECTION_VERSION})
    with pytest.raises(RefusalError):
        _agent().parse(raw, _input())


def test_non_list_sections_raises_schema_error() -> None:
    raw = json.dumps({"sections": "not a list", "section_version": CURRENT_SECTION_VERSION})
    with pytest.raises(SchemaError):
        _agent().parse(raw, _input())


def test_invalid_json_raises_schema_error() -> None:
    with pytest.raises(SchemaError):
        _agent().parse("not json at all", _input())


def test_non_dict_top_level_raises_schema_error() -> None:
    raw = json.dumps([1, 2, 3])
    with pytest.raises(SchemaError):
        _agent().parse(raw, _input())
