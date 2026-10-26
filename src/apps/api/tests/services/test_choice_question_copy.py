"""KRI-479 review P2-4: the REAL rendered text of every question kind reads as English.

Failure modes: a capitalised reason dropped into the 'Unfortunately {reason}' template
('Unfortunately Only one recording...'), 'The most I can do is: 60 seconds' where 33 works, a
doubled space, a missing full stop. A table over every kind the collector can produce.
"""

from __future__ import annotations

import re

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.reply_language import reply_language_for
from app.services.choice_questions import (
    CONFLICT_SOURCE_COVERAGE,
    ChoiceCapability,
    choice_question_text,
    collect_conflicts,
    title_text_choice,
)
from app.services.length_planning import build_length_plan

CAP = ChoiceCapability(voice_route=True)


def _rows(count, *, dated=True, seconds=12.0):
    rows = []
    for i in range(count):
        row = {"media_id": f"c{i:02d}", "kind": "video", "duration_s": seconds}
        if dated:
            row["capture"] = {"capture_time": f"2026-06-01T{9 + i // 60:02d}:{i % 60:02d}:00Z"}
        rows.append(row)
    return rows


def _voice(voice_s=147.7, pictures=6, seconds=12.0):
    voice = {
        "media_id": "talk",
        "kind": "video",
        "duration_s": voice_s,
        "capture": {"capture_time": "2026-06-01T08:00:00Z"},
        "analysis": {
            "understanding": {
                "speech": {"has_speech": True, "to_camera": True, "transcript": "hello there you"}
            }
        },
    }
    return [voice, *_rows(pictures, seconds=seconds)]


def _brief(*reqs):
    return CreativeBrief(version=1, requirements=list(reqs))


def _timing(seconds):
    return BriefRequirement(
        id="t", kind="timing", scope="global", description=f"Keep {seconds} seconds",
        facts={"duration_s": seconds},
    )  # fmt: skip


def _order():
    return BriefRequirement(
        id="o", kind="order", scope="global", description="in the order I filmed",
        facts={"key": "capture_time"},
    )  # fmt: skip


def _strategy(**update):
    base = {
        "edit_format": "montage",
        "audio_strategy": "original_audio",
        "voice_mode": "continuous",
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": ["talk"]},
        "ordering_choice": "chronological",
        "target_duration_s": 30,
        "target_duration_requested": True,
    }
    return CreativeStrategy.model_validate({**base, **update})


def _coverage_text(codes, *, strategy=None, sources=None, **envelope) -> str:
    """The question text for a durable length-plan conflict, through the public collector."""
    plan = {
        "conflicts": [{"code": code} for code in codes],
        "sources": [{"duration_s": 10.0}] if sources is None else sources,
        **envelope,
    }
    found = collect_conflicts(
        strategy or {"edit_format": "montage"}, None, {"length_plan": plan}, CAP
    )
    coverage = [c for c in found if c.kind == CONFLICT_SOURCE_COVERAGE]
    assert coverage, codes
    return choice_question_text(coverage[0].candidate())


def _built_plan_text(snapshot_rows, selected_ids) -> str:
    plan = build_length_plan(
        {"edit_format": "montage"}, {"clip_assignments": snapshot_rows}, selected_ids=selected_ids
    )
    found = collect_conflicts(
        {"edit_format": "montage"},
        None,
        {"clip_assignments": snapshot_rows, "length_plan": plan},
        CAP,
    )
    coverage = [c for c in found if c.kind == CONFLICT_SOURCE_COVERAGE]
    assert coverage, plan["conflicts"]
    return choice_question_text(coverage[0].candidate())


_COVERAGE_CASES = {
    "coverage_source_below_minimum": dict(codes=["source_below_minimum"]),
    "coverage_no_usable_sources": dict(codes=["no_usable_sources"]),
    "coverage_cap_below_coverage": dict(codes=["cap_below_coverage"]),
    "coverage_source_count_limit": dict(
        codes=[], sources=[{"duration_s": 5.0}] * 3, max_source_count=2
    ),
    "coverage_snapshot_min_duration": dict(codes=[], sources=[{"duration_s": 1.0}]),
    "coverage_natural_source_capacity": dict(
        codes=["insufficient_capacity"],
        sources=[{"duration_s": 4.0}],
        natural_owner="voiceover",
        natural_duration_s=30.0,
    ),
    "coverage_hold_coverage": dict(
        codes=["target_below_coverage"],
        strategy={"edit_format": "montage", "montage_cadence": "fast"},
        target_duration_s=5.0,
    ),
    "coverage_hold_coverage_ceiling": dict(
        codes=["target_below_coverage"],
        strategy={"edit_format": "montage", "montage_cadence": "fast"},
        target_duration_s=5.0,
        target_duration_bound="at_most",
    ),
    "coverage_single_hero_order": dict(codes=["single_hero_order_conflict"]),
    "coverage_insufficient_media": dict(codes=["insufficient_media"]),
    "coverage_duration_out_of_bounds": dict(codes=["duration_out_of_bounds"]),
    "coverage_named_moments": dict(codes=["named_moments_unplaceable"]),
}


def _texts() -> dict[str, str]:
    out: dict[str, str] = {}

    def add(name, strategy, brief, rows):
        found = collect_conflicts(strategy, brief, {"clip_assignments": rows}, CAP)
        assert found, name
        out[name] = choice_question_text(found[0].candidate())

    plain = CreativeStrategy(
        edit_format="montage", target_duration_s=15, target_duration_requested=True
    )
    # Included footage has the founder-approved 100 ms floor.  41 clips need
    # 4.1 seconds, which exceeds a 3-second request's 10% allowance.
    count_strategy = plain.model_copy(update={"target_duration_s": 3})
    add("duration_vs_count", count_strategy, _brief(_timing(3)), _rows(41, seconds=3.0))
    add(
        "order_basis",
        CreativeStrategy(edit_format="montage", ordering_choice="chronological"),
        _brief(_order()),
        _rows(5, dated=False),
    )
    add("voice_short", _strategy(), _brief(_timing(30), _order()), _voice(voice_s=20.0))
    add("how_long_many", _strategy(), _brief(_order()), _voice())
    add("how_long_single", _strategy(), _brief(_order()), _voice(pictures=41, seconds=0.9))
    two = _strategy(
        montage_audio={"preserve_source_audio": True, "source_media_ids": ["talk", "c00"]}
    )
    add("which_voice", two, _brief(_timing(30)), _voice())
    out["title_text"] = choice_question_text(title_text_choice(["r1"]).candidate())
    for name, case in _COVERAGE_CASES.items():
        out[name] = _coverage_text(**case)
    return out


TEXTS = _texts()


@pytest.mark.parametrize("name", sorted(TEXTS))
def test_no_capitalised_reason_after_unfortunately_and_no_stray_spacing(name):
    text = TEXTS[name]
    assert not re.search(r"Unfortunately [A-Z]", text), text
    assert "  " not in text and " ." not in text and ".." not in text, text
    assert text.strip() == text
    assert all(line.strip() for line in text.splitlines()), text
    assert text.splitlines()[0].endswith((".", "?", "!", ":")), text


def test_the_voice_questions_read_as_written():
    assert TEXTS["which_voice"].splitlines()[0] == (
        "More than one of your clips has someone talking. Unfortunately only one recording "
        "can be the voice that plays under the rest. Which do you prefer?"
    )
    assert TEXTS["how_long_many"].splitlines()[0] == (
        "Your voice runs 147.7 seconds. How long should the video be?"
    )
    assert "5 seconds (recommended)" in TEXTS["how_long_many"]


def test_a_single_length_is_a_statement_not_the_most_i_can_do():
    assert TEXTS["how_long_single"] == (
        "Your voice runs 147.7 seconds. 41 clips need at least 32.8 seconds to each be seen. "
        'I can make it 33 seconds. Reply "33 seconds" to go with that, or change your request.'
    )


def test_source_coverage_never_puts_an_instruction_after_unfortunately():
    for name in _COVERAGE_CASES:
        first = TEXTS[name].splitlines()[0]
        assert "Unfortunately " in first, first
        assert not re.search(r"Unfortunately (choose|replace|add|select)\b", first, re.I), first


def test_a_clip_that_is_no_longer_attached_is_not_blamed_on_clip_length():
    """The prod Cunda thread: a selected id with no clip row said 'shorter than 0.1 seconds'."""

    rows = [{"media_id": "a", "kind": "video", "duration_s": 5.0}]
    text = _built_plan_text(rows, selected_ids=["a", "gone"])
    assert "0.1" not in text, text
    assert "no longer attached to this edit" in text, text
    assert "Select your clips again, or add them back." in text, text
    with reply_language_for("tr"):
        assert "0,1" not in _built_plan_text(rows, selected_ids=["a", "gone"])


def test_a_clip_that_really_is_too_short_still_says_so():
    rows = [
        {"media_id": "a", "kind": "video", "duration_s": 5.0},
        {"media_id": "b", "kind": "video", "duration_s": 0.05},
    ]
    text = _built_plan_text(rows, selected_ids=["a", "b"])
    assert "shorter than 0.1 seconds" in text, text
    assert "no longer attached" not in text, text
